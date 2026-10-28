"""Q1 (does the shipped score predict outcomes?) and Q2 (does any alert-time factor?) exactly as PREREGISTRATION.md specifies.

Session-block bootstrap everywhere: sessions are resampled with replacement, all trades of a drawn session come along.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm, rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

B = 10_000
SEED = 20261028
FACTORS = ['tclass_index', 'direction', 'm_entry', 'rvol_entry', 'vix', 'abs_gap', 'gap_dir', 'day_move_dir',
           'vwap_z_dir', 'rvol20', 'nth_of_day']


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['tclass_index'] = (df['tclass'] == 'index').astype(float)
    return df


# --------------------------------------------------------------------------------------------- bootstrap plumbing
def session_index(df: pd.DataFrame):
    return [np.flatnonzero((df['date'] == d).to_numpy()) for d in sorted(df['date'].unique())]


def draws(groups, b=B, seed=SEED):
    rng = np.random.default_rng(seed)
    k = len(groups)
    for _ in range(b):
        pick = rng.integers(0, k, k)
        yield np.concatenate([groups[i] for i in pick])


def auc(score, y):
    y = np.asarray(y)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return np.nan
    r = rankdata(score)
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def spearman(a, b):
    if np.ptp(a) == 0 or np.ptp(b) == 0:
        return np.nan
    return float(np.corrcoef(rankdata(a), rankdata(b))[0, 1])


def pct_rank_within(values, mod):
    out = np.empty(len(values))
    for m in np.unique(mod):
        idx = np.flatnonzero(mod == m)
        out[idx] = (rankdata(values[idx]) - 0.5) / len(idx)
    return out


def within_module_rho(x, roi, mod):
    rx, rr = pct_rank_within(x, mod), pct_rank_within(roi, mod)
    if rx.std() == 0 or rr.std() == 0:
        return np.nan
    return float(np.corrcoef(rx, rr)[0, 1])


def ci(vals, lo=2.5, hi=97.5):
    v = np.asarray(vals, dtype=float)
    v = v[~np.isnan(v)]
    return [float(np.percentile(v, lo)), float(np.percentile(v, hi))] if len(v) else [np.nan, np.nan]


def boot_stat(df, fn, b=B):
    g = session_index(df)
    pt = fn(df)
    vals = [fn(df.iloc[ix]) for ix in draws(g, b)]
    return pt, ci(vals), np.asarray(vals, dtype=float)


def p_two_sided(vals):
    v = np.asarray(vals, dtype=float)
    v = v[~np.isnan(v)]
    return float(min(1.0, 2 * min((np.sum(v <= 0) + 1) / (len(v) + 1), (np.sum(v >= 0) + 1) / (len(v) + 1))))


def p_one_sided(vals, sign):
    v = np.asarray(vals, dtype=float)
    v = v[~np.isnan(v)]
    return float(((np.sum(v <= 0) if sign > 0 else np.sum(v >= 0)) + 1) / (len(v) + 1))


def holm(ps):
    order = np.argsort(ps)
    m = len(ps)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, (m - rank) * ps[i])
        adj[i] = min(1.0, run)
    return adj


# ------------------------------------------------------------------------------------------------------ power
def icc(df, col):
    g = df.groupby('date')[col]
    k = g.ngroups
    n = len(df)
    n_i = g.size().to_numpy()
    n0 = (n - (n_i ** 2).sum() / n) / (k - 1)
    ssb = (n_i * (g.mean().to_numpy() - df[col].mean()) ** 2).sum()
    ssw = ((df[col] - g.transform('mean')) ** 2).sum()
    msb, msw = ssb / (k - 1), ssw / (n - k)
    return float(max(0.0, (msb - msw) / (msb + (n0 - 1) * msw)))


def min_detectable_auc(df, alpha=0.05, power=0.80):
    n1, n0 = int(df['win'].sum()), int((1 - df['win']).sum())
    deff = 1 + (len(df) / df['date'].nunique() - 1) * icc(df, 'win')
    z = norm.ppf(1 - alpha / 2) + norm.ppf(power)

    def se(a):
        q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
        return np.sqrt((a * (1 - a) + (n1 - 1) * (q1 - a * a) + (n0 - 1) * (q2 - a * a)) / (n1 * n0))
    a = 0.6
    for _ in range(50):
        a = 0.5 + z * se(a) * np.sqrt(deff)
    return {'n': len(df), 'wins': n1, 'losses': n0, 'sessions': int(df['date'].nunique()),
            'win_icc': round(icc(df, 'win'), 3), 'design_effect': round(deff, 2), 'min_detectable_auc': round(float(min(a, 0.99)), 3)}


# -------------------------------------------------------------------------------------------------------- Q1
def tier_of(score):
    return 'A' if score >= 85 else ('B' if score >= 70 else ('C' if score >= 50 else 'D'))


def q1(df):
    out = {}
    for name, d in (('backtest', df[df['sample'] == 'backtest']), ('forward', df[df['sample'] == 'forward']), ('pooled', df)):
        res = {}
        for label, col in (('headline', 'score_ontime'), ('setup_part', 'setup_part'), ('execution_part', 'exec_part')):
            a, a_ci, _ = boot_stat(d, lambda x, c=col: auc(x[c].to_numpy(), x['win'].to_numpy()))
            r, r_ci, _ = boot_stat(d, lambda x, c=col: spearman(x[c].to_numpy(), x['roi'].to_numpy()))
            res[label] = {'auc_win': round(a, 3), 'auc_ci95': [round(v, 3) for v in a_ci],
                          'spearman_roi': round(r, 3), 'spearman_ci95': [round(v, 3) for v in r_ci],
                          'distinct_values': int(d[col].nunique())}
        tiers = {}
        d = d.assign(tier=d['score_ontime'].map(tier_of))
        for t, g in d.groupby('tier'):
            _, wci, _ = boot_stat(g, lambda x: float(x['win'].mean()), b=2000)
            _, mci, _ = boot_stat(g, lambda x: float(x['roi'].mean()), b=2000)
            tiers[t] = {'n': len(g), 'sessions': int(g['date'].nunique()), 'win': round(float(g['win'].mean()), 3),
                        'win_ci95': [round(v, 3) for v in wci], 'mean_roi': round(float(g['roi'].mean()), 4),
                        'mean_roi_ci95': [round(v, 4) for v in mci],
                        'tickers': sorted(g['ticker'].unique())}
        out[name] = {'n': len(d), 'sessions': int(d['date'].nunique()), 'scores': res, 'tiers': tiers,
                     'power': min_detectable_auc(d)}
    # the same test on forward trades with the lateness the live bot actually posted
    fw = df[(df['sample'] == 'forward') & df['late_s_sent'].notna()].copy()
    if len(fw):
        import alert_confidence_bridge as ACB
        fw['score_as_sent'] = ACB.score_as_sent(fw)
        a, a_ci, _ = boot_stat(fw, lambda x: auc(x['score_as_sent'].to_numpy(), x['win'].to_numpy()))
        out['forward_as_sent'] = {'n': len(fw), 'auc_win': round(a, 3), 'auc_ci95': [round(v, 3) for v in a_ci],
                                  'late_s_range': [float(fw['late_s_sent'].min()), float(fw['late_s_sent'].max())],
                                  'distinct_scores': int(fw['score_as_sent'].nunique())}
    # verdict per R1
    ok = True
    for s in ('backtest', 'forward'):
        lo, hi = out[s]['scores']['headline']['auc_ci95']
        ok &= (lo > 0.5)
    out['R1_score_predicts'] = bool(ok)
    return out


# -------------------------------------------------------------------------------------------------------- Q2
def factor_table(df, b=B):
    g = session_index(df)
    mod = df['module'].to_numpy()
    roi = df['roi'].to_numpy()
    X = {f: df[f].to_numpy(dtype=float) for f in FACTORS}
    point = {f: within_module_rho(X[f], roi, mod) for f in FACTORS}
    boots = {f: [] for f in FACTORS}
    for ix in draws(g, b):
        m_, r_ = mod[ix], roi[ix]
        rr = pct_rank_within(r_, m_)
        for f in FACTORS:
            rx = pct_rank_within(X[f][ix], m_)
            boots[f].append(np.nan if rx.std() == 0 or rr.std() == 0 else float(np.corrcoef(rx, rr)[0, 1]))
    return point, {f: np.asarray(v) for f, v in boots.items()}


def q2(df):
    bt, fw = df[df['sample'] == 'backtest'], df[df['sample'] == 'forward']
    pb, bb = factor_table(bt)
    pf, bf = factor_table(fw)
    pp, bp = factor_table(df)
    raw = np.array([p_two_sided(bb[f]) for f in FACTORS])
    adj = holm(raw)
    rows = {}
    for i, f in enumerate(FACTORS):
        sign = 1 if pb[f] > 0 else -1
        p_fw = p_one_sided(bf[f], sign)
        derive = bool(adj[i] < 0.10)
        confirm = bool(np.sign(pf[f]) == sign and p_fw < 0.10)
        rows[f] = {'rho_backtest': round(pb[f], 3), 'ci95_backtest': [round(v, 3) for v in ci(bb[f])],
                   'p_backtest': round(raw[i], 4), 'p_backtest_holm': round(float(adj[i]), 4),
                   'rho_forward': round(pf[f], 3), 'ci95_forward': [round(v, 3) for v in ci(bf[f])], 'p_forward_one_sided': round(p_fw, 4),
                   'rho_pooled': round(pp[f], 3), 'ci95_pooled': [round(v, 3) for v in ci(bp[f])],
                   'same_sign_both': bool(np.sign(pb[f]) == np.sign(pf[f])), 'passes_derivation': derive,
                   'passes_confirmation': confirm, 'ADOPT_R2': bool(derive and confirm)}
    return {'factors': rows, 'adopted': [f for f, r in rows.items() if r['ADOPT_R2']],
            'power': {'backtest': min_detectable_auc(bt), 'forward': min_detectable_auc(fw), 'pooled': min_detectable_auc(df)}}


# ------------------------------------------------------------------------------------------------ exploratory (seen)
def exploratory(df):
    out = {}
    for s, d in (('backtest', df[df['sample'] == 'backtest']), ('forward', df[df['sample'] == 'forward'])):
        rec = {}
        for m, g in d.groupby('module'):
            rec[m] = {'n': len(g), 'win': round(float(g['win'].mean()), 3), 'mean_roi': round(float(g['roi'].mean()), 4)}
        for c, g in d.groupby('tclass'):
            rec['class:' + c] = {'n': len(g), 'win': round(float(g['win'].mean()), 3), 'mean_roi': round(float(g['roi'].mean()), 4)}
        out[s] = rec
    return out


def sensitivity_matched(df_matched):
    """Same Q1 headline test with forward restricted to the 53 trades the ledger also contains."""
    fw = df_matched[df_matched['sample'] == 'forward']
    a, a_ci, _ = boot_stat(fw, lambda x: auc(x['score_ontime'].to_numpy(), x['win'].to_numpy()))
    return {'n_forward': len(fw), 'auc_win': round(a, 3), 'auc_ci95': [round(v, 3) for v in a_ci],
            'win': round(float(fw['win'].mean()), 3), 'mean_roi': round(float(fw['roi'].mean()), 4)}


if __name__ == '__main__':
    import dataset
    df = prep(pd.read_csv(os.path.join(HERE, 'data', 'trades_features.csv')))
    res = {'q1': q1(df), 'q2': q2(df), 'exploratory_seen': exploratory(df)}
    dm = prep(dataset.build(restrict_forward_to_ledger_matched=True))
    res['sensitivity_forward_ledger_matched'] = sensitivity_matched(dm)
    with open(os.path.join(HERE, 'results_q1q2.json'), 'w') as fh:
        json.dump(res, fh, indent=1, default=float)
    print(json.dumps({'q1_headline': {s: res['q1'][s]['scores']['headline'] for s in ('backtest', 'forward', 'pooled')},
                      'R1_score_predicts': res['q1']['R1_score_predicts'],
                      'q2_adopted': res['q2']['adopted'], 'q2_power': res['q2']['power']}, indent=1, default=float))
