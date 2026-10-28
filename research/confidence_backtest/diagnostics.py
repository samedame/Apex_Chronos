"""Post-hoc diagnostics (NOT pre-registered; labelled as such in the report). They exist to stop in-sample numbers being mistaken for evidence.

D1  Walk-forward setup score: the shipped setup component is built from tables that contain these very trades, so its AUC is partly
    in-sample. Here each session is scored from a table built ONLY from earlier sessions (first WARMUP sessions are training-only).
D2  Refit ceiling: the best a data-driven re-score could do with the registered inputs (module + the 11 candidate factors), measured
    walk-forward with strong L2 regularisation. If this is ~0.5, re-weighting the score to chase wins would only fit noise.

Method note: a first version used leave-one-session-out. That is biased low for group-mean scores (removing a session lowers the training
mean exactly when its outcomes were high); under a pure-noise null it averaged AUC 0.38, so its 0.32 on the real data meant nothing. It was
discarded before any conclusion was drawn and replaced by walk-forward, which is checked against the same null (`null_check`).
D3  Sample size needed to detect a given AUC, and how many sessions that is at the observed trade rate.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import norm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from apex_chronos import alert_confidence as AC, config as C       # noqa: E402
import analysis_q1q2 as A                                          # noqa: E402


# ------------------------------------------------------------------------------------------------------------- D1
WARMUP = 10


def walkforward_setup(df):
    """Setup quality for every trade after the warm-up, from a table built only from strictly earlier sessions. NaN during warm-up."""
    q = np.full(len(df), np.nan)
    dates = sorted(df['date'].unique())
    saved = {k: dict(v) for k, v in AC.RECORD.items()}
    try:
        for i, d in enumerate(dates):
            if i < WARMUP:
                continue
            train = df[df['date'].isin(dates[:i])]
            rec = {}
            for m in C.MODULE_ORDER:
                g = train[train['module'] == m]
                rec[m] = {'bt': (len(g), int(g['win'].sum()), float(g['roi'].mean()) if len(g) else 0.0), 'fwd': (0, 0, 0.0)}
            AC.RECORD.clear()
            AC.RECORD.update(rec)
            for ix in np.flatnonzero((df['date'] == d).to_numpy()):
                q[ix] = AC.setup_quality(df['module'].iloc[ix])[0]
    finally:
        AC.RECORD.clear()
        AC.RECORD.update(saved)
    return q


def null_check(df, n=40, seed=1):
    """AUC of the walk-forward setup score when outcomes are shuffled (true AUC 0.5): must centre on 0.5 for the method to be trusted."""
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        p = df.copy()
        perm = rng.permutation(len(p))
        p['win'] = p['win'].to_numpy()[perm]
        p['roi'] = p['roi'].to_numpy()[perm]
        q = walkforward_setup(p)
        ok = ~np.isnan(q)
        vals.append(A.auc(q[ok], p['win'].to_numpy()[ok]))
    return {'mean': round(float(np.mean(vals)), 3), 'min': round(float(min(vals)), 3), 'max': round(float(max(vals)), 3)}


def d1(df):
    df = df.reset_index(drop=True)
    q = walkforward_setup(df)
    d = df.assign(q_wf=q).dropna(subset=['q_wf'])
    out = {'warmup_sessions': WARMUP, 'scored_trades': int(len(d)), 'scored_sessions': int(d['date'].nunique()),
           'null_check_auc_when_outcomes_shuffled': null_check(df)}
    for name, g in (('later_backtest_sessions', d[d['sample'] == 'backtest']), ('forward', d[d['sample'] == 'forward']), ('all_scored', d)):
        a, a_ci, _ = A.boot_stat(g, lambda x: A.auc(x['q_wf'].to_numpy(), x['win'].to_numpy()))
        r, r_ci, _ = A.boot_stat(g, lambda x: A.spearman(x['q_wf'].to_numpy(), x['roi'].to_numpy()))
        out[name] = {'n': int(len(g)), 'auc_win_walkforward': round(a, 3), 'auc_ci95': [round(v, 3) for v in a_ci],
                     'spearman_roi_walkforward': round(r, 3), 'spearman_ci95': [round(v, 3) for v in r_ci]}
    return out


# ------------------------------------------------------------------------------------------------------------- D2
def _design(df):
    X = df[A.FACTORS].to_numpy(dtype=float)
    mods = pd.get_dummies(df['module']).reindex(columns=C.MODULE_ORDER, fill_value=0).to_numpy(dtype=float)
    return np.hstack([mods, X])


def _fit_logit(X, y, lam):
    n, p = X.shape

    def f(w):
        z = X @ w[:-1] + w[-1]
        ll = np.sum(np.logaddexp(0, z) - y * z)
        return ll + 0.5 * lam * np.sum(w[:-1] ** 2)

    def g(w):
        z = X @ w[:-1] + w[-1]
        pr = 1 / (1 + np.exp(-z))
        gr = X.T @ (pr - y) + lam * w[:-1]
        return np.append(gr, np.sum(pr - y))
    return minimize(f, np.zeros(p + 1), jac=g, method='L-BFGS-B').x


def d2(df, lam=20.0, shuffle_seed=None):
    df = df.reset_index(drop=True)
    if shuffle_seed is not None:
        perm = np.random.default_rng(shuffle_seed).permutation(len(df))
        df = df.assign(win=df['win'].to_numpy()[perm], roi=df['roi'].to_numpy()[perm])
    Xall = _design(df)
    y = df['win'].to_numpy(dtype=float)
    roi = df['roi'].to_numpy(dtype=float)
    pred, pred_roi = np.full(len(df), np.nan), np.full(len(df), np.nan)
    dates = sorted(df['date'].unique())
    for i, d in enumerate(dates):
        if i < WARMUP:
            continue
        te = (df['date'] == d).to_numpy()
        tr = df['date'].isin(dates[:i]).to_numpy()
        mu, sd = Xall[tr].mean(0), Xall[tr].std(0)
        sd[sd == 0] = 1
        Z = (Xall - mu) / sd
        w = _fit_logit(Z[tr], y[tr], lam)
        pred[te] = Z[te] @ w[:-1] + w[-1]
        A_ = np.hstack([Z[tr], np.ones((tr.sum(), 1))])
        beta = np.linalg.solve(A_.T @ A_ + lam * np.eye(A_.shape[1]), A_.T @ roi[tr])
        pred_roi[te] = np.hstack([Z[te], np.ones((te.sum(), 1))]) @ beta
    d = df.assign(pred=pred, pred_roi=pred_roi).dropna(subset=['pred'])
    if shuffle_seed is not None:
        return A.auc(d['pred'].to_numpy(), d['win'].to_numpy())
    res = {'warmup_sessions': WARMUP, 'scored_trades': int(len(d))}
    for name, g in (('later_backtest_sessions', d[d['sample'] == 'backtest']), ('forward', d[d['sample'] == 'forward']), ('all_scored', d)):
        a, a_ci, _ = A.boot_stat(g, lambda x: A.auc(x['pred'].to_numpy(), x['win'].to_numpy()))
        r, r_ci, _ = A.boot_stat(g, lambda x: A.spearman(x['pred_roi'].to_numpy(), x['roi'].to_numpy()))
        res[name] = {'n': int(len(g)), 'auc_win_walkforward': round(a, 3), 'auc_ci95': [round(v, 3) for v in a_ci],
                     'spearman_roi_walkforward': round(r, 3), 'spearman_ci95': [round(v, 3) for v in r_ci]}
    nulls = [d2(df, lam, shuffle_seed=s) for s in range(20)]
    res['null_check_auc_when_outcomes_shuffled'] = {'mean': round(float(np.mean(nulls)), 3), 'min': round(float(min(nulls)), 3), 'max': round(float(max(nulls)), 3)}
    return res


# ------------------------------------------------------------------------------------------------------------- D3
def n_for_auc(target, win_share=0.5, alpha=0.05, power=0.80, deff=1.0):
    z = norm.ppf(1 - alpha / 2) + norm.ppf(power)
    q1, q2 = target / (2 - target), 2 * target ** 2 / (1 + target)
    for n in range(20, 5000):
        n1, n0 = n * win_share, n * (1 - win_share)
        se = np.sqrt((target * (1 - target) + (n1 - 1) * (q1 - target ** 2) + (n0 - 1) * (q2 - target ** 2)) / (n1 * n0))
        if (target - 0.5) / (se * np.sqrt(deff)) >= z:
            return n
    return None


def d3(df):
    per_session = len(df) / df['date'].nunique()
    out = {'trades_per_session': round(per_session, 2), 'have': len(df), 'sessions_have': int(df['date'].nunique())}
    for t in (0.58, 0.60, 0.65, 0.70):
        n = n_for_auc(t, win_share=float(df['win'].mean()))
        out[f'auc_{t}'] = {'trades_needed': n, 'more_trades_than_now': max(0, n - len(df)), 'sessions_needed_total': round(n / per_session, 1),
                           'more_sessions_pooled': round(max(0, n - len(df)) / per_session, 1),
                           'clean_forward_only_sessions': round(n / per_session, 1)}
    return out


if __name__ == '__main__':
    import json
    df = A.prep(pd.read_csv(os.path.join(HERE, 'data', 'trades_features.csv')))
    res = {'D1_walkforward_setup': d1(df), 'D2_refit_ceiling_walkforward': d2(df), 'D3_sample_size': d3(df)}
    with open(os.path.join(HERE, 'results_diag.json'), 'w') as fh:
        json.dump(res, fh, indent=1, default=float)
    print(json.dumps(res, indent=1, default=float))
