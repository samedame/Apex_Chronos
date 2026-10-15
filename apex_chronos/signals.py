"""The five entry detectors, ported from analyze_chronos_portfolio.py.

The backtest functions scan a whole day and return the FIRST minute that
satisfies the condition. Here each detector is stepped one minute at a time and
returns the same first-trigger minute: `step(m, f)` is called for every minute
m in order, reads only f.<array>[<= m], and fires at most once per day (after
firing, or being filtered, that ticker/module is finished for the day, exactly
as in the backtest).
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from .features import Features

Fire = Optional[Tuple[int, int]]  # (minute, direction)


class Detector:
    module: str = ''
    lo: int = 0
    hi: int = 0     # exclusive, like range(lo, hi) in the original

    def __init__(self, f0: Features):
        self.done = False

    def in_window(self, m: int) -> bool:
        return self.lo <= m < self.hi

    def step(self, m: int, f: Features) -> Fire:  # pragma: no cover
        raise NotImplementedError


class E1GapTrap(Detector):
    """sig_1m_E1_gap_trap: gap > +/-0.25% then a sweep through the 5-minute opening range against the gap."""
    module = 'P1A'
    lo, hi = 5, 60

    def __init__(self, f0):
        super().__init__(f0)
        self.prev_c = f0.prev_c
        self.gap = (f0.O[0] - f0.prev_c) / f0.prev_c

    def step(self, m, f):
        if not self.in_window(m):
            return None
        if m == self.lo or not hasattr(self, 'orb5_h'):
            # opening range = bars 0..4, all known by minute 5
            self.orb5_h, self.orb5_l = np.max(f.H[:5]), np.min(f.L[:5])
        C, vwap, ema9 = f.C, f.vwap, f.ema9
        if self.gap > 0.0025:
            if C[m] < self.orb5_l and C[m] < vwap[m] and C[m] < ema9[m] and C[m] > self.prev_c:
                return (m, 1)
        elif self.gap < -0.0025:
            if C[m] > self.orb5_h and C[m] > vwap[m] and C[m] > ema9[m] and C[m] < self.prev_c:
                return (m, -1)
        return None


class LG2ConfirmedGap(Detector):
    """sig_1m_LG2_confirmed_gap: gap-up >= 0.35%, three closes above ORB15 high, above noise area, RVOL >= 1.25."""
    module = 'P1B'
    lo, hi = 18, 65

    def __init__(self, f0):
        super().__init__(f0)
        self.gap = (f0.O[0] - f0.prev_c) / f0.prev_c

    def step(self, m, f):
        if not self.in_window(m) or self.gap < 0.0035:
            return None
        if not hasattr(self, 'orb15_h'):
            self.orb15_h = np.max(f.H[:15])
        O, C, vwap, vstd, rvol, nu, ema9, ema21 = f.O, f.C, f.vwap, f.vwap_std, f.rvol_cum, f.noise_upper, f.ema9, f.ema21
        z = (C[m] - vwap[m]) / max(vstd[m], C[m] * 0.0005)
        if (C[m] > self.orb15_h and C[m - 1] > self.orb15_h and C[m - 2] > self.orb15_h and
                C[m] > nu[m] and rvol[m] >= 1.25 and z > 0.35 and ema9[m] >= ema21[m] and C[m] > O[m]):
            return (m, 1)
        return None


class E3Orb15Reject(Detector):
    """sig_1m_E3_orb15_reject: price pokes above the ORB15 high, then closes back below it on a red, lower bar."""
    module = 'P1C'
    lo, hi = 16, 105

    def __init__(self, f0):
        super().__init__(f0)
        self.popped = False

    def step(self, m, f):
        if not self.in_window(m):
            return None
        if not hasattr(self, 'orb15_h'):
            self.orb15_h = np.max(f.H[:15])
        O, H, L, C, ema9, rvol = f.O, f.H, f.L, f.C, f.ema9, f.rvol_cum
        if H[m] > self.orb15_h:
            self.popped = True
        if self.popped and rvol[m] < 1.6 and C[m] < self.orb15_h and C[m] < ema9[m] and C[m] < O[m] and C[m] < L[m - 1]:
            return (m, -1)
        return None


class U5MiddayPin(Detector):
    """sig_1m_U5_midday_pin: rejection candle at the 75-minute morning high/low wall."""
    module = 'P2'
    lo, hi = 75, 240

    def step(self, m, f):
        if not self.in_window(m):
            return None
        if not hasattr(self, 'morn_h'):
            self.morn_h, self.morn_l = np.max(f.H[:75]), np.min(f.L[:75])
        O, H, L, C, ema9 = f.O, f.H, f.L, f.C, f.ema9
        if H[m - 1] >= self.morn_h * 0.9995 and C[m] < self.morn_h and C[m] < O[m] and C[m] < L[m - 1] and C[m] < ema9[m]:
            return (m, -1)
        if L[m - 1] <= self.morn_l * 1.0005 and C[m] > self.morn_l and C[m] > O[m] and C[m] > H[m - 1] and C[m] > ema9[m]:
            return (m, 1)
        return None


class E7PmVanna(Detector):
    """sig_1m_E7_pm_vanna: afternoon flush (< -1.3 SD from open and < -1.1 SD below VWAP) then a reclaim bar."""
    module = 'P3'
    lo, hi = 210, 320

    def __init__(self, f0):
        super().__init__(f0)
        self.flushed = False

    def step(self, m, f):
        if not self.in_window(m):
            return None
        O, H, C, vwap, vstd, ema9, iv = f.O, f.H, f.C, f.vwap, f.vwap_std, f.ema9, f.iv
        day_sd = (C[m] - O[0]) / (O[0] * iv[m] * np.sqrt(m / (390.0 * 252.0)))
        z_vwap = (C[m] - vwap[m]) / max(vstd[m], C[m] * 0.0005)
        if day_sd < -1.3 and z_vwap < -1.1:
            self.flushed = True
        if self.flushed and C[m] > O[m] and C[m] > H[m - 1] and C[m] > ema9[m]:
            return (m, 1)
        return None


DETECTORS = [E1GapTrap, LG2ConfirmedGap, E3Orb15Reject, U5MiddayPin, E7PmVanna]
