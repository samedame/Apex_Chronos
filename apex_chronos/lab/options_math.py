"""Option mathematics for the lab programme (catalogue items 2, 3, 12-14, 21-24, 26, 28, 29, 61, 86, 88-90, 104).

Chain rows are the compact dicts written by lab/snapshots.py:
  k strike, t 'c'|'p', b bid, a ask, bs bid size, as ask size, v volume, oi open interest, iv mid IV (decimal),
  d delta, g gamma, th theta, vg vega, bd/ad bid/ask epoch-ms, last
All times are in YEARS of the 390 x 252 minute convention used by the bot (config.MINUTES_PER_YEAR_DEN).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from .. import config as C

NAN = float('nan')
ANN = C.MINUTES_PER_YEAR_DEN
R = C.RISK_FREE


def minutes_left(m: int) -> float:
    """Minutes from the close of bar m to 16:00 ET."""
    return float(C.SESSION_BARS - (m + 1))


def T_years(minutes: float) -> float:
    return max(float(minutes), 0.25) / ANN          # floor of 15 seconds avoids the T -> 0 singularity


# ---------------------------------------------------------------------------------------------------------------
def bs_price(S, K, T, sigma, is_call, r: float = R):
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / sq
    d2 = d1 - sq
    if is_call:
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_gamma(S, K, T, sigma, r: float = R):
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / sq
    return norm.pdf(d1) / (S * sq)


def bs_vanna(S, K, T, sigma, r: float = R):
    """d(delta)/d(sigma) = d(vega)/dS = -pdf(d1) d2 / sigma."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / sq
    d2 = d1 - sq
    return -norm.pdf(d1) * d2 / sigma


def bs_charm(S, K, T, sigma, is_call, r: float = R):
    """d(delta)/dT per year of calendar time passing (negative of d delta / d tau), no dividends."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / sq
    d2 = d1 - sq
    c = -norm.pdf(d1) * (2 * r * T - d2 * sq) / (2 * T * sq)
    return c if is_call else c            # no-dividend charm is identical for calls and puts


def implied_vol(price: float, S: float, K: float, T: float, is_call: bool, r: float = R) -> float:
    """Black-Scholes IV from an option price; nan when the price is outside the no-arbitrage bounds."""
    try:
        intrinsic = max(S - K * math.exp(-r * T), 0.0) if is_call else max(K * math.exp(-r * T) - S, 0.0)
        if not (price > intrinsic + 1e-9) or price >= (S if is_call else K):
            return NAN
        f = lambda s: float(bs_price(S, K, T, s, is_call, r)) - price
        lo, hi = 1e-3, 6.0
        if f(lo) > 0 or f(hi) < 0:
            return NAN
        return float(brentq(f, lo, hi, xtol=1e-8, maxiter=100))
    except (ValueError, ZeroDivisionError, OverflowError):
        return NAN


# ---------------------------------------------------------------------------------------------------------------
# 86: touch probability
def touch_probability(S: float, K: float, sigma_ann: float, minutes_to_close: float) -> float:
    """P(price touches K before the close) for a driftless diffusion: 2 N(-|ln(K/S)| / (sigma sqrt(T))). Capped at 1."""
    if not (S > 0 and K > 0 and np.isfinite(sigma_ann) and sigma_ann > 0 and minutes_to_close > 0):
        return NAN
    z = abs(math.log(K / S)) / (sigma_ann * math.sqrt(minutes_to_close / ANN))
    return float(min(1.0, 2.0 * norm.cdf(-z)))


def touch_probability_var(S: float, K: float, rest_var: float) -> float:
    """Same, with the forecast remaining variance (sum of squared one-minute log returns still to come) used directly."""
    if not (S > 0 and K > 0 and np.isfinite(rest_var) and rest_var > 0):
        return NAN
    return float(min(1.0, 2.0 * norm.cdf(-abs(math.log(K / S)) / math.sqrt(rest_var))))


# 21: implied move
def implied_move_from_straddle(call_mid: float, put_mid: float) -> float:
    """Expected ABSOLUTE move to expiry in price points. For an ATM option E|dS| = 0.7979 sigma sqrt(T) S, which is also
    the ATM straddle's value, so the straddle mid is the expected absolute move itself. (The research note's "x 0.8" haircut
    would double-count that factor; the one-sigma move is straddle / 0.7979 and is returned by implied_sigma_move.)"""
    if not (call_mid > 0 and put_mid > 0):
        return NAN
    return float(call_mid + put_mid)


def implied_sigma_move(call_mid: float, put_mid: float) -> float:
    """One-standard-deviation implied move to expiry in price points: straddle / sqrt(2/pi)."""
    m = implied_move_from_straddle(call_mid, put_mid)
    return float(m / math.sqrt(2.0 / math.pi)) if np.isfinite(m) else NAN


def distance_in_implied_moves(S: float, K: float, implied_move: float) -> float:
    return float(abs(K - S) / implied_move) if implied_move and np.isfinite(implied_move) and implied_move > 0 else NAN


# 3: variance risk premium at the strike
def vrp_ratio(iv_strike: float, forecast_sigma_ann: float) -> float:
    if not (np.isfinite(iv_strike) and np.isfinite(forecast_sigma_ann)) or forecast_sigma_ann <= 0:
        return NAN
    return float(iv_strike / forecast_sigma_ann)


def forecast_sigma_ann(rest_var: float, minutes_to_close: float) -> float:
    """Annualised vol implied by a remaining-variance forecast over `minutes_to_close` minutes."""
    if not (np.isfinite(rest_var) and rest_var > 0 and minutes_to_close > 0):
        return NAN
    return float(math.sqrt(rest_var * ANN / minutes_to_close))


# 61, 62, 63, 89: spread and credit gates
def leg_half_spread(leg: Dict) -> float:
    b, a = leg.get('b'), leg.get('a')
    if b is None or a is None or not (a >= b >= 0):
        return NAN
    return float((a - b) / 2.0)


def credit_spread_metrics(short_leg: Dict, long_leg: Dict, width: float) -> Dict[str, float]:
    """Net credit at mid and at the touch (sell short at bid, buy long at ask), half-spread cost, % of credit, credit/width."""
    sb, sa, lb, la = short_leg.get('b'), short_leg.get('a'), long_leg.get('b'), long_leg.get('a')
    try:
        mid_credit = (sb + sa) / 2.0 - (lb + la) / 2.0
        taker_credit = sb - la
    except TypeError:
        return {k: NAN for k in ('mid_credit', 'taker_credit', 'half_spread_sum', 'spread_pct_credit', 'credit_over_width')}
    hs = leg_half_spread(short_leg) + leg_half_spread(long_leg)
    return {'mid_credit': float(mid_credit), 'taker_credit': float(taker_credit), 'half_spread_sum': float(hs),
            'spread_pct_credit': float(hs / mid_credit) if mid_credit > 0 else NAN,
            'credit_over_width': float(mid_credit / width) if width > 0 else NAN}


def quote_age_seconds(leg: Dict, now_epoch_s: float) -> float:
    stamps = [leg.get(k) for k in ('bd', 'ad') if leg.get(k)]
    return float(max(0.0, now_epoch_s - max(stamps) / 1000.0)) if stamps else NAN


# 90: EV per collateral at fair vol
def fair_credit_spread_value(S: float, K_short: float, K_long: float, T: float, sigma: float, is_put_side: bool) -> float:
    """Risk-neutral (= zero-drift) value of the credit spread at fair vol, i.e. what a seller should expect to pay out
    holding to expiry. Terminal-expiry proxy: P2 exits earlier, so this ranks trades, it does not forecast their ROI."""
    is_call = not is_put_side
    return float(bs_price(S, K_short, T, sigma, is_call) - bs_price(S, K_long, T, sigma, is_call))


def ev_per_collateral(credit: float, width: float, S: float, K_short: float, K_long: float, minutes_to_close: float,
                      sigma_ann: float, is_put_side: bool) -> float:
    if not (credit > 0 and width > credit and np.isfinite(sigma_ann) and sigma_ann > 0):
        return NAN
    fair = fair_credit_spread_value(S, K_short, K_long, T_years(minutes_to_close), sigma_ann, is_put_side)
    return float((credit - fair) / (width - credit))


# ---------------------------------------------------------------------------------------------------------------
# chain helpers
def _rows(rows: Iterable[Dict], t: Optional[str] = None) -> List[Dict]:
    return [r for r in rows if (t is None or r.get('t') == t)]


def nearest_strike_rows(rows: Sequence[Dict], K: float, t: Optional[str] = None) -> Optional[Dict]:
    c = _rows(rows, t)
    return min(c, key=lambda r: abs(r['k'] - K)) if c else None


def atm_iv(rows: Sequence[Dict], spot: float) -> float:
    """Mean of the call and put mid IV at the strike nearest spot."""
    ks = sorted({r['k'] for r in rows}, key=lambda k: abs(k - spot))
    for k in ks[:3]:
        v = [r['iv'] for r in rows if r['k'] == k and r.get('iv') and r['iv'] > 0]
        if v:
            return float(np.mean(v))
    return NAN


def atm_straddle_mid(rows: Sequence[Dict], spot: float) -> float:
    ks = sorted({r['k'] for r in rows}, key=lambda k: abs(k - spot))
    for k in ks[:3]:
        c = [r for r in rows if r['k'] == k and r['t'] == 'c' and r.get('b') is not None and r.get('a')]
        p = [r for r in rows if r['k'] == k and r['t'] == 'p' and r.get('b') is not None and r.get('a')]
        if c and p:
            return float((c[0]['b'] + c[0]['a']) / 2.0 + (p[0]['b'] + p[0]['a']) / 2.0)
    return NAN


def skew_metrics(rows: Sequence[Dict], spot: float, short_strike: Optional[float] = None) -> Dict[str, float]:
    """12, 13: 25-delta risk reversal and butterfly curvature, plus the short strike's IV minus ATM IV."""
    atm = atm_iv(rows, spot)

    def at_delta(t: str, target: float) -> float:
        c = [r for r in rows if r['t'] == t and r.get('d') is not None and r.get('iv') and r['iv'] > 0]
        if not c:
            return NAN
        r = min(c, key=lambda x: abs(abs(x['d']) - target))
        return float(r['iv']) if abs(abs(r['d']) - target) < 0.08 else NAN
    ivc, ivp = at_delta('c', 0.25), at_delta('p', 0.25)
    out = {'atm_iv': atm, 'iv25c': ivc, 'iv25p': ivp, 'rr25': ivc - ivp if np.isfinite(ivc) and np.isfinite(ivp) else NAN,
           'fly25': (ivc + ivp) / 2.0 - atm if np.isfinite(ivc) and np.isfinite(ivp) and np.isfinite(atm) else NAN,
           'short_iv_minus_atm': NAN}
    if short_strike is not None:
        r = nearest_strike_rows(rows, short_strike)
        if r and r.get('iv') and np.isfinite(atm):
            out['short_iv_minus_atm'] = float(r['iv'] - atm)
    return out


def term_slope(rows_near: Sequence[Dict], rows_next: Sequence[Dict], spot: float) -> float:
    """14: ATM IV of the next expiry minus the nearest expiry (positive = normal slope)."""
    a, b = atm_iv(rows_near, spot), atm_iv(rows_next, spot)
    return float(b - a) if np.isfinite(a) and np.isfinite(b) else NAN


# 22-24: dealer positioning proxies from open interest ---------------------------------------------------------------
def gex_from_chain(rows: Sequence[Dict], spot: float, call_sign: int = 1, put_sign: int = -1, mult: int = 100) -> Dict:
    """Net gamma exposure per 1% move using the assumed 'dealers long calls, short puts' convention.
    GEX = sum sign x gamma x OI x mult x S^2 x 0.01. Returns total and the per-strike map. Proxy only: the academic
    results use proprietary trade-direction data (docs/LAB_PROGRAMME.md)."""
    by: Dict[float, float] = {}
    tot = 0.0
    n = 0
    for r in rows:
        g, oi = r.get('g'), r.get('oi')
        if not (g and oi and g > 0 and oi > 0):
            continue
        sgn = call_sign if r['t'] == 'c' else put_sign
        v = sgn * g * oi * mult * spot * spot * 0.01
        by[r['k']] = by.get(r['k'], 0.0) + v
        tot += v
        n += 1
    return {'gex': float(tot) if n else NAN, 'sign': (1 if tot > 0 else (-1 if tot < 0 else 0)) if n else NAN,
            'by_strike': by, 'n_rows': n}


def zero_gamma_level(rows: Sequence[Dict], spot: float, minutes_to_close: float, span: float = 0.05, steps: int = 101) -> float:
    """Spot level at which net dealer gamma (same convention) changes sign, found by re-pricing each option's gamma at
    hypothetical spots using its own IV. nan if there is no sign change inside +/- span."""
    T = T_years(minutes_to_close)
    use = [r for r in rows if r.get('iv') and r['iv'] > 0 and r.get('oi') and r['oi'] > 0]
    if len(use) < 6:
        return NAN
    grid = spot * (1.0 + np.linspace(-span, span, steps))
    tot = np.zeros(steps)
    for r in use:
        g = bs_gamma(grid, r['k'], T, r['iv'])
        tot += (1 if r['t'] == 'c' else -1) * g * r['oi'] * 100 * grid * grid * 0.01
    s = np.sign(tot)
    flips = np.where(np.diff(s) != 0)[0]
    if not len(flips):
        return NAN
    i = min(flips, key=lambda j: abs(grid[j] - spot))
    x0, x1, y0, y1 = grid[i], grid[i + 1], tot[i], tot[i + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0)) if y1 != y0 else float(x0)


def oi_walls(rows: Sequence[Dict], spot: float) -> Dict[str, float]:
    """Call wall = strike at/above spot with the largest call OI; put wall = strike at/below spot with the largest put OI."""
    calls = [r for r in rows if r['t'] == 'c' and r['k'] >= spot and r.get('oi')]
    puts = [r for r in rows if r['t'] == 'p' and r['k'] <= spot and r.get('oi')]
    cw = max(calls, key=lambda r: r['oi']) if calls else None
    pw = max(puts, key=lambda r: r['oi']) if puts else None
    return {'call_wall': float(cw['k']) if cw else NAN, 'call_wall_oi': float(cw['oi']) if cw else NAN,
            'put_wall': float(pw['k']) if pw else NAN, 'put_wall_oi': float(pw['oi']) if pw else NAN}


def wall_coincidence(short_strike: float, walls: Dict[str, float], tol_frac: float = 0.002) -> float:
    """1 when the short strike sits within tol of the OI wall on its side, 0 if not, nan without walls."""
    best = NAN
    for k in ('call_wall', 'put_wall'):
        w = walls.get(k, NAN)
        if np.isfinite(w) and abs(w - short_strike) / short_strike <= tol_frac:
            return 1.0
        if np.isfinite(w):
            best = 0.0
    return best


def vanna_charm_exposure(rows: Sequence[Dict], spot: float, minutes_to_close: float) -> Dict[str, float]:
    """26: net vanna (per vol point) and charm (per hour) exposure, same dealer sign convention. T3 context only."""
    T = T_years(minutes_to_close)
    van = cha = 0.0
    n = 0
    for r in rows:
        iv, oi = r.get('iv'), r.get('oi')
        if not (iv and iv > 0 and oi and oi > 0):
            continue
        sgn = 1 if r['t'] == 'c' else -1
        van += sgn * float(bs_vanna(spot, r['k'], T, iv)) * oi * 100 * 0.01
        cha += sgn * float(bs_charm(spot, r['k'], T, iv, r['t'] == 'c')) * oi * 100 / (252.0 * 6.5)
        n += 1
    return {'vanna_exposure': float(van) if n else NAN, 'charm_exposure_per_hour': float(cha) if n else NAN}


# 28, 29, 66: volume / OI statistics -----------------------------------------------------------------------------
def chain_stats(rows: Sequence[Dict]) -> Dict[str, float]:
    cv = sum((r.get('v') or 0) for r in rows if r['t'] == 'c')
    pv = sum((r.get('v') or 0) for r in rows if r['t'] == 'p')
    co = sum((r.get('oi') or 0) for r in rows if r['t'] == 'c')
    po = sum((r.get('oi') or 0) for r in rows if r['t'] == 'p')
    return {'call_volume': float(cv), 'put_volume': float(pv), 'pc_volume': float(pv / cv) if cv else NAN,
            'call_oi': float(co), 'put_oi': float(po), 'pc_oi': float(po / co) if co else NAN,
            'total_volume': float(cv + pv)}


def zero_dte_volume_share(rows_by_expiry: Dict[str, Sequence[Dict]], today: str) -> float:
    """Volume in today's expiry as a share of the volume across the snapshotted expiries (SPY-level proxy for item 28)."""
    tot = sum(chain_stats(r)['total_volume'] for r in rows_by_expiry.values())
    return float(chain_stats(rows_by_expiry.get(today, []))['total_volume'] / tot) if tot > 0 else NAN


def volume_oi_ratio(row: Optional[Dict]) -> float:
    return float((row.get('v') or 0) / row['oi']) if row and row.get('oi') else NAN


# 104: aggregate greeks across concurrent positions ----------------------------------------------------------------
def aggregate_greeks(positions: Iterable[Dict]) -> Dict[str, float]:
    """positions: dicts with qty (+ long / - short, in contracts), d, g, vg per leg. Returns net delta/gamma/vega in shares."""
    d = g = v = 0.0
    for p in positions:
        q = p.get('qty', 0) * 100
        d += q * (p.get('d') or 0.0)
        g += q * (p.get('g') or 0.0)
        v += q * (p.get('vg') or 0.0)
    return {'net_delta': float(d), 'net_gamma': float(g), 'net_vega': float(v)}
