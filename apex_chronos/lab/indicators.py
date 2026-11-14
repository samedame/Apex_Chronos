"""Causal price/volatility indicators for the lab programme (catalogue items 5-7, 15-18, 35-56, 58-60, 65, 82, 85).

Conventions
  * Arrays are one-minute bars on the 09:30..15:59 grid. `m` is the entry bar index; a function that takes `m` uses bars
    0..m only (the bar at m has closed when the signal fires), so every value is available at the entry minute.
  * Variances are NOT annualised unless the name says `ann`. ANN = 390 * 252 minutes per year, as in config.
  * A function returns float('nan') when it lacks the data to answer; callers treat nan as "unknown", never as a pass.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .. import config as C

ANN = C.MINUTES_PER_YEAR_DEN
NAN = float('nan')


def _a(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def log_returns(close) -> np.ndarray:
    c = _a(close)
    with np.errstate(divide='ignore', invalid='ignore'):
        r = np.diff(np.log(c))
    return r[np.isfinite(r)] if len(r) else r


# ---------------------------------------------------------------------------------------------------------------
# 6, 5, 15, 16, 18: realised-variance estimators
def realised_var(close, a: int = 0, b: Optional[int] = None) -> float:
    """Sum of squared one-minute log returns over bars a..b (inclusive of the return into bar b). Not annualised."""
    c = _a(close)
    b = len(c) - 1 if b is None else b
    r = log_returns(c[a:b + 1])
    return float(np.sum(r * r)) if len(r) else NAN


def ann_vol(var_sum: float, n_returns: int) -> float:
    """Annualised vol from a sum of squared one-minute returns over n_returns minutes."""
    if not (np.isfinite(var_sum) and n_returns > 0):
        return NAN
    return float(math.sqrt(var_sum * ANN / n_returns))


def parkinson(high, low) -> float:
    """Per-bar Parkinson variance, mean over bars: (ln H/L)^2 / (4 ln 2)."""
    h, l = _a(high), _a(low)
    with np.errstate(divide='ignore', invalid='ignore'):
        v = np.log(h / l) ** 2 / (4.0 * math.log(2.0))
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) else NAN


def garman_klass(o, high, low, close) -> float:
    o, h, l, c = _a(o), _a(high), _a(low), _a(close)
    with np.errstate(divide='ignore', invalid='ignore'):
        v = 0.5 * np.log(h / l) ** 2 - (2.0 * math.log(2.0) - 1.0) * np.log(c / o) ** 2
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) else NAN


def rogers_satchell(o, high, low, close) -> float:
    """Drift-robust per-bar variance."""
    o, h, l, c = _a(o), _a(high), _a(low), _a(close)
    with np.errstate(divide='ignore', invalid='ignore'):
        v = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) else NAN


def yang_zhang(o, high, low, close) -> float:
    """Yang-Zhang per-bar variance over a window of consecutive bars (overnight term is the bar-to-bar open gap)."""
    o, h, l, c = _a(o), _a(high), _a(low), _a(close)
    n = len(c)
    if n < 3:
        return NAN
    with np.errstate(divide='ignore', invalid='ignore'):
        oj = np.log(o[1:] / c[:-1])
        cj = np.log(c[1:] / o[1:])
    oj, cj = oj[np.isfinite(oj)], cj[np.isfinite(cj)]
    if len(oj) < 2 or len(cj) < 2:
        return NAN
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
    rs = rogers_satchell(o[1:], h[1:], l[1:], c[1:])
    return float(np.var(oj, ddof=1) + k * np.var(cj, ddof=1) + (1.0 - k) * rs)


def bipower_variation(close) -> float:
    """(pi/2) * sum |r_i||r_{i-1}|: jump-robust variance, not annualised."""
    r = log_returns(close)
    if len(r) < 3:
        return NAN
    return float(math.pi / 2.0 * np.sum(np.abs(r[1:]) * np.abs(r[:-1])))


def two_scale_rv(close, k: int = 5) -> float:
    """Two-scale realised variance (Zhang-Mykland-Ait-Sahalia): sparse-grid average minus bias term. Not annualised."""
    c = _a(close)
    n = len(c) - 1
    if n < 4 * k:
        return NAN
    full = realised_var(c)
    sparse = []
    for j in range(k):
        r = log_returns(c[j::k])
        sparse.append(float(np.sum(r * r)))
    nbar = (n - k + 1.0) / k
    return float(np.mean(sparse) - (nbar / n) * full)


def max_abs_return_z(close) -> float:
    """Largest one-minute |return| in units of the window's own robust sigma (median absolute deviation)."""
    r = log_returns(close)
    if len(r) < 10:
        return NAN
    s = 1.4826 * np.median(np.abs(r - np.median(r)))
    return float(np.max(np.abs(r)) / s) if s > 0 else NAN


def jump_ratio(close) -> float:
    """max(RV - BV, 0) / RV in [0, 1]: share of realised variance attributed to jumps."""
    rv, bv = realised_var(close), bipower_variation(close)
    if not (np.isfinite(rv) and np.isfinite(bv)) or rv <= 0:
        return NAN
    return float(max(rv - bv, 0.0) / rv)


def garch11_forecast(daily_returns: Sequence[float], horizon: int = 1, min_obs: int = 250) -> float:
    """GARCH(1,1) variance forecast by Gaussian QMLE. Returns nan below `min_obs` daily observations: the repo archive
    starts 2026-10-16, so this stays dormant until a longer daily history is supplied (catalogue item 17, T3)."""
    r = _a(daily_returns)
    r = r[np.isfinite(r)]
    if len(r) < min_obs:
        return NAN
    from scipy.optimize import minimize
    r = r - r.mean()
    v0 = float(np.var(r))

    def nll(p):
        om, al, be = p
        if om <= 0 or al < 0 or be < 0 or al + be >= 0.999:
            return 1e12
        h = v0
        s = 0.0
        for x in r:
            s += math.log(h) + x * x / h
            h = om + al * x * x + be * h
        return 0.5 * s
    res = minimize(nll, [v0 * 0.05, 0.08, 0.9], method='Nelder-Mead', options={'maxiter': 600, 'xatol': 1e-9, 'fatol': 1e-9})
    om, al, be = res.x
    h = v0
    for x in r:
        h = om + al * x * x + be * h
    lr = om / max(1.0 - al - be, 1e-6)
    pers = al + be
    return float(lr + (h - lr) * (pers ** max(horizon - 1, 0)))


# ---------------------------------------------------------------------------------------------------------------
# 5: seasonality-adjusted remaining-session variance forecast
def minute_variance_shares(prior_closes: Sequence[Sequence[float]], n_bars: int = C.SESSION_BARS) -> np.ndarray:
    """Average share of a session's variance that falls on return k (k = 1..n_bars-1), from archived sessions (U-shape).
    Returns an array of length n_bars-1 summing to 1. Falls back to a flat profile when there are no sessions."""
    acc = np.zeros(n_bars - 1)
    n = 0
    for cl in prior_closes:
        r = log_returns(cl)
        if len(r) != n_bars - 1:
            continue
        tot = float(np.sum(r * r))
        if tot > 0:
            acc += (r * r) / tot
            n += 1
    if n == 0:
        return np.full(n_bars - 1, 1.0 / (n_bars - 1))
    return acc / n


def har_daily_forecast(daily_rv: Sequence[float]) -> Tuple[float, str]:
    """Next-day realised variance from past daily RVs. HAR-RV OLS on (d, 5-day mean, 22-day mean) once >= 40 days exist;
    otherwise the mean of the last up-to-14 days (the verdict's estimator). Returns (forecast, method)."""
    x = _a(daily_rv)
    x = x[np.isfinite(x) & (x > 0)]
    if len(x) < 5:
        return NAN, 'insufficient'
    if len(x) < 40:
        return float(np.mean(x[-14:])), 'mean14'
    d, w, mth, y = [], [], [], []
    for t in range(22, len(x)):
        d.append(x[t - 1])
        w.append(np.mean(x[t - 5:t]))
        mth.append(np.mean(x[t - 22:t]))
        y.append(x[t])
    X = np.column_stack([np.ones(len(y)), d, w, mth])
    beta, *_ = np.linalg.lstsq(X, np.asarray(y), rcond=None)
    f = float(beta @ np.array([1.0, x[-1], np.mean(x[-5:]), np.mean(x[-22:])]))
    return (max(f, float(np.min(x)) * 0.25), 'har')


def rest_of_day_var_forecast(prior_closes: Sequence[Sequence[float]], today_close: Sequence[float], m: int) -> Dict:
    """Forecast of sum_{k>m} r_k^2 (variance still to come) combining (a) the U-shaped minute shares, (b) a HAR/mean
    forecast of the whole day's variance, (c) today's morning realised variance, shrunk toward the baseline by the share of
    the day's variance already elapsed (lambda = min(0.8, elapsed share)). Heuristic weights are fixed here and tested
    by R8 in docs/LAB_PREREG.md against the 14-session mean."""
    shares = minute_variance_shares(prior_closes)
    daily = [realised_var(cl) for cl in prior_closes]
    base, method = har_daily_forecast(daily)
    c = _a(today_close)[:m + 1]
    if not np.isfinite(base) or len(c) < 10:
        return {'forecast': NAN, 'method': method, 'baseline_day': base, 'ratio': NAN, 'elapsed_share': NAN}
    elapsed = float(np.sum(shares[:m]))             # returns 1..m belong to bars 1..m
    rest_share = float(np.sum(shares[m:]))
    morning = realised_var(c)
    expected_morning = base * elapsed
    ratio = morning / expected_morning if expected_morning > 0 else 1.0
    lam = min(0.8, elapsed)
    adj = 1.0 + lam * (ratio - 1.0)
    adj = min(max(adj, 0.4), 2.5)
    return {'forecast': float(base * rest_share * adj), 'method': method, 'baseline_day': float(base), 'ratio': float(ratio),
            'elapsed_share': elapsed}


def mean14_rest_forecast(prior_closes: Sequence[Sequence[float]], m: int, lookback: int = 14) -> float:
    """The verdict's estimator in the same units: mean over prior sessions of sum_{k>m} r_k^2."""
    v = []
    for cl in list(prior_closes)[-lookback:]:
        r = log_returns(cl)
        if len(r) > m:
            v.append(float(np.sum(r[m:] ** 2)))
    return float(np.mean(v)) if len(v) >= C.MIN_HISTORY_SESSIONS else NAN


def qlike(forecast: float, realised: float) -> float:
    if not (np.isfinite(forecast) and np.isfinite(realised)) or forecast <= 0 or realised <= 0:
        return NAN
    q = realised / forecast
    return float(q - math.log(q) - 1.0)


# ---------------------------------------------------------------------------------------------------------------
# 47-53, 56: regime / trendiness measures
def variance_ratio(close, q: int = 5, min_returns: int = 30) -> float:
    """Lo-MacKinlay VR(q) on overlapping q-minute returns: Var(r_q) / (q Var(r_1)). < 1 mean-reverting, > 1 trending."""
    r = log_returns(close)
    if len(r) < max(min_returns, 2 * q):
        return NAN
    v1 = float(np.var(r, ddof=1))
    if v1 <= 0:
        return NAN
    rq = np.convolve(r, np.ones(q), mode='valid')
    return float(np.var(rq, ddof=1) / (q * v1))


def autocorr1(close) -> float:
    r = log_returns(close)
    if len(r) < 20 or np.std(r) == 0:
        return NAN
    return float(np.corrcoef(r[1:], r[:-1])[0, 1])


def efficiency_ratio(close, n: Optional[int] = None) -> float:
    """Kaufman efficiency ratio: |net move| / sum |moves| over the last n bars (default: all bars so far)."""
    c = _a(close)
    c = c if n is None else c[-(n + 1):]
    if len(c) < 3:
        return NAN
    path = float(np.sum(np.abs(np.diff(c))))
    return float(abs(c[-1] - c[0]) / path) if path > 0 else NAN


def hurst_rs(close, min_returns: int = 64) -> float:
    """Rescaled-range Hurst exponent of one-minute returns (T3; superseded by variance_ratio, logged for completeness)."""
    r = log_returns(close)
    n = len(r)
    if n < min_returns:
        return NAN
    sizes = [s for s in (8, 16, 32, 64, 128, 256) if s <= n]
    rs = []
    for s in sizes:
        vals = []
        for i in range(0, n - s + 1, s):
            seg = r[i:i + s]
            z = np.cumsum(seg - seg.mean())
            sd = seg.std()
            if sd > 0:
                vals.append((z.max() - z.min()) / sd)
        rs.append(np.mean(vals) if vals else NAN)
    ok = [(math.log(s), math.log(v)) for s, v in zip(sizes, rs) if np.isfinite(v) and v > 0]
    if len(ok) < 3:
        return NAN
    x, y = zip(*ok)
    return float(np.polyfit(x, y, 1)[0])


def choppiness_index(high, low, close, n: int = 30) -> float:
    """100 * log10(sum ATR1 / (max H - min L)) / log10(n). High (> 61.8) = choppy, low (< 38.2) = trending."""
    h, l, c = _a(high), _a(low), _a(close)
    if len(c) < n + 1:
        return NAN
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))[-n:]
    rng = float(np.max(h[-n:]) - np.min(l[-n:]))
    if rng <= 0 or float(tr.sum()) <= 0:
        return NAN
    return float(100.0 * math.log10(float(tr.sum()) / rng) / math.log10(n))


def nr_inside_flags(prior_hl: Sequence[Tuple[float, float]]) -> Dict[str, float]:
    """From prior sessions' (high, low) newest last: NR4 / NR7 (narrowest range of the last 4 / 7), inside day."""
    if len(prior_hl) < 2:
        return {'nr4': NAN, 'nr7': NAN, 'inside_day': NAN}
    rng = [h - l for h, l in prior_hl]
    out = {'inside_day': float(prior_hl[-1][0] <= prior_hl[-2][0] and prior_hl[-1][1] >= prior_hl[-2][1])}
    out['nr4'] = float(len(rng) >= 4 and rng[-1] <= min(rng[-4:]))
    out['nr7'] = float(len(rng) >= 7 and rng[-1] <= min(rng[-7:]))
    if len(rng) < 4:
        out['nr4'] = NAN
    if len(rng) < 7:
        out['nr7'] = NAN
    return out


def day_range_used(high, low, m: int, prior_ranges: Sequence[float]) -> float:
    """(session high - low so far) / mean prior daily range."""
    if len(prior_ranges) < C.MIN_HISTORY_SESSIONS:
        return NAN
    cur = float(np.max(_a(high)[:m + 1]) - np.min(_a(low)[:m + 1]))
    return float(cur / np.mean(prior_ranges[-14:]))


def minutes_since_extreme(high, low, m: int) -> Dict[str, int]:
    h, l = _a(high)[:m + 1], _a(low)[:m + 1]
    return {'since_high': int(m - int(np.argmax(h))), 'since_low': int(m - int(np.argmin(l)))}


def ibs_and_z(prior_closes_daily: Sequence[float], prior_hl: Sequence[Tuple[float, float]], z_n: int = 20) -> Dict[str, float]:
    """Previous day's internal bar strength and the z-score of its close vs the last z_n daily closes."""
    out = {'ibs': NAN, 'close_z': NAN}
    if prior_hl:
        h, l = prior_hl[-1]
        if h > l and prior_closes_daily:
            out['ibs'] = float((prior_closes_daily[-1] - l) / (h - l))
    c = _a(prior_closes_daily)[-z_n:]
    if len(c) >= 10 and np.std(c, ddof=1) > 0:
        out['close_z'] = float((c[-1] - np.mean(c)) / np.std(c, ddof=1))
    return out


def first_n_return(close, open0: float, n: int = 30) -> float:
    c = _a(close)
    return float(c[n - 1] / open0 - 1.0) if len(c) >= n else NAN


def orb_state(high, low, close, m: int, n: int = 15) -> int:
    """+1 above the n-minute opening range, -1 below, 0 inside, nan before the range is complete."""
    if m < n:
        return 0
    rh, rl = float(np.max(_a(high)[:n])), float(np.min(_a(low)[:n]))
    c = float(_a(close)[m])
    return 1 if c > rh else (-1 if c < rl else 0)


def overnight_intraday_split(prev_close: float, open0: float, close, m: int) -> Dict[str, float]:
    c = _a(close)
    if not (prev_close and open0 and len(c) > m):
        return {'overnight_ret': NAN, 'intraday_ret': NAN, 'overnight_share': NAN}
    on, intr = math.log(open0 / prev_close), math.log(float(c[m]) / open0)
    den = abs(on) + abs(intr)
    return {'overnight_ret': float(on), 'intraday_ret': float(intr), 'overnight_share': float(abs(on) / den) if den > 0 else NAN}


# ---------------------------------------------------------------------------------------------------------------
# 40: Noise Area (Zarattini, Aziz, Barbon 2024)
def noise_area(prior_open_close: Sequence[Tuple[Sequence[float], Sequence[float]]], today_open0: float, prev_close: float,
               m: int, lookback: int = 14) -> Dict[str, float]:
    """Gap-adjusted Noise Area at minute m.

    sigma_m = mean over the last `lookback` prior sessions of |C_m / O_0 - 1| (the move from the open at the same
    minute). upper = max(O_0, prev_close) * (1 + sigma_m); lower = min(O_0, prev_close) * (1 - sigma_m).
    `prior_open_close` is a list of (open array, close array) per prior session, oldest first."""
    sig = []
    for o, c in list(prior_open_close)[-lookback:]:
        o, c = _a(o), _a(c)
        if len(c) > m and len(o) and o[0] > 0:
            sig.append(abs(c[m] / o[0] - 1.0))
    if len(sig) < C.MIN_HISTORY_SESSIONS or not today_open0 or not prev_close:
        return {'lower': NAN, 'upper': NAN, 'sigma': NAN, 'half_width': NAN}
    s = float(np.mean(sig))
    upper = max(today_open0, prev_close) * (1.0 + s)
    lower = min(today_open0, prev_close) * (1.0 - s)
    return {'lower': float(lower), 'upper': float(upper), 'sigma': s, 'half_width': float((upper - lower) / 2.0)}


def inside_noise_area(price: float, na: Dict[str, float]) -> float:
    if not np.isfinite(na.get('lower', NAN)):
        return NAN
    return float(na['lower'] <= price <= na['upper'])


def strike_beyond_band(price: float, strike: float, na: Dict[str, float]) -> float:
    """Distance from the nearest band edge to the short strike, in units of the band half-width, positive when the strike
    lies outside the band on the side it sits (a call wall above the upper edge, a put wall below the lower edge)."""
    if not np.isfinite(na.get('half_width', NAN)) or na['half_width'] <= 0:
        return NAN
    if strike >= price:
        return float((strike - na['upper']) / na['half_width'])
    return float((na['lower'] - strike) / na['half_width'])


# ---------------------------------------------------------------------------------------------------------------
# 38, 39, 41, 42, 44: levels and bands
def vwap_series(high, low, close, volume) -> np.ndarray:
    h, l, c, v = _a(high), _a(low), _a(close), _a(volume)
    typ = (h + l + c) / 3.0
    cv = np.maximum(np.cumsum(v), 1.0)
    return np.cumsum(typ * v) / cv


def vwap_deviation_z(high, low, close, volume, m: int) -> float:
    """(close - VWAP) / running std of (close - VWAP) over the session so far."""
    vw = vwap_series(high, low, close, volume)[:m + 1]
    d = _a(close)[:m + 1] - vw
    if len(d) < 20 or np.std(d) == 0:
        return NAN
    return float(d[-1] / np.std(d))


def anchored_vwap(high, low, close, volume, anchor: int, m: int) -> float:
    h, l, c, v = (_a(x)[anchor:m + 1] for x in (high, low, close, volume))
    if len(c) == 0 or float(v.sum()) <= 0:
        return NAN
    return float(np.sum((h + l + c) / 3.0 * v) / v.sum())


def volume_profile(close, volume, m: int, bins: int = 40, value_area: float = 0.70) -> Dict[str, float]:
    """Point of control and value-area high/low of the session so far."""
    c, v = _a(close)[:m + 1], _a(volume)[:m + 1]
    if len(c) < 30 or c.max() == c.min() or v.sum() <= 0:
        return {'poc': NAN, 'vah': NAN, 'val': NAN}
    hist, edges = np.histogram(c, bins=bins, weights=v)
    centers = (edges[:-1] + edges[1:]) / 2.0
    i = int(np.argmax(hist))
    lo = hi = i
    tot, acc = hist.sum(), hist[i]
    while acc < value_area * tot and (lo > 0 or hi < len(hist) - 1):
        down = hist[lo - 1] if lo > 0 else -1
        up = hist[hi + 1] if hi < len(hist) - 1 else -1
        if up >= down:
            hi += 1
            acc += hist[hi]
        else:
            lo -= 1
            acc += hist[lo]
    return {'poc': float(centers[i]), 'vah': float(centers[hi]), 'val': float(centers[lo])}


def atr_intraday(high, low, close, n: int = 30) -> float:
    h, l, c = _a(high), _a(low), _a(close)
    if len(c) < n + 1:
        return NAN
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(np.mean(tr[-n:]))


def atr_daily(prior_hl_c: Sequence[Tuple[float, float, float]], n: int = 14) -> float:
    """Daily ATR from prior sessions' (high, low, close), oldest first."""
    if len(prior_hl_c) < 3:
        return NAN
    tr = []
    for i in range(1, len(prior_hl_c)):
        h, l, _ = prior_hl_c[i]
        pc = prior_hl_c[i - 1][2]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(tr[-n:]))


def bollinger_width(close, n: int = 20, k: float = 2.0) -> float:
    c = _a(close)
    if len(c) < n:
        return NAN
    w = c[-n:]
    return float(2.0 * k * np.std(w, ddof=1) / np.mean(w))


def squeeze_flag(high, low, close, n: int = 20) -> float:
    """1 when the Bollinger bands sit inside the Keltner channel (compressed range), else 0."""
    h, l, c = _a(high), _a(low), _a(close)
    if len(c) < n + 1:
        return NAN
    w = c[-n:]
    bb = 2.0 * np.std(w, ddof=1)
    atr = atr_intraday(h, l, c, n)
    return float(bb < 1.5 * atr) if np.isfinite(atr) else NAN


def donchian(high, low, n: int = 30) -> Dict[str, float]:
    h, l = _a(high), _a(low)
    if len(h) < n:
        return {'upper': NAN, 'lower': NAN}
    return {'upper': float(h[-n:].max()), 'lower': float(l[-n:].min())}


def round_number_distance(price: float) -> Dict[str, float]:
    """Distance to the nearest 'round' level as a fraction of price (step 10 above $1000, 5 above $100, else 1)."""
    step = 10.0 if price >= 1000 else (5.0 if price >= 100 else 1.0)
    near = round(price / step) * step
    return {'level': float(near), 'dist_frac': float(abs(price - near) / price)}


def pivot_levels(prev_h: float, prev_l: float, prev_c: float) -> Dict[str, float]:
    """Classic floor-trader pivots, logged as context only (catalogue item 43 is rejected as a signal)."""
    p = (prev_h + prev_l + prev_c) / 3.0
    return {'pivot': p, 'r1': 2 * p - prev_l, 's1': 2 * p - prev_h}


# ---------------------------------------------------------------------------------------------------------------
# 58-60, 65: cross-section / flow proxies computed from the bot's own 12-ticker universe
def breadth_above_vwap(tickers_bars: Dict[str, Dict[str, Sequence[float]]], m: int) -> float:
    """Share of the tickers trading above their own session VWAP at bar m (breadth proxy; the paid NYSE feeds stay DEFER)."""
    up = tot = 0
    for b in tickers_bars.values():
        try:
            vw = vwap_series(b['H'], b['L'], b['C'], b['V'])
            up += float(_a(b['C'])[m] > vw[m])
            tot += 1
        except (KeyError, IndexError):
            continue
    return float(up / tot) if tot else NAN


def megacap_contribution(index_ret: float, megacap_rets: Sequence[float]) -> float:
    """Mean mega-cap return since the open divided by the index return (concentration proxy); nan if the index is flat."""
    if not megacap_rets or abs(index_ret) < 1e-5:
        return NAN
    return float(np.mean(megacap_rets) / index_ret)


def bar_ofi(close, volume, n: int = 30) -> float:
    """Tick-rule order-flow proxy from one-minute bars: sum(sign(dC) * V) / sum(V) over the last n bars (Lee-Ready needs
    ticks; this is the bar-level stand-in for catalogue item 65)."""
    c, v = _a(close), _a(volume)
    if len(c) < n + 1:
        return NAN
    s = np.sign(np.diff(c))[-n:]
    vv = v[1:][-n:]
    return float(np.sum(s * vv) / np.sum(vv)) if np.sum(vv) > 0 else NAN


# ---------------------------------------------------------------------------------------------------------------
# 82: change-point alarm (kill-switch input)
def cusum_variance_alarm(close, ref_var_per_min: float, k: float = 0.5, h: float = 6.0) -> Dict[str, float]:
    """One-sided CUSUM on standardised squared returns against a reference per-minute variance. `alarm` = 1 when the
    statistic crosses h; `stat` is its latest value. Use only as a mute input (judged on CVaR, not mean)."""
    r = log_returns(close)
    if len(r) < 20 or not np.isfinite(ref_var_per_min) or ref_var_per_min <= 0:
        return {'stat': NAN, 'alarm': NAN, 'first_alarm_bar': NAN}
    z = r * r / ref_var_per_min - 1.0
    s, first = 0.0, None
    for i, x in enumerate(z):
        s = max(0.0, s + x - k)
        if first is None and s > h:
            first = i + 1
    return {'stat': float(s), 'alarm': float(first is not None), 'first_alarm_bar': float(first) if first is not None else NAN}


# ---------------------------------------------------------------------------------------------------------------
# 85: conformal bound on the remaining high/low
def conformal_remaining_range(prior_hlc: Sequence[Tuple[Sequence[float], Sequence[float], Sequence[float]]], m: int,
                              alpha: float = 0.10) -> Dict[str, float]:
    """Distribution-free upper bounds, as fractions of the price at minute m, for how far the REST of the session can
    travel up and down. Split-conformal quantile with the (1 + 1/n) finite-sample correction over prior sessions.
    Returns up/down bounds at coverage 1 - alpha per side and the number of sessions used."""
    ups, dns = [], []
    for h, l, c in prior_hlc:
        h, l, c = _a(h), _a(l), _a(c)
        if len(c) > m + 1 and c[m] > 0:
            ups.append(float(np.max(h[m + 1:]) / c[m] - 1.0))
            dns.append(float(1.0 - np.min(l[m + 1:]) / c[m]))
    n = len(ups)
    if n < C.MIN_HISTORY_SESSIONS:
        return {'up': NAN, 'down': NAN, 'n': n}
    q = min(1.0, math.ceil((n + 1) * (1.0 - alpha)) / n)
    return {'up': float(np.quantile(ups, q)), 'down': float(np.quantile(dns, q)), 'n': n}
