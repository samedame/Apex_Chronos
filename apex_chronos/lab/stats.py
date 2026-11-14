"""Statistics used by the lab programme (catalogue items 87, 105, 106, 108 and the pre-registered tests).

Everything resamples whole sessions, never single trades: several P2 trades on one day are not independent.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

NAN = float('nan')
SEED = 20261114
DRAWS = 10000


def by_session(values: Sequence[float], sessions: Sequence[str]) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {}
    for v, s in zip(values, sessions):
        out.setdefault(s, []).append(float(v))
    return out


def clustered_mean_lb(values: Sequence[float], sessions: Sequence[str], draws: int = DRAWS, seed: int = SEED,
                      level: float = 0.95) -> float:
    """One-sided lower bound of the per-trade mean, resampling sessions with replacement (same scheme as
    phase_review._clustered_lower_bound)."""
    groups = list(by_session(values, sessions).values())
    if len(groups) < 3:
        return NAN
    rng = np.random.default_rng(seed)
    n = len(groups)
    sums = np.array([sum(g) for g in groups])
    cnts = np.array([len(g) for g in groups], dtype=float)
    idx = rng.integers(0, n, size=(draws, n))
    means = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    return float(np.quantile(means, 1.0 - level))


def clustered_diff(a_vals: Sequence[float], a_sess: Sequence[str], b_vals: Sequence[float], b_sess: Sequence[str],
                   draws: int = DRAWS, seed: int = SEED) -> Dict[str, float]:
    """mean(a) - mean(b) with a session-clustered bootstrap. Sessions are resampled jointly (a session contributes to both
    arms when it has trades in both). Returns diff, one-sided p for diff <= 0, the 95% one-sided lower bound, n per arm."""
    a_g, b_g = by_session(a_vals, a_sess), by_session(b_vals, b_sess)
    days = sorted(set(a_g) | set(b_g))
    na, nb = sum(len(v) for v in a_g.values()), sum(len(v) for v in b_g.values())
    if na == 0 or nb == 0 or len(days) < 3:
        return {'diff': NAN, 'p': NAN, 'lb95': NAN, 'n_a': na, 'n_b': nb}
    sa = np.array([sum(a_g.get(d, [])) for d in days]); ca = np.array([len(a_g.get(d, [])) for d in days], dtype=float)
    sb = np.array([sum(b_g.get(d, [])) for d in days]); cb = np.array([len(b_g.get(d, [])) for d in days], dtype=float)
    obs = sa.sum() / ca.sum() - sb.sum() / cb.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(days), size=(draws, len(days)))
    A, CA, B, CB = sa[idx].sum(1), ca[idx].sum(1), sb[idx].sum(1), cb[idx].sum(1)
    ok = (CA > 0) & (CB > 0)
    d = A[ok] / CA[ok] - B[ok] / CB[ok]
    if len(d) < draws * 0.5:
        return {'diff': float(obs), 'p': NAN, 'lb95': NAN, 'n_a': na, 'n_b': nb}
    # one-sided p-value: share of the centred bootstrap distribution at or above the observed difference
    p = float(np.mean((d - d.mean()) >= obs))
    return {'diff': float(obs), 'p': p, 'lb95': float(np.quantile(d, 0.05)), 'n_a': na, 'n_b': nb}


def holm(pvals: Dict[str, float]) -> Dict[str, float]:
    """Holm-Bonferroni adjusted p-values. nan stays nan and is not counted."""
    items = [(k, v) for k, v in pvals.items() if np.isfinite(v)]
    items.sort(key=lambda kv: kv[1])
    m = len(items)
    out = {k: NAN for k in pvals}
    run = 0.0
    for i, (k, v) in enumerate(items):
        run = max(run, min(1.0, (m - i) * v))
        out[k] = run
    return out


def cvar(values: Sequence[float], level: float = 0.95) -> float:
    """Mean of the worst (1 - level) share of values (left tail), e.g. of session P&L."""
    x = np.sort(np.asarray(values, dtype=float))
    if len(x) == 0:
        return NAN
    k = max(1, int(math.ceil(round((1.0 - level) * len(x), 9))))      # round: 0.05 * 20 is 1.0000000000000009
    return float(x[:k].mean())


def tail_summary(session_pnl: Sequence[float]) -> Dict[str, float]:
    x = np.asarray(session_pnl, dtype=float)
    if len(x) == 0:
        return {'n_sessions': 0, 'mean': NAN, 'worst': NAN, 'cvar95': NAN}
    return {'n_sessions': len(x), 'mean': float(x.mean()), 'worst': float(x.min()), 'cvar95': cvar(x, 0.95)}


def diebold_mariano(loss_a: Sequence[float], loss_b: Sequence[float], lag: int = 4) -> Dict[str, float]:
    """One-sided DM test that A has LOWER mean loss than B (H1: mean(loss_a - loss_b) < 0), Newey-West variance.
    Observations should be ordered by time; overlapping cross-sectional units make this conservative, not exact."""
    a, b = np.asarray(loss_a, dtype=float), np.asarray(loss_b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    d = (a - b)[ok]
    n = len(d)
    if n < 10:
        return {'stat': NAN, 'p': NAN, 'n': n, 'mean_diff': NAN}
    dm = d.mean()
    u = d - dm
    gamma0 = float(np.dot(u, u) / n)
    s = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        s += 2.0 * w * float(np.dot(u[k:], u[:-k]) / n)
    se = math.sqrt(max(s, 1e-18) / n)
    stat = dm / se
    from scipy.stats import norm
    return {'stat': float(stat), 'p': float(norm.cdf(stat)), 'n': n, 'mean_diff': float(dm)}


def risk_of_ruin(session_returns: Sequence[float], ruin_level: float = -0.5, horizon: int = 250, draws: int = 20000,
                 seed: int = SEED) -> float:
    """108: probability that cumulative account return touches `ruin_level` within `horizon` sessions, bootstrapping
    observed session returns with replacement (block of 1; serial dependence ignored: stated limit)."""
    x = np.asarray(session_returns, dtype=float)
    if len(x) < 20:
        return NAN
    rng = np.random.default_rng(seed)
    paths = x[rng.integers(0, len(x), size=(draws, horizon))]
    eq = np.cumprod(1.0 + paths, axis=1)
    return float(np.mean(np.min(eq, axis=1) <= 1.0 + ruin_level))


def fractional_kelly(mean: float, var: float, fraction: float = 0.25) -> float:
    """105: fraction of capital per bet under a small-edge Gaussian approximation, f* = mean/var, times `fraction`."""
    return float(max(0.0, fraction * mean / var)) if var > 0 and np.isfinite(mean) else NAN


def vol_target_size(base_fraction: float, forecast_sigma: float, target_sigma: float) -> float:
    """105: scale a base position fraction by target/forecast vol, capped at 1x the base (never lever up)."""
    if not (np.isfinite(forecast_sigma) and forecast_sigma > 0):
        return NAN
    return float(base_fraction * min(1.0, target_sigma / forecast_sigma))


def shrunk_ticker_means(values: Sequence[float], tickers: Sequence[str], k: float = 10.0) -> Dict[str, float]:
    """87: per-ticker mean shrunk toward the pooled mean, weight n/(n+k). Guards against 'drop the worst ticker' overfitting."""
    x = np.asarray(values, dtype=float)
    if len(x) == 0:
        return {}
    pooled = float(x.mean())
    out = {}
    for t in sorted(set(tickers)):
        v = np.array([a for a, b in zip(x, tickers) if b == t])
        n = len(v)
        out[t] = float((n * v.mean() + k * pooled) / (n + k))
    return out
