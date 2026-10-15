"""Prove the bot behaves the way the strategy was backtested.

    python -m verification.verify_against_backtest --data-dir /path/to/workspace/data

The ORIGINAL research code (verification/reference/, pinned by SHA-256) is the
oracle. The bot engine is run over the same recorded data and compared to it:

  A  oracle integrity           hashes match; oracle reproduces the saved ledger
  B  feature parity             every VWAP/EMA/noise/RVOL/IV array equals the original's
  C  candidate parity           the bot generates exactly the original's pre-scheduler candidates
  D  trade-for-trade parity     entries, exits, reasons, slots, costs, ROI (12-ticker ledger)
  E  extended universe          the same comparison on 24 tickers (131 trades from 364 candidates)
  F  live-path / no look-ahead  features rebuilt each minute from ONLY the bars seen so far
  G  poll cadence invariance    stepping in bursts of 3/7/15/45 minutes gives identical trades
  H  restart equivalence        a cold-started engine that catches up equals one that ran all day
  I  scheduler invariants       <=3 concurrent, phase limits, one trade per (ticker, phase)
  J  differential fuzzing       original vs bot on many perturbed markets (far more collisions/exits)
  K  mutation power             deliberately broken bots must FAIL the comparison
  L  live-vs-backtest gaps      (informational) clock-grid indexing, delayed VIX
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import inspect
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from apex_chronos import config as C
from apex_chronos import features as F
from apex_chronos import engine as E
from apex_chronos import replay, signals
from apex_chronos.engine import SessionEngine
from verification import fuzz
from verification import reference_runner as rr

KEY = ['ticker', 'date', 'm_entry', 'm_exit', 'module', 'direction', 'exit_reason', 'slot']
NUM = ['cost_basis', 'roi', 'peak_roi', 'rvol_entry', 'score']
TOL = 1e-9
CORE12 = list(C.TICKERS)
EXT24 = CORE12 + ['AVGO', 'ORCL', 'SMH', 'MU', 'GOOGL', 'JPM', 'NFLX', 'UBER', 'XLF', 'BA', 'PLTR', 'GLD']


@dataclass
class Check:
    id: str
    name: str
    passed: Optional[bool]          # None = informational
    detail: str
    data: dict = field(default_factory=dict)


def _sorted(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(['date', 'm_entry', 'ticker', 'module']).reset_index(drop=True)


def _empty_safe(df: pd.DataFrame, cols) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame({c: [] for c in cols})
    return df


def compare_trades(oracle: pd.DataFrame, bot: pd.DataFrame) -> dict:
    cols = KEY + NUM + ['phase']
    a, b = _sorted(_empty_safe(oracle, cols)), _sorted(_empty_safe(bot, cols))
    ka = set(map(tuple, a[KEY].astype(str).values))
    kb = set(map(tuple, b[KEY].astype(str).values))
    out = {'n_oracle': len(a), 'n_bot': len(b), 'only_oracle': sorted(ka - kb), 'only_bot': sorted(kb - ka)}
    out['decisions_equal'] = (len(a) == len(b)) and a[KEY].equals(b[KEY]) if len(a) == len(b) else False
    diffs = {}
    if len(a) == len(b) and out['decisions_equal']:
        for c in NUM:
            diffs[c] = float(np.abs(a[c].astype(float) - b[c].astype(float)).max()) if len(a) else 0.0
    out['max_abs_diff'] = diffs
    out['numeric_ok'] = bool(diffs) and all(v <= TOL for v in diffs.values()) if len(a) else True
    out['ok'] = out['decisions_equal'] and out['numeric_ok']
    return out


def compare_candidates(oracle_c: pd.DataFrame, bot_c: pd.DataFrame) -> dict:
    ck = ['ticker', 'date', 'module', 'm_entry', 'direction']
    cols = ck + ['score', 'rvol_entry']
    a = _empty_safe(oracle_c, cols).sort_values(ck).reset_index(drop=True)
    b = _empty_safe(bot_c, cols).sort_values(ck).reset_index(drop=True)
    ka = set(map(tuple, a[ck].astype(str).values))
    kb = set(map(tuple, b[ck].astype(str).values))
    out = {'n_oracle': len(a), 'n_bot': len(b), 'only_oracle': sorted(ka - kb), 'only_bot': sorted(kb - ka)}
    if not out['only_oracle'] and not out['only_bot'] and len(a) == len(b):
        out['max_abs_diff_score'] = float(np.abs(a['score'].astype(float) - b['score'].astype(float)).max())
        out['max_abs_diff_rvol'] = float(np.abs(a['rvol_entry'].astype(float) - b['rvol_entry'].astype(float)).max())
        out['ok'] = out['max_abs_diff_score'] <= TOL and out['max_abs_diff_rvol'] <= TOL
    elif not len(a) and not len(b):
        out['ok'] = True
    else:
        out['ok'] = False
    return out


def stats(df: pd.DataFrame) -> dict:
    if df is None or len(df) == 0:
        return {'n': 0}
    r = df['roi'].astype(float).values
    w, l = r[r > 0], r[r <= 0]
    return {'n': int(len(r)), 'win_rate': float(len(w) / len(r)), 'profit_factor': float(w.sum() / max(abs(l.sum()), 1e-4)),
            'mean_roi': float(r.mean())}


def bot_run(data_dir, tickers, days_cache, **kw):
    df, events = replay.run_all(data_dir, tickers, days_cache=days_cache, **kw)
    return df, replay.run_all.last_candidates, events


def event_signature(events) -> list:
    sig = []
    for d, e in events:
        extra = ''
        if e.kind == 'ENTRY':
            extra = f"{e.data['cost_basis']:.9f}|{e.data['K1']}|{e.data['K2']}|{e.data['slot']}"
        elif e.kind == 'EXIT':
            t = e.data['trade']
            extra = f"{t['roi']:.9f}|{t['exit_reason']}|{t['m_exit']}"
        elif e.kind == 'TP1':
            extra = f"{e.data['model_roi']:.9f}"
        sig.append((d, e.kind, e.ticker, e.module, e.minute, extra))
    return sig


# ----------------------------------------------------------------------------------------------
def check_oracle_integrity(ref12, saved_ledger: Optional[str]) -> Check:
    bad = rr.check_hashes()
    detail = [f"reference files match pinned SHA-256: {'yes' if not bad else 'NO -> ' + str(bad)}"]
    ok = not bad
    data = {}
    if saved_ledger and os.path.exists(saved_ledger):
        saved = _sorted(pd.read_csv(saved_ledger))
        rerun = _sorted(ref12['df_exec'])
        cmp = compare_trades(saved, rerun)
        n_diff, worst = None, None
        if cmp['decisions_equal']:
            d = np.abs(saved['cost_basis'].values - rerun['cost_basis'].values)
            n_diff, worst = int((d > 1e-9).sum()), float(d.max())
        detail.append(f"original script re-run on this data vs the saved apex_chronos_final_trades.csv: "
                      f"{cmp['n_oracle']} vs {cmp['n_bot']} trades, every decision identical = {cmp['decisions_equal']}; "
                      f"{n_diff} rows differ in modelled option cost (max ${worst if worst is not None else float('nan'):.3f}/share) - "
                      f"the code is deterministic, so its inputs must have changed since the ledger was saved (cause not established; "
                      f"refreshed daily files are the likely one). The bot is compared with the fresh run, and the fresh run's statistics "
                      f"equal the published ones (see the table below)")
        data['saved_vs_rerun'] = {k: v for k, v in cmp.items() if k not in ('only_oracle', 'only_bot')}
        ok = ok and cmp['decisions_equal']
    return Check('A', 'Oracle integrity (the original code, unmodified)', ok, '; '.join(detail), data)


def check_features(ref, days_map, dts) -> Check:
    worst = {}
    n = 0
    for tkr, recs in ref['all_data'].items():
        for rec in recs:
            d = rec['date']
            td = days_map.get(d, {}).get(tkr)
            if td is None:
                continue
            f = F.compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
            pairs = {'vwap': (rec['vwap'], f.vwap), 'vwap_std': (rec['vwap_std'], f.vwap_std),
                     'noise_upper': (rec['noise_upper'], f.noise_upper), 'rvol_cum': (rec['rvol_cum'], f.rvol_cum),
                     'ema9': (rec['ema9'], f.ema9), 'ema21': (rec['ema21'], f.ema21), 'iv': (rec['iv_series'], f.iv),
                     'prev_c': (np.array([rec['prev_c']]), np.array([td.ctx.prev_c])),
                     'rvol20': (np.array([rec['rvol20']]), np.array([td.ctx.rvol20]))}
            for k, (x, y) in pairs.items():
                dd = float(np.max(np.abs(np.asarray(x, float) - np.asarray(y, float)))) if len(x) else 0.0
                worst[k] = max(worst.get(k, 0.0), dd)
            n += 1
    ok = all(v <= 1e-9 for v in worst.values())
    return Check('B', 'Feature parity (VWAP, EMA9/21, noise area, RVOL, IV, prior close, 20d vol)', ok,
                 f"{n} ticker-sessions compared; worst absolute difference per feature: " +
                 ', '.join(f"{k} {v:.1e}" for k, v in worst.items()), {'worst': worst, 'n': n})


def scheduler_invariants(df: pd.DataFrame) -> dict:
    viol = []
    for d, g in df.groupby('date'):
        g = g.sort_values('m_entry')
        for m in range(0, 400):
            live = ((g.m_entry <= m) & (g.m_exit >= m)).sum()
            if live > C.N_SLOTS:
                viol.append((d, m, int(live)))
                break
        for ph, lim in C.PHASE_LIMITS.items():
            if (g.phase == ph).sum() > lim:
                viol.append((d, ph, 'phase limit'))
        if g.duplicated(['ticker', 'phase']).any():
            viol.append((d, 'dup ticker-phase'))
        for s, gs in g.groupby('slot'):
            gs = gs.sort_values('m_entry')
            if (gs.m_entry.values[1:] <= gs.m_exit.values[:-1]).any():
                viol.append((d, 'slot overlap', int(s)))
    return {'violations': viol, 'ok': not viol}


def restart_equivalence(days_map, dts, tickers, cuts=(1, 8, 20, 40, 70, 100, 140, 200, 241, 300, 330, 380)) -> dict:
    def sig(e: SessionEngine):
        return (e.last_minute, tuple(sorted((p.ticker, p.spec.name, p.slot, p.m_entry, p.half_scaled, round(p.peak_roi, 12),
                                               round(p.dyn_sl, 12)) for p in e.open)),
                tuple(e.slot_free), tuple(sorted(e.p_counts.items())), tuple(sorted(e.used_tkr_phase)),
                tuple(sorted((k, tuple(v)) for k, v in e.pending.items())),
                tuple(sorted((t, tuple(dd.done for dd in dets)) for t, dets in e.dets.items())),
                len(e.trades))
    bad, n = [], 0
    for d in dts:
        day = days_map.get(d)
        if not day:
            continue
        nb = {t: len(td.C) for t, td in day.items()}
        full = {t: F.compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix) for t, td in day.items()}
        persistent = SessionEngine(d, tickers, n_bars=nb)
        sigs = {}
        usable = [c for c in cuts if c < min(nb.values())]
        for m in range(max(nb.values())):
            persistent.step(m, full)
            if m in usable:
                sigs[m] = sig(persistent)
        for cut in usable:
            k = cut + 1
            feats = {t: F.compute_features(td.ctx, td.O[:k], td.H[:k], td.L[:k], td.C[:k], td.V[:k], td.vix[:k])
                     for t, td in day.items() if len(td.C) >= k}
            cold = SessionEngine(d, tickers, n_bars=nb)
            for m in range(0, k):
                cold.step(m, feats)
            n += 1
            if sig(cold) != sigs[cut]:
                bad.append((d, cut))
    return {'n': n, 'bad': bad, 'ok': not bad}


# ---- mutations -----------------------------------------------------------------------------
@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def patched_item(d, key, value):
    old = d[key]
    d[key] = value
    try:
        yield
    finally:
        d[key] = old


def _exits_first_step():
    """The engine's own step() with its 'exits' block moved ahead of its 'entries' block (source-level mutation)."""
    src = textwrap.dedent(inspect.getsource(E.SessionEngine.step))
    i2, i3, ir = src.index('    # 2) schedule'), src.index('    # 3) exits'), src.rindex('    return events')
    mutant = src[:i2] + src[i3:ir] + src[i2:i3] + src[ir:]
    ns: dict = {}
    exec(compile(mutant, '<mutant:exits-before-entries>', 'exec'), E.__dict__, ns)
    return ns['step']


def mutation_catalog():
    """(label, context-manager factory, run kwargs, equivalence note or None).

    Each is a plausible porting / transcription error. `equivalence note` marks mutants that provably cannot
    change any outcome (so no data could ever distinguish them); the claim is checked, not assumed."""
    def spec(k, **kw):
        return patched_item(C.MODULES, k, dataclasses.replace(C.MODULES[k], **kw))

    def e1_lookahead():
        def step(self, m, f):
            if not self.in_window(m):
                return None
            if not hasattr(self, 'orb5_h'):
                self.orb5_h, self.orb5_l = np.max(f.H[:5]), np.min(f.L[:5])
            nxt = f.C[min(m + 1, f.n - 1)]            # peeks one bar into the future
            if self.gap > 0.0025 and nxt < self.orb5_l and f.C[m] < f.vwap[m] and f.C[m] < f.ema9[m] and f.C[m] > self.prev_c:
                return (m, 1)
            if self.gap < -0.0025 and nxt > self.orb5_h and f.C[m] > f.vwap[m] and f.C[m] > f.ema9[m] and f.C[m] < self.prev_c:
                return (m, -1)
            return None
        return patched(signals.E1GapTrap, 'step', step)

    def iv_stale():
        orig = F.compute_features

        def wrapped(ctx, *a, **k):
            f = orig(ctx, *a, **k)
            f.iv = np.concatenate([[f.iv[0]], f.iv[:-1]])      # implied vol one bar stale
            return f
        return patched(replay, 'compute_features', wrapped)

    def ema_span10():
        orig = F.compute_features

        def wrapped(ctx, O, H, L, C_, V, vix):
            f = orig(ctx, O, H, L, C_, V, vix)
            f.ema9 = pd.Series(np.asarray(C_, float)).ewm(span=10, adjust=False).mean().values
            return f
        return patched(replay, 'compute_features', wrapped)

    def rvol_baseline_7():
        orig = F.avg_profile
        return patched(F, 'avg_profile', lambda hist, k: orig(hist[-7:], k))   # 7-session baseline instead of 14

    @contextlib.contextmanager
    def exits_first():
        with patched(C, 'SLOT_REUSE_DELAY', 0), patched(E.SessionEngine, 'step', _exits_first_step()):
            yield

    nul = contextlib.nullcontext
    return [
        ('P1A RVOL cap 1.25 -> 1.40', lambda: patched(C, 'P1A_MAX_RVOL', 1.40), {}, None),
        ('P1A retrace cap 0.75 -> 0.90', lambda: patched(C, 'P1A_MAX_RETRACE', 0.90), {}, None),
        ('P1C RVOL ceiling 1.65 -> 1.40', lambda: patched(C, 'P1C_RVOL_MAX', 1.40), {}, None),
        ('P2 RVOL floor 1.00 -> 0.80', lambda: patched(C, 'P2_MIN_RVOL', 0.80), {}, None),
        ('Phase-1 daily limit 4 -> 3', lambda: patched_item(C.PHASE_LIMITS, 'P1', 3), {}, None),
        ('4 capital slots instead of 3', nul, {'engine_kwargs': {'n_slots': 4}}, None),
        ('slot reusable in the minute its trade exits (exits processed before entries)', exits_first, {}, None),
        ('trail 72% -> 60% of peak', lambda: patched(C, 'TRAIL_PCT_OF_PEAK', 0.60), {}, None),
        ('post-TP1 floor 60% -> 50% of the target', lambda: patched(C, 'AFTER_TP1_FLOOR_FRAC', 0.50), {},
         'after a TP1 touch the peak is >= the target, so the 72%-of-peak trail is always above the 60%-of-target floor; '
         'the floor can only bind if a bar touches the target to the last bit'),
        ('risk-free rate 4.5% -> 3%', lambda: patched(C, 'RISK_FREE', 0.03), {}, None),
        ('half-spread (liquid names) 0.5% -> 1.0%', lambda: patched(C, 'HALF_SPREAD_TIGHT', 0.01), {}, None),
        ('P2 stop-loss 20% -> 25%', lambda: spec('P2', sl_pct=0.25), {}, None),
        ('P3 take-profit 65% -> 60%', lambda: spec('P3', tp1_pct=0.60), {}, None),
        ('P1A max hold 50 -> 45 min', lambda: spec('P1A', max_hold_mins=45), {}, None),
        ('P1B score base 20 -> 10 (changes ranking)', lambda: spec('P1B', score_base=10.0), {}, None),
        ('E1 entry window ends at minute 20 instead of 60', lambda: patched(signals.E1GapTrap, 'hi', 20), {}, None),
        ('U5 window starts at minute 80 instead of 75', lambda: patched(signals.U5MiddayPin, 'lo', 80), {}, None),
        ('E1 peeks one bar ahead (look-ahead)', e1_lookahead, {}, None),
        ('EMA9 computed with span 10', ema_span10, {}, None),
        ('implied vol one bar stale', iv_stale, {}, None),
        ('RVOL baseline over 7 sessions instead of 14', rvol_baseline_7, {}, None),
    ]


def _differs(orc, df, cands) -> Optional[str]:
    tr, cd = compare_trades(orc['df_exec'], df), compare_candidates(orc['df_c'], cands)
    if tr['ok'] and cd['ok']:
        return None
    how = []
    if not cd['ok']:
        how.append(f"{len(cd['only_oracle']) + len(cd['only_bot'])} candidate differences")
    if not tr['ok']:
        how.append(f"{len(tr['only_oracle']) + len(tr['only_bot'])} trade differences" if (tr['only_oracle'] or tr['only_bot']) else 'numeric differences')
    return '; '.join(how)


def run_mutations(universes) -> List[dict]:
    """universes: [(name, oracle, tickers, days_cache)] tried in order; a mutant counts as detected at the first that exposes it."""
    out = []
    for label, factory, kw, equiv in mutation_catalog():
        r = {'mutation': label, 'caught_in': None, 'how': '', 'equivalent': equiv}
        for uname, orc, tk, cache in universes:
            try:
                with factory():
                    df, cands, _ = bot_run(None, tk, cache, **kw)
                d = _differs(orc, df, cands)
            except Exception as e:                       # a crash is also a detection
                d = f'crashed: {type(e).__name__}'
            if d:
                r['caught_in'], r['how'] = uname, d
                break
        out.append(r)
    return out


# ---- differential fuzzing --------------------------------------------------------------------
def tp1_margins(events) -> List[float]:
    tp = {v.name: v.tp1_pct for v in C.MODULES.values()}
    return [e.data['model_roi'] - tp[e.module] for _, e in events if e.kind == 'TP1']


def fuzz_case(args) -> dict:
    seed, src, tickers, survivors, do_livepath = args
    tmp = tempfile.mkdtemp(prefix=f'apex_fuzz{seed}_')
    try:
        fuzz.make_fuzz_dataset(src, tmp, seed, tickers)
        ref = rr.run_reference(tmp, tickers)
        cache = replay.build_days(tmp, tickers)
        df, cands, ev = bot_run(tmp, tickers, cache, mode='fast')
        tr, cd = compare_trades(ref['df_exec'], df), compare_candidates(ref['df_c'], cands)
        inv = scheduler_invariants(df)
        res = {'seed': seed, 'ok': tr['ok'] and cd['ok'] and inv['ok'], 'n_trades': tr['n_oracle'], 'n_cands': cd['n_oracle'],
               'trade_detail': {k: v for k, v in tr.items() if k in ('only_oracle', 'only_bot', 'max_abs_diff')},
               'by_module': ref['df_exec'].groupby('module').size().to_dict() if len(ref['df_exec']) else {},
               'by_reason': ref['df_exec'].exit_reason.value_counts().to_dict() if len(ref['df_exec']) else {},
               'tp1_margins': tp1_margins(ev), 'invariants_ok': inv['ok'], 'livepath_ok': None, 'detected': {}}
        if do_livepath:
            dfl, cl, evl = bot_run(tmp, tickers, cache, mode='livepath')
            res['livepath_ok'] = compare_trades(ref['df_exec'], dfl)['ok'] and compare_candidates(ref['df_c'], cl)['ok'] \
                and event_signature(ev) == event_signature(evl)
        cat = {lab: (f, kw) for lab, f, kw, _ in mutation_catalog()}
        for lab in survivors:
            f, kw = cat[lab]
            try:
                with f():
                    d2, c2, _ = bot_run(tmp, tickers, cache, **kw)
                res['detected'][lab] = _differs(ref, d2, c2)
            except Exception as e:
                res['detected'][lab] = f'crashed: {type(e).__name__}'
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_fuzz(src_dir, tickers, seeds, survivors, workers=2) -> List[dict]:
    jobs = [(s, src_dir, tickers, survivors, s < 2) for s in seeds]
    ctx = mp.get_context('fork')
    with ctx.Pool(workers) as pool:
        return pool.map(fuzz_case, jobs, chunksize=1)


# ---- sensitivities ----------------------------------------------------------------------------
def diff_vs_oracle(base: pd.DataFrame, other: pd.DataFrame) -> dict:
    ix = ['date', 'ticker', 'module']
    a = base.set_index(ix)
    b = other.set_index(ix)
    common = a.index.intersection(b.index)
    ac, bc = a.loc[common], b.loc[common]
    return {'n_base': len(a), 'n_other': len(b), 'missing': len(a.index.difference(b.index)), 'new': len(b.index.difference(a.index)),
            'entry_min_changed': int((ac['m_entry'] != bc['m_entry']).sum()),
            'exit_changed': int(((ac['m_exit'] != bc['m_exit']) | (ac['exit_reason'] != bc['exit_reason'])).sum()),
            'roi_changed': int((np.abs(ac['roi'] - bc['roi']) > 1e-9).sum()),
            'mean_abs_roi_diff': float(np.abs(ac['roi'] - bc['roi']).mean()) if len(common) else 0.0}


def md_table(rows: List[List[str]], header: List[str]) -> str:
    out = ['| ' + ' | '.join(header) + ' |', '|' + '|'.join(['---'] * len(header)) + '|']
    out += ['| ' + ' | '.join(str(c).replace('|', '/') for c in r) + ' |' for r in rows]
    return '\n'.join(out)


# ----------------------------------------------------------------------------------------------
def run(args) -> int:
    t0 = time.time()
    checks: List[Check] = []
    data_dir = os.path.abspath(args.data_dir)
    if data_dir.endswith(('.tar.gz', '.tgz')):
        from verification.fixtures import extract
        data_dir = extract(data_dir)
    have24 = all(os.path.exists(os.path.join(data_dir, '1m', f'{t}.csv')) for t in EXT24)
    if not have24:
        args.skip_extended = True
    fuzz_tickers = EXT24 if have24 else CORE12
    print('running the ORIGINAL backtest code (12 tickers) ...')
    ref12 = rr.run_reference(data_dir, CORE12)
    ref24 = None
    if not args.skip_extended:
        print('running the ORIGINAL backtest code (24 tickers) ...')
        ref24 = rr.run_reference(data_dir, EXT24)

    print('loading recorded sessions ...')
    dts, days12 = replay.build_days(data_dir, CORE12)
    cache12 = (dts, days12)

    checks.append(check_oracle_integrity(ref12, args.saved_ledger))
    checks.append(check_features(ref12, days12, dts))

    print('bot (fast) vs oracle, 12 tickers ...')
    df12, cands12, ev_fast = bot_run(data_dir, CORE12, cache12, mode='fast')
    cc = compare_candidates(ref12['df_c'], cands12)
    checks.append(Check('C', 'Candidate parity (every signal the original generated, before slot scheduling)', cc['ok'],
                        f"{cc['n_oracle']} oracle candidates vs {cc['n_bot']} bot candidates; missing in bot {len(cc['only_oracle'])}, "
                        f"extra in bot {len(cc['only_bot'])}; score diff {cc.get('max_abs_diff_score', float('nan')):.1e}", cc))
    td = compare_trades(ref12['df_exec'], df12)
    checks.append(Check('D', 'Trade-for-trade parity, 12-ticker portfolio (the 110-trade ledger)', td['ok'],
                        f"{td['n_oracle']} vs {td['n_bot']} trades; ticker/date/module/direction/entry minute/exit minute/exit reason/slot "
                        f"identical = {td['decisions_equal']}; max |diff|: " +
                        ', '.join(f"{k} {v:.1e}" for k, v in td['max_abs_diff'].items()), td))

    days24, cache24, ev24 = None, None, []
    if ref24 is not None:
        print('bot (fast) vs oracle, 24 tickers ...')
        dts24, days24 = replay.build_days(data_dir, EXT24)
        cache24 = (dts24, days24)
        df24, cands24, ev24 = bot_run(data_dir, EXT24, cache24, mode='fast')
        cc24 = compare_candidates(ref24['df_c'], cands24)
        td24 = compare_trades(ref24['df_exec'], df24)
        checks.append(Check('E', 'Extended universe: 24 tickers (more candidates, scheduler under pressure)', cc24['ok'] and td24['ok'],
                            f"candidates {cc24['n_oracle']} vs {cc24['n_bot']} (identical sets: {not cc24['only_oracle'] and not cc24['only_bot']}); "
                            f"trades {td24['n_oracle']} vs {td24['n_bot']} (decisions identical: {td24['decisions_equal']}); max |diff| "
                            + ', '.join(f"{k} {v:.1e}" for k, v in td24['max_abs_diff'].items()), {'cands': cc24, 'trades': td24}))

    print('live-path replay (features rebuilt every minute from bars seen so far) ...')
    t1 = time.time()
    df_lp, cands_lp, ev_lp = bot_run(data_dir, CORE12, cache12, mode='livepath')
    tlp = compare_trades(ref12['df_exec'], df_lp)
    same_events = event_signature(ev_fast) == event_signature(ev_lp)
    clp = compare_candidates(ref12['df_c'], cands_lp)
    f_ok = tlp['ok'] and clp['ok'] and same_events
    f24 = ''
    if ref24 is not None and not args.skip_livepath24:
        print('live-path replay, 24 tickers ...')
        df_lp24, _, _ = bot_run(data_dir, EXT24, cache24, mode='livepath')
        t24 = compare_trades(ref24['df_exec'], df_lp24)
        f_ok = f_ok and t24['ok']
        f24 = f"; 24-ticker live-path: {t24['n_bot']} trades, identical to oracle = {t24['ok']}"
    checks.append(Check('F', 'Live-path replay: each minute sees ONLY bars up to that minute (no look-ahead)', f_ok,
                        f"{tlp['n_bot']} trades identical to oracle = {tlp['ok']}; candidate sets identical = {clp['ok']}; "
                        f"full event stream (every ENTRY/TP1/EXIT with its minute) identical to the all-day-data run = {same_events}{f24}; "
                        f"{time.time() - t1:.0f}s", {'trades': tlp, 'events_equal': same_events}))

    print('poll-cadence invariance ...')
    gres = {}
    for b in (3, 7, 15, 45):
        dfb, _, _ = bot_run(data_dir, CORE12, cache12, mode='burst', burst=b)
        gres[b] = compare_trades(ref12['df_exec'], dfb)['ok']
    checks.append(Check('G', 'Poll-cadence invariance (a poll that arrives late and catches up several minutes at once)', all(gres.values()),
                        'burst sizes ' + ', '.join(f"{b} min: {'identical' if ok else 'DIFFERENT'}" for b, ok in gres.items()), gres))

    print('restart equivalence ...')
    rs = restart_equivalence(days12, dts, CORE12)
    checks.append(Check('H', 'Restart equivalence (cold start + catch-up == ran all day)', rs['ok'],
                        f"{rs['n']} cold-start comparisons at 12 cut points x {len(dts)} sessions; mismatches: {len(rs['bad'])}", rs))

    inv_o = scheduler_invariants(ref12['df_exec'])
    inv_b = scheduler_invariants(df12)
    inv_b24 = scheduler_invariants(df24) if ref24 is not None else {'ok': True, 'violations': []}
    checks.append(Check('I', 'Scheduler invariants (3 slots, phase limits 4/2/1, one trade per ticker-phase, no slot overlap)',
                        inv_b['ok'] and inv_b24['ok'],
                        f"bot 12-ticker violations {len(inv_b['violations'])}, 24-ticker violations {len(inv_b24['violations'])} "
                        f"(oracle itself: {len(inv_o['violations'])})", {'bot12': inv_b, 'bot24': inv_b24, 'oracle12': inv_o}))

    # mutation ladder, stage 1: the real data (12 tickers, then 24)
    mu = None
    survivors: List[str] = []
    if not args.skip_mutations:
        print('mutation testing on the recorded data ...')
        unis = [('recorded data, 12 tickers', ref12, CORE12, cache12)]
        if ref24 is not None:
            unis.append(('recorded data, 24 tickers', ref24, EXT24, cache24))
        mu = run_mutations(unis)
        survivors = [r['mutation'] for r in mu if r['caught_in'] is None and not r['equivalent']]

    # differential fuzzing (also gives the mutation survivors a bigger, more varied market to be caught in)
    fz = None
    if args.fuzz_seeds > 0:
        print(f'differential fuzzing: {args.fuzz_seeds} perturbed markets x {len(fuzz_tickers)} tickers (survivors to hunt: {len(survivors)}) ...')
        t2 = time.time()
        fz = run_fuzz(data_dir, fuzz_tickers, list(range(args.fuzz_seeds)), survivors, workers=args.workers)
        bad = [r['seed'] for r in fz if not r['ok']]
        lp = [r for r in fz if r['livepath_ok'] is not None]
        tot_t, tot_c = sum(r['n_trades'] for r in fz), sum(r['n_cands'] for r in fz)
        bm, br = {}, {}
        for r in fz:
            for k, v in r['by_module'].items():
                bm[k] = bm.get(k, 0) + v
            for k, v in r['by_reason'].items():
                br[k] = br.get(k, 0) + v
        checks.append(Check('J', 'Differential fuzzing: original vs bot on perturbed markets (prices, gaps, volume, VIX)',
                            not bad and all(r['livepath_ok'] for r in lp),
                            f"{len(fz)} different markets x {len(fuzz_tickers)} tickers x {len(dts)} sessions: {tot_c} candidates and {tot_t} scheduled trades compared, "
                            f"seeds with ANY difference: {bad or 'none'}; live-path (no look-ahead) re-check on {len(lp)} of them: "
                            f"{'identical' if all(r['livepath_ok'] for r in lp) else 'DIFFERENT'}; exits {br}; per module {bm}; {time.time() - t2:.0f}s",
                            {'seeds': [{k: v for k, v in r.items() if k != 'tp1_margins'} for r in fz]}))
        if mu is not None:
            for r in mu:
                if r['caught_in'] is None and not r['equivalent']:
                    for z in fz:
                        d = z['detected'].get(r['mutation'])
                        if d:
                            r['caught_in'], r['how'] = f"fuzzed market #{z['seed']}", d
                            break

    if mu is not None:
        margins = tp1_margins(ev_fast) + tp1_margins(ev24) + [m for z in (fz or []) for m in z['tp1_margins']]
        minmargin = min(margins) if margins else float('nan')
        missed = [r for r in mu if r['caught_in'] is None and not r['equivalent']]
        eq = [r for r in mu if r['equivalent']]
        eq_ok = all(minmargin > 0 for _ in eq) if eq else True
        eq_undetected = all(r['caught_in'] is None for r in eq)
        n_real = len([r for r in mu if not r['equivalent']])
        checks.append(Check('K', 'Mutation power: deliberately broken bots must be detected', not missed and eq_ok,
                            f"{n_real - len(missed)}/{n_real} injected faults detected"
                            + (f"; NOT detected: {[r['mutation'] for r in missed]}" if missed else '')
                            + (f"; {len(eq)} provably-equivalent fault(s) excluded (smallest TP1 overshoot seen in {len(margins)} touches: {minmargin * 100:.4f} "
                               f"ROI points above the target, never exactly on it)" if eq else ''), {'results': mu, 'tp1_touches': len(margins), 'min_margin': minmargin}))

    # live-vs-backtest gaps
    print('live-vs-backtest sensitivities ...')
    base = stats(ref12['df_exec'])
    scen = []
    dts_g, days_g = replay.build_days(data_dir, CORE12, grid=True)
    df_g, _, _ = bot_run(data_dir, CORE12, (dts_g, days_g), mode='fast')
    scen.append(('Live loader\'s clock-grid indexing (holes forward-filled) instead of the original\'s row indexing', stats(df_g),
                 diff_vs_oracle(ref12['df_exec'], df_g)))
    for lag in (5, 15):
        dts_l, days_l = replay.build_days(data_dir, CORE12, vix_lag_minutes=lag)
        df_l, _, _ = bot_run(data_dir, CORE12, (dts_l, days_l), mode='fast')
        scen.append((f'VIX delayed {lag} min (Yahoo\'s ^VIX runs ~15 min behind the equities)', stats(df_l),
                     diff_vs_oracle(ref12['df_exec'], df_l)))
    dts_b, days_b = replay.build_days(data_dir, CORE12, baseline='5m')
    df_b, _, _ = bot_run(data_dir, CORE12, (dts_b, days_b), mode='fast')
    scen.append(('Volume/noise baselines rebuilt from 5-minute bars for every session (the original used 5-minute data only for its first 14 sessions; live always has real 1-minute history)',
                 stats(df_b), diff_vs_oracle(ref12['df_exec'], df_b)))
    checks.append(Check('L', 'Live-vs-backtest gaps measured on the same data (informational, not pass/fail)', None,
                        '; '.join(f"{n}: {d['missing']} of {d['n_base']} trades lost, {d['new']} new, {d['entry_min_changed']} entry minutes moved, "
                                  f"{d['exit_changed']} exits changed, mean |ROI change| {d['mean_abs_roi_diff'] * 100:.2f} pts" for n, _, d in scen),
                        {'baseline': base, 'scenarios': [{'name': n, 'stats': s_, 'diff': d} for n, s_, d in scen]}))

    # ---- report -------------------------------------------------------------------------
    order = {c: i for i, c in enumerate('ABCDEFGHIJKL')}
    checks.sort(key=lambda c: order[c.id])
    verdict_pass = all(c.passed for c in checks if c.passed is not None)
    lines = ['# APEX-CHRONOS bot: verification against the backtest', '',
             f"**Result: {'PASS' if verdict_pass else 'FAIL'}** - the bot "
             f"{'reproduces the original backtest trade-for-trade' if verdict_pass else 'does NOT reproduce the original backtest (see failing checks)'}.",
             f"Recorded data: {len(dts)} sessions {dts[0]} .. {dts[-1]} · numpy {np.__version__}, pandas {pd.__version__} · "
             f"python {sys.version.split()[0]} · {time.time() - t0:.0f}s",
             '',
             md_table([[c.id, c.name, {True: 'PASS', False: '**FAIL**', None: 'info'}[c.passed], c.detail] for c in checks],
                      ['#', 'Check', 'Result', 'Detail']), '']
    if mu is not None:
        lines += ['## Mutation results (does the verification have teeth?)', '',
                  md_table([[r['mutation'],
                             ('equivalent - cannot change an outcome' if r['equivalent'] and r['caught_in'] is None else
                              (f"detected ({r['caught_in']})" if r['caught_in'] else '**NOT detected**')),
                             r['how'] or (r['equivalent'] or '')] for r in mu],
                           ['Injected fault', 'Verdict', 'How / why']), '']
    k = next(c for c in checks if c.id == 'L')
    lines += ['## Live-vs-backtest gaps (same data, bot engine)', '',
              md_table([['Backtest as published (oracle)', base['n'], f"{base['win_rate'] * 100:.1f}%", f"{base['profit_factor']:.2f}",
                         f"{base['mean_roi'] * 100:+.2f}%", '-']] +
                       [[s_['name'], s_['stats']['n'], f"{s_['stats']['win_rate'] * 100:.1f}%", f"{s_['stats']['profit_factor']:.2f}",
                         f"{s_['stats']['mean_roi'] * 100:+.2f}%",
                         f"{s_['diff']['missing']} trades lost, {s_['diff']['new']} new, {s_['diff']['entry_min_changed']} entry minutes moved, "
                         f"{s_['diff']['exit_changed']} exits changed"] for s_ in k.data['scenarios']],
                       ['Scenario', 'Trades', 'Win rate', 'Profit factor', 'Mean ROI/trade', 'Difference from the backtest']), '']
    lines += ['## What this proves, and what it does not', '',
              '**Proven (against the original code, bit for bit):** given the same one-minute bars, VIX and history, the bot makes the same '
              'entries, picks the same options and strikes, ranks and schedules them the same way, and takes the same exits at the same minutes '
              'with the same model P&L. It does so using only bars that have already closed, and it gives the same answer whether it is polled every '
              'minute, polls late and catches up, or restarts mid-session.', '',
              '**Not proven by this report:**',
              '- that the backtest result itself is a durable edge. It is 21 sessions, one regime, with thresholds fitted on the same data;',
              '- that Black-Scholes model prices resemble the real bid/ask you will be quoted. The backtest never used real option quotes;',
              '- that a same-day expiry exists for the strike and ticker in an alert (the backtest assumed 0DTE for every name);',
              '- the live data path end to end (Yahoo availability, bar timing, GitHub scheduling). The EOD reconcile job measures that every day instead;',
              '- anything about fills, slippage, fees or pattern-day-trader limits, none of which are in the simulator.', '']
    report = '\n'.join(lines)
    here = os.path.abspath(args.out_dir) if args.out_dir else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(here, exist_ok=True)
    with open(os.path.join(here, 'VERIFICATION_REPORT.md'), 'w') as fh:
        fh.write(report + '\n')
    with open(os.path.join(here, 'results.json'), 'w') as fh:
        json.dump([dataclasses.asdict(c) for c in checks], fh, indent=1, default=str)
    print('\n' + report)
    return 0 if verdict_pass else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', required=True, help='folder with 1m/ 5m/ 1d/ CSVs, or a fixtures .tar.gz')
    ap.add_argument('--saved-ledger', default=None, help="the original apex_chronos_final_trades.csv, if you have it")
    ap.add_argument('--skip-extended', action='store_true', help='skip the 24-ticker cross-check')
    ap.add_argument('--skip-livepath24', action='store_true')
    ap.add_argument('--skip-mutations', action='store_true')
    ap.add_argument('--fuzz-seeds', type=int, default=20, help='number of perturbed markets (0 = skip fuzzing)')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--out-dir', default=None, help='where to write VERIFICATION_REPORT.md and results.json (default: this folder)')
    return run(ap.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
