"""Per-ticker intraday features, ported from run_1m_options_lab.prepare_ticker_data.

Every array here is CAUSAL: value[i] depends only on bars 0..i (cumsum, ewm
with adjust=False, history baselines from previous sessions). That means the
features computed on the bars seen so far are bit-identical to the first k
entries of the features computed on the whole day. tests/test_features.py
asserts exactly that, and the verification harness re-checks it on every
session in the dataset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from . import config as C


class InsufficientHistory(RuntimeError):
    """Fewer than MIN_HISTORY_SESSIONS prior sessions; the bot refuses to trade that ticker."""


@dataclass
class TickerContext:
    """Everything known BEFORE the session opens for one ticker."""
    ticker: str
    prev_c: float                       # last completed daily close
    rvol20: float                       # 20-day realised vol (annualised), through yesterday
    hist_moves: List[np.ndarray]        # |C/O0-1| profiles of prior sessions (oldest first)
    hist_cumv: List[np.ndarray]         # cumulative-volume profiles of prior sessions (oldest first)


@dataclass
class Features:
    ticker: str
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray
    vix: np.ndarray
    prev_c: float
    vwap: np.ndarray
    vwap_std: np.ndarray
    noise_upper: np.ndarray
    rvol_cum: np.ndarray
    ema9: np.ndarray
    ema21: np.ndarray
    iv: np.ndarray
    n: int = field(init=False)

    def __post_init__(self):
        self.n = len(self.C)


def avg_profile(hist: List[np.ndarray], k: int) -> np.ndarray:
    """Mean of the last 14 history profiles over minutes 0..k-1.

    Mirrors the oracle exactly: shorter profiles are edge-padded, longer ones
    sliced, then np.mean(..., axis=0) over the stack.
    """
    if len(hist) < C.MIN_HISTORY_SESSIONS:
        raise InsufficientHistory(f"only {len(hist)} prior sessions")
    look = hist[-C.HISTORY_SESSIONS:]
    rows = [m[:k] if len(m) >= k else np.pad(m, (0, k - len(m)), 'edge') for m in look]
    return np.mean(rows, axis=0)


def vol_multiplier(ticker: str, rvol20: float) -> float:
    vol_mult = max(1.0, rvol20 / 0.16) if ticker not in ['SPY', 'IWM'] else (1.25 if ticker == 'IWM' else 1.0)
    if ticker == 'QQQ':
        vol_mult = 1.18
    return vol_mult


def compute_features(ctx: TickerContext, O, H, L, C_, V, vix) -> Features:
    """Features for the bars seen so far (arrays of equal length k >= 1)."""
    O = np.asarray(O, dtype=float)
    H = np.asarray(H, dtype=float)
    L = np.asarray(L, dtype=float)
    C_ = np.asarray(C_, dtype=float)
    V = np.asarray(V, dtype=float)
    vix = np.asarray(vix, dtype=float)
    k = len(C_)
    prev_c = ctx.prev_c

    typ = (H + L + C_) / 3.0
    cum_v = np.maximum(np.cumsum(V), 1.0)
    cum_vp = np.cumsum(typ * V)
    vwap = cum_vp / cum_v
    cum_vp2 = np.cumsum((typ ** 2) * V)
    vwap_var = np.maximum(cum_vp2 / cum_v - vwap ** 2, (O[0] * 0.0005) ** 2)
    vwap_std = np.sqrt(vwap_var)

    avg_move_prof = avg_profile(ctx.hist_moves, k)
    upper_ref = max(O[0], prev_c)
    noise_upper = upper_ref * (1.0 + avg_move_prof)

    avg_cum_v = avg_profile(ctx.hist_cumv, k)
    rvol_cum = cum_v / np.maximum(avg_cum_v, 1.0)

    ema9 = pd.Series(C_).ewm(span=9, adjust=False).mean().values
    ema21 = pd.Series(C_).ewm(span=21, adjust=False).mean().values

    vol_mult = vol_multiplier(ctx.ticker, ctx.rvol20)
    base_iv = (vix / 100.0) * vol_mult
    ret_from_open = (C_ - O[0]) / O[0]
    skew_adj = np.where(ret_from_open < 0, -1.5 * ret_from_open, -0.25 * ret_from_open)
    iv = np.clip(base_iv * (1.0 + skew_adj), 0.10, 2.20)

    return Features(ticker=ctx.ticker, O=O, H=H, L=L, C=C_, V=V, vix=vix, prev_c=prev_c,
                    vwap=vwap, vwap_std=vwap_std, noise_upper=noise_upper, rvol_cum=rvol_cum,
                    ema9=ema9, ema21=ema21, iv=iv)


def session_profiles(O, C_, V):
    """(|C/O0-1| profile, cumulative-volume profile) of a COMPLETED session, as the oracle appends to its history."""
    O = np.asarray(O, dtype=float)
    C_ = np.asarray(C_, dtype=float)
    V = np.asarray(V, dtype=float)
    cum_v = np.maximum(np.cumsum(V), 1.0)
    return np.abs(C_ / O[0] - 1.0), cum_v
