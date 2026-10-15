"""Black-Scholes option model, ported line-for-line from run_1m_options_lab.py
(bs_option_price_and_delta, select_strike) and fast_sim.py (structure pricing).

The backtest never used real option quotes: every entry cost and every exit
decision comes from this model, so the live bot uses the same model. Real fills
will differ; see docs/LIVE_VS_BACKTEST.md.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

from . import config as C


def bs_option_price_and_delta(S, K, T_years, r, sigma, is_call=True):
    T = np.maximum(T_years, 1.0 / (390.0 * 252.0 * 60.0))  # min 1 second
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        nd1 = norm.cdf(d1)
        nd2 = norm.cdf(d2)
        price = S * nd1 - K * np.exp(-r * T) * nd2
        delta = nd1
    else:
        nd1 = norm.cdf(-d1)
        nd2 = norm.cdf(-d2)
        price = K * np.exp(-r * T) * nd2 - S * nd1
        delta = -nd1
    return np.maximum(price, 0.01), delta


def select_strike(S, T_years, sigma, target_abs_delta, is_call=True, ticker='SPY'):
    T = np.maximum(T_years, 1e-5)
    if is_call:
        d1_target = norm.ppf(np.clip(target_abs_delta, 0.05, 0.95))
    else:
        d1_target = -norm.ppf(np.clip(target_abs_delta, 0.05, 0.95))
    K_raw = S * np.exp(-d1_target * sigma * np.sqrt(T) + 0.5 * sigma * sigma * T)
    if ticker in C.STEP1_NAMES:
        step = 1.0
    elif S < 400:
        step = 1.0
    else:
        step = 2.5
    return np.round(K_raw / step) * step


def minutes_to_T(m, base_dte=0.0):
    """Years to expiry used by the backtest at bar index m (0DTE: close = bar 390, +25 min cushion)."""
    return (base_dte * 390.0 + (390.0 - m) + 25.0) / (390.0 * 252.0)


def half_spread_for(ticker: str) -> float:
    return C.HALF_SPREAD_TIGHT if ticker in C.TIGHT_SPREAD_NAMES else C.HALF_SPREAD_WIDE


class Structure:
    """An option structure priced the way fast_sim.sim_trade_vec prices it.

    Strikes and entry cost are fixed at entry; value(S, T, iv) re-prices the
    structure at any later underlying/time/vol (vectorised over S, T, iv).
    """

    def __init__(self, ticker, structure, direction, S_entry, iv_entry, T_entry, target_delta):
        self.ticker = ticker
        self.structure = structure
        self.direction = direction
        self.is_call = (direction == 1)
        self.hs = half_spread_for(ticker)
        r = C.RISK_FREE
        is_call = self.is_call
        hs = self.hs
        self.K1 = self.K2 = None
        self.credit = None
        self.width = None
        if structure == 'single':
            self.K1 = select_strike(S_entry, T_entry, iv_entry, target_delta, is_call, ticker)
            mid_entry, _ = bs_option_price_and_delta(S_entry, self.K1, T_entry, r, iv_entry, is_call)
            self.cost_basis = max(float(mid_entry) * (1.0 + hs) + 0.01, 0.15)
        elif structure == 'debit_spread':
            K1 = select_strike(S_entry, T_entry, iv_entry, 0.58, is_call, ticker)
            K2 = select_strike(S_entry, T_entry, iv_entry, 0.28, is_call, ticker)
            if K1 == K2:
                K2 = K1 + (2.0 if is_call else -2.0)
            p1, _ = bs_option_price_and_delta(S_entry, K1, T_entry, r, iv_entry, is_call)
            p2, _ = bs_option_price_and_delta(S_entry, K2, T_entry, r, iv_entry * 1.01, is_call)
            self.K1, self.K2 = K1, K2
            self.cost_basis = max(float(p1 - p2) * (1.0 + hs) + 0.015, 0.20)
        elif structure == 'credit_spread':
            opp_call = not is_call
            K1 = select_strike(S_entry, T_entry, iv_entry, 0.32, opp_call, ticker)
            K2 = select_strike(S_entry, T_entry, iv_entry, 0.10, opp_call, ticker)
            if K1 == K2:
                K2 = K1 + (2.0 if opp_call else -2.0)
            p1, _ = bs_option_price_and_delta(S_entry, K1, T_entry, r, iv_entry, opp_call)
            p2, _ = bs_option_price_and_delta(S_entry, K2, T_entry, r, iv_entry * 1.02, opp_call)
            self.K1, self.K2 = K1, K2
            self.credit = max(float(p1 - p2) * (1.0 - hs) - 0.015, 0.12)
            self.width = abs(K2 - K1)
            self.cost_basis = max(self.width - self.credit, 0.25)
        else:
            raise ValueError(structure)
        self.K1 = float(self.K1)
        self.K2 = None if self.K2 is None else float(self.K2)

    def value(self, S_v, T_v, iv_v):
        r = C.RISK_FREE
        hs = self.hs
        is_call = self.is_call
        if self.structure == 'single':
            mid, _ = bs_option_price_and_delta(S_v, self.K1, T_v, r, iv_v, is_call)
            return np.maximum(mid * (1.0 - hs) - 0.01, 0.0)
        if self.structure == 'debit_spread':
            p1, _ = bs_option_price_and_delta(S_v, self.K1, T_v, r, iv_v, is_call)
            p2, _ = bs_option_price_and_delta(S_v, self.K2, T_v, r, iv_v * 1.01, is_call)
            return np.maximum(np.maximum(p1 - p2, 0.0) * (1.0 - hs) - 0.015, 0.0)
        opp_call = not is_call
        p1, _ = bs_option_price_and_delta(S_v, self.K1, T_v, r, iv_v, opp_call)
        p2, _ = bs_option_price_and_delta(S_v, self.K2, T_v, r, iv_v * 1.02, opp_call)
        debit_close = np.maximum(p1 - p2, 0.0) * (1.0 + hs) + 0.015
        return np.maximum(self.cost_basis + (self.credit - debit_close), 0.0)

    def roi(self, S_v, T_v, iv_v):
        return (self.value(S_v, T_v, iv_v) - self.cost_basis) / self.cost_basis

    def describe(self):
        """Human description of the legs (model strikes on the model's strike grid)."""
        side = 'CALL' if self.is_call else 'PUT'
        if self.structure == 'single':
            return f"BUY {self.K1:g} {side}"
        if self.structure == 'debit_spread':
            return f"BUY {self.K1:g} {side} / SELL {self.K2:g} {side} (debit spread)"
        opp = 'CALL' if not self.is_call else 'PUT'
        return f"SELL {self.K1:g} {opp} / BUY {self.K2:g} {opp} ({'call' if opp == 'CALL' else 'put'} credit spread)"
