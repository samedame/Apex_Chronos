"""Q3: execution stress on real bars. S1 = wider half-spreads (full re-run), S2 = delayed fills (strikes fixed at the signal).

S2 detail. The alert is built at the signal bar with strikes chosen then. A trader who fills `k` minutes later pays the ask for THOSE
strikes at the then-current spot / IV / time, and the stop, targets and trail are re-anchored to the delayed cost. The time stop stays
anchored to the signal. Slot scheduling is held fixed (each trade independent). k = 0 must reproduce the engine's ROI to 1e-9 (asserted).
"""
from __future__ import annotations

import contextlib
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from apex_chronos import config as C, replay                                  # noqa: E402
from apex_chronos.features import compute_features                            # noqa: E402
from apex_chronos.position import Position                                    # noqa: E402
from apex_chronos.pricing import bs_option_price_and_delta, half_spread_for, minutes_to_T   # noqa: E402

B = 5000
SEED = 20261028
SPREAD_MULTS = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
DELAYS = [0, 1, 2, 3, 5, 10]


@contextlib.contextmanager
def spread_mult(k: float):
    t0, w0 = C.HALF_SPREAD_TIGHT, C.HALF_SPREAD_WIDE
    C.HALF_SPREAD_TIGHT, C.HALF_SPREAD_WIDE = t0 * k, w0 * k
    try:
        yield
    finally:
        C.HALF_SPREAD_TIGHT, C.HALF_SPREAD_WIDE = t0, w0


def run_all_sessions(sets, k=1.0):
    """-> (trades df with 'sample', events [(sample, date, Event)]) for every session in both samples at spread multiple k."""
    trades, events = [], []
    with spread_mult(k):
        for sample, (dates, days) in sets.items():
            for d in dates:
                if d not in days:
                    continue
                eng, ev = replay.run_session(d, days[d], list(C.TICKERS), mode='fast')
                for t in eng.trades:
                    trades.append({**t, 'sample': sample})
                events += [(sample, d, e) for e in ev]
    return pd.DataFrame(trades), events


# ----------------------------------------------------------------------------------------------------- metrics
def metrics(roi):
    r = np.asarray(roi, dtype=float)
    if len(r) == 0:
        return {'n': 0}
    w, l = r[r > 0].sum(), -r[r <= 0].sum()
    return {'n': int(len(r)), 'win': float((r > 0).mean()), 'mean_roi': float(r.mean()), 'median_roi': float(np.median(r)),
            'profit_factor': float(w / l) if l > 0 else float('inf')}


def block_ci_mean(roi, dates, b=B, seed=SEED):
    r = np.asarray(roi, dtype=float)
    d = np.asarray(dates)
    groups = [np.flatnonzero(d == x) for x in sorted(set(d))]
    rng = np.random.default_rng(seed)
    k = len(groups)
    vals = []
    for _ in range(b):
        ix = np.concatenate([groups[i] for i in rng.integers(0, k, k)])
        vals.append(r[ix].mean())
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


# ------------------------------------------------------------------------------------------------------------- S1
def s1(sets):
    out = {}
    for k in SPREAD_MULTS:
        tr, _ = run_all_sessions(sets, k)
        row = {'all': metrics(tr['roi'])}
        row['all']['mean_roi_ci95'] = block_ci_mean(tr['roi'], tr['date'])
        for s in ('backtest', 'forward'):
            g = tr[tr['sample'] == s]
            row[s] = metrics(g['roi'])
            row[s]['mean_roi_ci95'] = block_ci_mean(g['roi'], g['date'])
        row['by_module'] = {m: {'n': int(len(g)), 'mean_roi': float(g['roi'].mean()), 'win': float((g['roi'] > 0).mean())}
                            for m, g in tr.groupby('module')}
        out[str(k)] = row
    return out


# ------------------------------------------------------------------------------------------------------------- S2
def _entry_cost(pos: Position, S, T, iv, hs):
    st = pos.struct
    r = C.RISK_FREE
    if st.structure == 'single':
        mid, _ = bs_option_price_and_delta(S, st.K1, T, r, iv, st.is_call)
        return max(float(mid) * (1.0 + hs) + 0.01, 0.15), None
    if st.structure == 'debit_spread':
        p1, _ = bs_option_price_and_delta(S, st.K1, T, r, iv, st.is_call)
        p2, _ = bs_option_price_and_delta(S, st.K2, T, r, iv * 1.01, st.is_call)
        return max(float(p1 - p2) * (1.0 + hs) + 0.015, 0.20), None
    opp = not st.is_call
    p1, _ = bs_option_price_and_delta(S, st.K1, T, r, iv, opp)
    p2, _ = bs_option_price_and_delta(S, st.K2, T, r, iv * 1.02, opp)
    credit = max(float(p1 - p2) * (1.0 - hs) - 0.015, 0.12)
    return max(st.width - credit, 0.25), credit


def delayed_roi(ev_data, spec, key, ticker, date, m_sig, td, f, k):
    """ROI of one trade filled k minutes after the signal; NaN if the delayed fill falls past the time stop."""
    pos = Position(spec, key, ticker, date, m_sig, ev_data['direction'], ev_data['S_entry'], ev_data['iv_entry'],
                   len(td.C), ev_data['score'], ev_data['rvol_entry'], ev_data['slot'])
    m_fill = m_sig + k
    if m_fill >= pos.m_end:
        return float('nan')
    if k > 0:
        cost, credit = _entry_cost(pos, float(f.C[m_fill]), minutes_to_T(m_fill), float(f.iv[m_fill]), half_spread_for(ticker))
        pos.struct.cost_basis = cost
        if credit is not None:
            pos.struct.credit = credit
        pos.cost_basis = cost
        pos.m_entry = m_fill
    for m in range(m_fill + 1, len(td.C)):
        _, closed = pos.on_bar(m, float(f.H[m]), float(f.L[m]), float(f.C[m]), float(f.iv[m]))
        if closed is not None:
            return float(closed['roi'])
    return float('nan')


def s2(sets, mult=1.0):
    """Delay sweep at a given spread multiple, over the baseline trades (scheduling fixed)."""
    feats_cache = {}
    rows = []
    with spread_mult(mult):
        _, events = run_all_sessions(sets, mult)
        for sample, d, e in events:
            if e.kind != 'ENTRY':
                continue
            td = sets[sample][1][d][e.ticker]
            fk = (d, e.ticker)
            if fk not in feats_cache:
                feats_cache[fk] = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
            f = feats_cache[fk]
            key = e.data['module_key']
            spec = C.MODULES[key]
            rec = {'sample': sample, 'date': d, 'ticker': e.ticker, 'module': key, 'm_sig': e.minute,
                   'tclass': 'index' if e.ticker in C.INDEX_0DTE else 'other'}
            for k in DELAYS:
                rec[f'd{k}'] = delayed_roi(e.data, spec, key, e.ticker, d, e.minute, td, f, k)
            rows.append(rec)
    return pd.DataFrame(rows)


def s2_summary(df, tr_engine=None):
    cols = [f'd{k}' for k in DELAYS]
    res = {'n_trades': int(len(df)), 'dropped_past_time_stop': {c: int(df[c].isna().sum()) for c in cols}}
    sub = df.dropna(subset=cols)         # same trades at every delay so the comparison is like-for-like
    res['n_like_for_like'] = int(len(sub))
    res['by_delay'] = {}
    for k in DELAYS:
        c = f'd{k}'
        m = metrics(sub[c])
        m['mean_roi_ci95'] = block_ci_mean(sub[c], sub['date'])
        res['by_delay'][str(k)] = m
    # slope over 0..5 minutes, session-block bootstrap
    ks = np.array([0, 1, 2, 3, 5], dtype=float)
    cs = [f'd{int(k)}' for k in ks]
    groups = [np.flatnonzero((sub['date'] == x).to_numpy()) for x in sorted(sub['date'].unique())]
    arr = sub[cs].to_numpy()

    def slope(ix):
        mu = arr[ix].mean(axis=0)
        return float(np.polyfit(ks, mu, 1)[0])
    rng = np.random.default_rng(SEED)
    vals = [slope(np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])) for _ in range(B)]
    res['slope_pts_per_min'] = {'point': slope(np.arange(len(sub))) * 100, 'ci95': [float(np.percentile(vals, 2.5)) * 100, float(np.percentile(vals, 97.5)) * 100]}
    res['by_module_cost_at'] = {}
    for k in (2, 5):
        res['by_module_cost_at'][str(k)] = {m: {'n': int(len(g)), 'mean_d0': float(g['d0'].mean()), f'mean_d{k}': float(g[f'd{k}'].mean()),
                                                'cost_pts': float((g['d0'] - g[f'd{k}']).mean() * 100)} for m, g in sub.groupby('module')}
        res['by_module_cost_at'][str(k)].update({'class:' + m: {'n': int(len(g)), 'cost_pts': float((g['d0'] - g[f'd{k}']).mean() * 100)}
                                                 for m, g in sub.groupby('tclass')})
    return res


def check_k0(df_s2, tr_engine):
    """Delay 0 must equal the engine's own ROI trade for trade."""
    a = df_s2.set_index(['sample', 'date', 'ticker', 'module', 'm_sig'])['d0']
    b = tr_engine.assign(module=tr_engine['module'].map({s.name: k for k, s in C.MODULES.items()})).set_index(
        ['sample', 'date', 'ticker', 'module', 'm_entry'])['roi']
    b.index.names = a.index.names
    j = a.to_frame('mine').join(b.rename('engine'), how='inner')
    return {'matched': int(len(j)), 'of': int(len(a)), 'max_abs_diff': float((j['mine'] - j['engine']).abs().max())}


if __name__ == '__main__':
    import json
    import dataset
    sets = dataset.day_sets()
    tr1, _ = run_all_sessions(sets, 1.0)
    out = {'baseline': metrics(tr1['roi'])}
    print('baseline', out['baseline'])
    d2 = s2(sets, 1.0)
    out['k0_check'] = check_k0(d2, tr1)
    print('k0 check', out['k0_check'])
    assert out['k0_check']['max_abs_diff'] < 1e-9, 'delay-0 fill does not reproduce the engine'
    out['s2_delay_at_1x_spread'] = s2_summary(d2)
    d2.to_csv(os.path.join(HERE, 'data', 's2_delay_trades.csv'), index=False)
    print(json.dumps(out['s2_delay_at_1x_spread'], indent=1, default=float)[:3500])
    out['s1_spread'] = s1(sets)
    for k, v in out['s1_spread'].items():
        print(f"spread x{k}: n={v['all']['n']} win={v['all']['win']:.3f} mean={v['all']['mean_roi']*100:+.2f}% "
              f"CI[{v['all']['mean_roi_ci95'][0]*100:+.1f},{v['all']['mean_roi_ci95'][1]*100:+.1f}] PF={v['all']['profit_factor']:.2f} | "
              f"bt {v['backtest']['mean_roi']*100:+.2f}% fw {v['forward']['mean_roi']*100:+.2f}%")
    # combined scenario
    d3 = s2(sets, 2.0)
    out['s3_2x_spread_delay'] = s2_summary(d3)
    with open(os.path.join(HERE, 'results_q3.json'), 'w') as fh:
        json.dump(out, fh, indent=1, default=float)
    print('saved results_q3.json')
