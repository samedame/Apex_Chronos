#!/usr/bin/env python3
"""Replay of the pre-registered lab tests (docs/LAB_PREREG.md) on the archived P2 trades.

Refuses to run unless docs/LAB_PREREG.md is committed and byte-identical to the commit that first added it.
Descriptive only: with this few sessions no result here is confirmatory (the pre-registration says so).

    python scripts/lab_replay.py [--out docs/LAB_REPLAY_2026-11-14.md] [--json /tmp/lab_replay.json]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from apex_chronos import verdict                                             # noqa: E402
from apex_chronos.lab import events as EV, indicators as I, shadow_eval as SE, stats as S   # noqa: E402

PREREG = 'docs/LAB_PREREG.md'
BACKFILL = 'ledger/p2_grading_backfill_2026-10-16_2026-11-13.csv'
R8_MINUTE = 120            # implementation choice for R8 (11:30 ET, inside P2's window); the pre-registration does not name a minute
MIN_ARM = 8
SLOT = 0.10
NAN = float('nan')


def _git(*args: str) -> str:
    return subprocess.run(['git', '-C', ROOT, *args], capture_output=True, text=True, check=True).stdout


def verify_prereg() -> Dict[str, str]:
    """Raise SystemExit unless the pre-registration is committed and unmodified."""
    try:
        _git('ls-files', '--error-unmatch', PREREG)
        dirty = _git('status', '--porcelain', '--', PREREG).strip()
        first = _git('log', '--diff-filter=A', '--format=%H', '--', PREREG).split()[-1]
        committed = subprocess.run(['git', '-C', ROOT, 'show', f'{first}:{PREREG}'], capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, IndexError):
        raise SystemExit(f'REFUSED: {PREREG} is not committed. Commit it before any replay.')
    with open(os.path.join(ROOT, PREREG), 'rb') as fh:
        current = fh.read()
    if dirty or current != committed:
        raise SystemExit(f'REFUSED: {PREREG} differs from the commit that froze it ({first[:8]}).')
    return {'commit': first, 'sha256': hashlib.sha256(current).hexdigest()}


def load_trades() -> List[Dict]:
    ledger = {}
    with open(os.path.join(ROOT, 'ledger', 'trades.csv'), newline='') as fh:
        for r in csv.DictReader(fh):
            if r['phase'] == 'P2':
                ledger[(r['date'], r['ticker'], int(r['m_entry']))] = r
    out = []
    with open(os.path.join(ROOT, BACKFILL), newline='') as fh:
        for b in csv.DictReader(fh):
            k = (b['date'], b['ticker'], int(b['m_entry']))
            L = ledger.get(k)
            if not L:
                continue
            out.append({'date': b['date'], 'ticker': b['ticker'], 'm_entry': k[2], 'm_exit': int(b['m_exit_model']), 'direction': int(L['direction']),
                        'roi': float(b['roi_model']), 'exit_reason': b['exit_model'], 'module': L['module'], 'phase': 'P2',
                        'listed': bool(int(b['listed_same_day'])), 'iv_model': float(b['iv_model_entry']),
                        'roi_fair': float(b['roi_fair']) if b['roi_fair'] else None,
                        'm_exit_fair': int(b['m_exit_fair']) if b['m_exit_fair'] else None})
    return out


def build_rows(trades: List[Dict], state_dir: str) -> List[Dict]:
    by_day = defaultdict(list)
    for t in trades:
        by_day[t['date']].append(t)
    hist_vix = {}
    earn, divs = EV.load_cache(state_dir, 'earnings'), EV.load_cache(state_dir, 'dividends')
    events = EV.load_events()
    rows = []
    for iso, day_trades in sorted(by_day.items()):
        d = date.fromisoformat(iso)
        b = verdict.load_bars(state_dir, d)
        prior = verdict.archived_sessions(state_dir, d, 20)
        plain = {tk: {k: np.asarray(v[k], dtype=float) for k in 'OHLCV'} for tk, v in b['tickers'].items()}
        for t in day_trades:
            tk = t['ticker']
            prev_c = NAN
            for p in reversed(prior):
                x = p.get('tickers', {}).get(tk)
                if x and len(x['C']):
                    prev_c = float(x['C'][-1])
                    break
            today = dict(plain[tk], vix=np.asarray(b['VIX'], dtype=float), prev_c=prev_c)
            r = SE.evaluate_trade(d, t, today, prior, state_dir=state_dir, iv_model=t['iv_model'], roi_fair=t['roi_fair'], day_trades=day_trades,
                                  events=events, earnings_rows=earn, exdiv_rows=divs, hist_vix=hist_vix, all_tickers_today=plain, listed=t['listed'])
            r['m_exit_fair'] = t['m_exit_fair'] if t['m_exit_fair'] is not None else ''
            rows.append(r)
    return rows


# ---- tests R1-R8 --------------------------------------------------------------------------------------------------------------------
def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return NAN


def flag_test(rows: List[Dict], flag: str, outcome: str, passes=None) -> Dict:
    use = [r for r in rows if math.isfinite(_num(r[outcome])) and (flag is None or math.isfinite(_num(r[flag])))]
    ok = [r for r in use if (passes(r) if passes else _num(r[flag]) == 1.0)]
    bad = [r for r in use if r not in ok]
    res = S.clustered_diff([r[outcome] for r in ok], [r['date'] for r in ok], [r[outcome] for r in bad], [r['date'] for r in bad])
    res.update(n_pass=len(ok), n_fail=len(bad), mean_pass=float(np.mean([r[outcome] for r in ok])) if ok else NAN,
               mean_fail=float(np.mean([r[outcome] for r in bad])) if bad else NAN,
               lb_pass=S.clustered_mean_lb([r[outcome] for r in ok], [r['date'] for r in ok]) if ok else NAN,
               sessions=len({r['date'] for r in use}))
    return res


def classify(res: Dict, p_adj: float) -> str:
    if res['n_pass'] < MIN_ARM or res['n_fail'] < MIN_ARM:
        return 'underpowered'
    if math.isfinite(p_adj) and p_adj < 0.05 and math.isfinite(res['lb_pass']) and res['lb_pass'] > 0:
        return 'supported'
    return 'not supported'


def r8(state_dir: str) -> Dict:
    days = sorted(f[:10] for f in os.listdir(os.path.join(state_dir, 'bars')) if f.endswith('.json.gz'))
    per_day: Dict[str, List] = {}
    pooled_a, pooled_b = [], []
    est = defaultdict(list)
    for iso in days:
        d = date.fromisoformat(iso)
        prior = verdict.archived_sessions(state_dir, d, 20)
        if len(prior) < 5:
            continue
        b = verdict.load_bars(state_dir, d)
        la, lb = [], []
        for tk, v in b['tickers'].items():
            if len(v['C']) < 350:
                continue
            ps = SE.prior_series(prior, tk)
            if len(ps) < 5:
                continue
            c = np.asarray(v['C'], dtype=float)
            real = float(np.sum(I.log_returns(c)[R8_MINUTE:] ** 2))
            pc = [p['C'] for p in ps]
            f = I.rest_of_day_var_forecast(pc, c, R8_MINUTE)['forecast']
            m14 = I.mean14_rest_forecast(pc, R8_MINUTE)
            qa, qb = I.qlike(f, real), I.qlike(m14, real)
            if math.isfinite(qa) and math.isfinite(qb):
                la.append(qa); lb.append(qb)
                O, H, L, Cc = (np.asarray(v[k], dtype=float)[:R8_MINUTE + 1] for k in 'OHLC')
                n_rest = len(c) - 1 - R8_MINUTE
                per_bar = {'close_to_close': I.realised_var(c, 0, R8_MINUTE) / R8_MINUTE, 'parkinson': I.parkinson(H, L),
                           'garman_klass': I.garman_klass(O, H, L, Cc), 'rogers_satchell': I.rogers_satchell(O, H, L, Cc)}
                for name, x in per_bar.items():
                    est[name].append(I.qlike(x * n_rest, real) if np.isfinite(x) else NAN)
                est['seasonality_forecast'].append(qa); est['mean14'].append(qb)
        if la:
            per_day[iso] = [float(np.mean(la)), float(np.mean(lb)), len(la)]
            pooled_a += la; pooled_b += lb
    ser_a = [v[0] for v in per_day.values()]; ser_b = [v[1] for v in per_day.values()]
    dm = S.diebold_mariano(ser_a, ser_b, lag=4)
    pooled = S.diebold_mariano(pooled_a, pooled_b, lag=4)
    return {'minute': R8_MINUTE, 'sessions': len(per_day), 'pairs': len(pooled_a), 'qlike_seasonality': float(np.mean(ser_a)) if ser_a else NAN,
            'qlike_mean14': float(np.mean(ser_b)) if ser_b else NAN, 'dm_session': dm, 'dm_pooled_sensitivity': pooled,
            'estimator_qlike': {k: float(np.nanmean(v)) if len(v) else NAN for k, v in est.items()}}


def descriptive(rows: List[Dict], trades: List[Dict]) -> Dict:
    out: Dict = {}
    events = EV.load_events()
    ev_days = {}
    for r in rows:
        d = date.fromisoformat(r['date'])
        mf = EV.macro_flags(d, events)
        tag = [k for k in ('fomc_day', 'cpi_day', 'nfp_day', 'ppi_day') if mf.get(k) == 1.0]
        if tag:
            ev_days.setdefault(r['date'], {'events': tag, 'trades': 0, 'roi_model': 0.0, 'roi_fair': []})
            ev_days[r['date']]['trades'] += 1
            ev_days[r['date']]['roi_model'] += r['roi_model']
            if math.isfinite(_num(r['roi_fair'])):
                ev_days[r['date']]['roi_fair'].append(r['roi_fair'])
    out['event_days'] = ev_days
    # (b) one index slot cap: static removal
    def sess_pnl(rs, col):
        by = defaultdict(float)
        for r in rs:
            v = _num(r[col])
            if math.isfinite(v):
                by[r['date']] += SLOT * v
        return list(by.values())
    cap = {}
    for col in ('roi_model', 'roi_fair'):
        kept = [r for r in rows if r['index_slot_conflict'] == 0.0]
        cap[col] = {'before': S.tail_summary(sess_pnl(rows, col)), 'after': S.tail_summary(sess_pnl(kept, col)),
                    'trades_removed': sum(1 for r in rows if r['index_slot_conflict'] == 1.0 and math.isfinite(_num(r[col])))}
    out['index_cap'] = cap
    out['open_after_1530'] = {'model': sum(1 for r in rows if r['m_exit'] > 360), 'fair': sum(1 for t in trades if t['m_exit_fair'] and t['m_exit_fair'] > 360), 'n': len(rows)}
    return out


TESTS = [('R1', 'Noise Area: inside band', 'f6_inside_band', None), ('R2', 'Noise Area: strike >= 1.0 x band', 'f6_strike_ge_1_0', None),
         ('R3', 'Noise Area: strike >= 1.5 x band', 'f6_strike_ge_1_5', None), ('R4', 'VR(5) < 1', 'vr5_lt1', None),
         ('R5', 'Touch probability <= 0.25', 'f8_touch_le_25', None), ('R6', 'Touch probability <= 0.35', 'f8_touch_le_35', None),
         ('R7', 'Put-credit minus call-credit', None, lambda r: r['direction'] == 1)]


def run(state_dir: str, ledger_dir: str) -> Dict:
    trades = load_trades()
    rows = build_rows(trades, state_dir)
    for iso in sorted({r['date'] for r in rows}):
        SE.write_rows(ledger_dir, date.fromisoformat(iso), [r for r in rows if r['date'] == iso], name='p2_shadow_backfill.csv')
    res = {'n_trades': len(rows), 'n_fair': sum(1 for r in rows if math.isfinite(_num(r['roi_fair']))), 'tests': {}}
    primary = {}
    for tid, name, flag, passes in TESTS:
        primary[tid] = flag_test(rows, flag, 'roi_fair', passes)
    r8res = r8(state_dir)
    pvals = {tid: primary[tid]['p'] for tid in primary}
    pvals['R8'] = r8res['dm_session']['p']
    adj = S.holm(pvals)
    for tid, name, flag, passes in TESTS:
        sec = flag_test(rows, flag, 'roi_model', passes)
        res['tests'][tid] = {'name': name, 'primary_roi_fair': primary[tid], 'p_holm': adj[tid], 'verdict': classify(primary[tid], adj[tid]), 'secondary_roi_model': sec}
    res['R8'] = dict(r8res, p_holm=adj['R8'], verdict='supported' if math.isfinite(adj['R8']) and adj['R8'] < 0.05 else 'not supported')
    res['descriptive'] = descriptive(rows, trades)
    res['coverage'] = {k: sum(1 for r in rows if math.isfinite(_num(r[k]))) for k in ('f6_inside_band', 'vr5_lt1', 'f8_touch_le_25', 'strike_beyond_band')}
    return res


def _f(x, nd=2, pct=True):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return 'n/a'
    return f'{x * 100:+.{nd}f}%' if pct else f'{x:.{nd}f}'


def to_markdown(res: Dict, frozen: Dict[str, str]) -> str:
    L = ['# Lab replay on the archived P2 trades (2026-10-16 to 2026-11-13)', '',
         f"Pre-registration verified before running: `{PREREG}` sha256 `{frozen['sha256'][:16]}...`, frozen in commit `{frozen['commit'][:8]}`.", '',
         f"Sample: {res['n_trades']} P2 trades, {res['n_fair']} with a fair-vol grade. **Descriptive only.** With roughly 20 sessions no row below is "
         'confirmatory; the table ranks which shadow flags earn a place in the forward sample. Outcome: `roi_fair` (primary, in the Holm family), '
         '`roi_model` (secondary, reported raw).', '', '## R1-R7 (passed minus failed, per trade, one-sided, session-clustered bootstrap)', '',
         '| ID | Flag | n pass / fail | mean pass | mean fail | diff | raw p | Holm p (of 8) | LB95 of passed mean | verdict | secondary (model prices): n, diff, raw p |',
         '|---|---|---|---|---|---|---|---|---|---|---|']
    for tid, t in res['tests'].items():
        p, s = t['primary_roi_fair'], t['secondary_roi_model']
        L.append(f"| {tid} | {t['name']} | {p['n_pass']} / {p['n_fail']} | {_f(p['mean_pass'])} | {_f(p['mean_fail'])} | {_f(p['diff'])} | {_f(p['p'], 3, False)} | "
                 f"{_f(t['p_holm'], 3, False)} | {_f(p['lb_pass'])} | **{t['verdict']}** | {s['n_pass']}+{s['n_fail']}, {_f(s['diff'])}, p {_f(s['p'], 3, False)} |")
    r = res['R8']
    d = r['dm_session']
    L += ['', '## R8: remaining-session variance forecast (QLIKE, lower is better)', '',
          f"Evaluated at minute {r['minute']} (11:30 ET) for every (ticker, session) with at least 5 prior archived sessions: {r['sessions']} sessions, "
          f"{r['pairs']} ticker-session pairs. **The pre-registration does not name the minute; this is an implementation choice, stated here.** "
          'The DM test runs on the per-session mean loss differential (Newey-West lag 4); the pooled pair-level result is a sensitivity check only.', '',
          f"| | seasonality forecast | 14-session mean |", '|---|---|---|', f"| mean QLIKE (session means) | {_f(r['qlike_seasonality'], 3, False)} | {_f(r['qlike_mean14'], 3, False)} |", '',
          f"DM (sessions): stat {_f(d['stat'], 2, False)}, one-sided p {_f(d['p'], 3, False)}, Holm p {_f(r['p_holm'], 3, False)} -> **{r['verdict']}**. "
          f"Pooled sensitivity: p {_f(r['dm_pooled_sensitivity']['p'], 3, False)} (n {r['dm_pooled_sensitivity']['n']}).", '',
          'QLIKE of single estimators as rest-of-day forecasts (morning estimate scaled by bar count, no seasonality; descriptive):', '',
          '| estimator | mean QLIKE |', '|---|---|']
    for k, v in r['estimator_qlike'].items():
        L.append(f'| {k} | {_f(v, 3, False)} |')
    de = res['descriptive']
    L += ['', '## Descriptive items', '', '**(a) Event days in the window** (calendar covers the Oct 28 FOMC and the Nov 6/10/13 releases; the October CPI/PPI/NFP dates fall before 10-16 or are not in the calendar):', '']
    if de['event_days']:
        L += ['| date | events | P2 trades | sum roi_model | mean roi_fair |', '|---|---|---|---|---|']
        for k, v in sorted(de['event_days'].items()):
            rf = np.mean(v['roi_fair']) if v['roi_fair'] else NAN
            L.append(f"| {k} | {', '.join(v['events'])} | {v['trades']} | {_f(v['roi_model'])} | {_f(float(rf))} |")
    else:
        L.append('No P2 trade fell on a listed event day.')
    L += ['', '**(b) One-index-slot cap** (static removal of a P2 trade that overlaps an open SPY/QQQ/IWM P2 trade; session P&L = 10% x sum of trade ROI):', '',
          '| outcome | | sessions | mean | worst session | CVaR95 | trades removed |', '|---|---|---|---|---|---|---|']
    for col, v in de['index_cap'].items():
        for lab in ('before', 'after'):
            t = v[lab]
            L.append(f"| {col} | {lab} | {t['n_sessions']} | {_f(t['mean'], 3)} | {_f(t['worst'], 3)} | {_f(t['cvar95'], 3)} | {v['trades_removed'] if lab == 'after' else ''} |")
    o = de['open_after_1530']
    L += ['', f"**(c) Trades still open after 15:30 ET:** {o['model']} of {o['n']} at model exits; {o['fair']} at fair-vol exits.", '',
          f"Coverage of the feature columns (trades with a finite value): {res['coverage']}.", '',
          'Rows are in `ledger/lab/p2_shadow_backfill.csv`. Nothing here changes P2.']
    return '\n'.join(L) + '\n'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    ap.add_argument('--json', default=None)
    ap.add_argument('--state-dir', default=os.path.join(ROOT, 'state'))
    ap.add_argument('--ledger-dir', default=os.path.join(ROOT, 'ledger'))
    a = ap.parse_args(argv)
    frozen = verify_prereg()
    res = run(a.state_dir, a.ledger_dir)
    md = to_markdown(res, frozen)
    if a.out:
        with open(a.out, 'w') as fh:
            fh.write(md)
    if a.json:
        with open(a.json, 'w') as fh:
            json.dump(res, fh, indent=1, default=lambda o: None if isinstance(o, float) and not math.isfinite(o) else str(o))
    print(md)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
