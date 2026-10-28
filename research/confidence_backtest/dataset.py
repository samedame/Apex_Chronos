"""One per-trade dataset for both samples, built by one code path from real 1-minute bars.

  backtest : the 21 sessions in verification/fixtures.tar.gz (2026-09-16 .. 2026-10-14)
  forward  : the 10 live sessions 2026-10-15 .. 2026-10-28, from the snapshot written by forward_replay.py

Every feature is computed from bars up to and including the entry bar only (nothing that happens after the alert).
Outcomes (roi, win) come from the bot's own engine, i.e. model-priced options on real underlying bars.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from apex_chronos import alert_confidence as AC, config as C, replay      # noqa: E402
from apex_chronos.features import compute_features                          # noqa: E402
from verification.fixtures import extract                                   # noqa: E402
import forward_replay as FR                                                 # noqa: E402

SHORT = {s.name: k for k, s in C.MODULES.items()}
INDEX = set(C.INDEX_0DTE)
CRYPTO = {'COIN', 'MSTR'}


def ticker_class(t: str) -> str:
    return 'index' if t in INDEX else ('crypto' if t in CRYPTO else 'single')


def minute_bucket(m: int) -> str:
    return 'a_open<30' if m < 30 else ('b_am30-90' if m < 90 else ('c_mid90-210' if m < 210 else 'd_pm210+'))


def sent_lateness(date: str, ticker: str, module: str, m: int):
    """Seconds late the live bot actually posted this entry (None if it never posted it)."""
    p = os.path.join(ROOT, 'state', f'sent-{date}.json')
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        sent = json.load(fh)
    v = sent.get(f'{date}|ENTRY|{ticker}|{module}|{m}')
    return v['late_s'] if v and v.get('posted') else None


def _rows(sample: str, days_by_date, trades: pd.DataFrame, events) -> pd.DataFrame:
    ent = {(d, e.ticker, e.module, e.minute): e.data for d, e in events if e.kind == 'ENTRY'}
    feats_cache = {}
    rows = []
    for t in trades.sort_values(['date', 'm_entry', 'slot']).to_dict('records'):
        d, tk, m = t['date'], t['ticker'], int(t['m_entry'])
        td = days_by_date[d][tk]
        key = (d, tk)
        if key not in feats_cache:
            feats_cache[key] = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
        f = feats_cache[key]
        ev = ent.get((d, tk, t['module'], m), {})
        sign = t['direction']
        o0 = td.O[0]
        gap = o0 / td.ctx.prev_c - 1.0
        z = (td.C[m] - f.vwap[m]) / max(f.vwap_std[m], td.C[m] * 0.0005)
        rows.append({
            'sample': sample, 'date': d, 'ticker': tk, 'module': SHORT[t['module']], 'phase': t['phase'],
            'structure': ev.get('structure'), 'direction': sign, 'm_entry': m, 'hold_mins': t['hold_mins'],
            'roi': t['roi'], 'win': int(t['roi'] > 0), 'peak_roi': t['peak_roi'], 'exit_reason': t['exit_reason'],
            'cost_basis': t['cost_basis'], 'rvol_entry': t['rvol_entry'], 'bot_score': t['score'], 'slot': t['slot'],
            # --- alert-time features -------------------------------------------------------------------------
            'tclass': ticker_class(tk), 'mbucket': minute_bucket(m), 'weekday': pd.Timestamp(d).dayofweek,
            'vix': float(td.vix[m]), 'iv_entry': float(f.iv[m]), 'abs_gap': abs(gap), 'gap_dir': gap * sign,
            'day_move_dir': (td.C[m] / o0 - 1.0) * sign, 'vwap_z_dir': z * sign,
            'range_so_far': (td.H[:m + 1].max() - td.L[:m + 1].min()) / o0, 'rvol20': td.ctx.rvol20,
            'S_entry': ev.get('S_entry', float(td.C[m])),
        })
    df = pd.DataFrame(rows)
    df['nth_of_day'] = df.groupby('date').cumcount() + 1
    # shipped score as it would have been posted: on time and fresh (the simulator cannot observe either), capital unset
    df['score_ontime'] = [AC.score_alert({'module': C.MODULES[r.module].name, 'ticker': r.ticker, 'late_s': 6.0,
                                          'data_age_s': 900.0, 'contracts': None}).score for r in df.itertuples()]
    df['setup_part'] = [AC.score_alert({'module': C.MODULES[r.module].name, 'ticker': r.ticker}).setup for r in df.itertuples()]
    df['exec_part'] = [AC.score_alert({'module': C.MODULES[r.module].name, 'ticker': r.ticker, 'late_s': 6.0,
                                       'data_age_s': 900.0, 'contracts': None}).execution for r in df.itertuples()]
    df['late_s_sent'] = [sent_lateness(r.date, r.ticker, C.MODULES[r.module].name, r.m_entry) if r.sample == 'forward' else None
                         for r in df.itertuples()]
    return df


_CACHE = {}


def backtest_days():
    if 'bt' not in _CACHE:
        d = extract(os.path.join(ROOT, 'verification', 'fixtures.tar.gz'), tempfile.mkdtemp())
        _CACHE['bt'] = replay.build_days(d, list(C.TICKERS))
    return _CACHE['bt']


def forward_days():
    if 'fw' not in _CACHE:
        dts = FR.ledger_dates()
        _CACHE['fw'] = (dts, FR.build_forward_days(dts))
    return _CACHE['fw']


def build(restrict_forward_to_ledger_matched: bool = False) -> pd.DataFrame:
    dts, days = backtest_days()
    tr, ev = replay.run_all(None, list(C.TICKERS), mode='fast', days_cache=(dts, days))
    bt = _rows('backtest', days, tr, ev)
    fdates, fdays = forward_days()
    ftr, fev, _ = FR.replay_forward(fdates, fdays)
    fw = _rows('forward', fdays, ftr, fev)
    if restrict_forward_to_ledger_matched:
        import csv
        with open(os.path.join(ROOT, 'ledger', 'trades.csv')) as fh:
            led = {(r['date'], r['ticker'], SHORT[r['module']], int(r['m_entry'])) for r in csv.DictReader(fh)}
        fw = fw[[(r.date, r.ticker, r.module, r.m_entry) in led for r in fw.itertuples()]]
    return pd.concat([bt, fw], ignore_index=True)


def day_sets():
    """For the stress tests: {sample: (dates, {date: {ticker: TickerDay}})}."""
    bdts, bdays = backtest_days()
    fdts, fdays = forward_days()
    return {'backtest': (bdts, bdays), 'forward': (fdts, fdays)}


if __name__ == '__main__':
    df = build()
    out = os.path.join(HERE, 'data', 'trades_features.csv')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False)
    print(df.groupby('sample').agg(n=('roi', 'size'), sessions=('date', 'nunique'), win=('win', 'mean'), mean_roi=('roi', 'mean')).round(4))
    print('saved', out)
