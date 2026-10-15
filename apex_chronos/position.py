"""One open model position and its exit rules.

Port of the exit loop in fast_sim.sim_trade_vec, evaluated one bar at a time.
The order of operations inside on_bar() is deliberately identical to the
original loop body (including that the TP1 trigger, the peak update and the
trailing stop all run before the stop-loss test on the same bar).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config as C
from .pricing import Structure, minutes_to_T


class Position:
    def __init__(self, spec: C.ModuleSpec, module_key: str, ticker: str, date: str, m_entry: int,
                 direction: int, S_entry: float, iv_entry: float, n_bars: int, score: float,
                 rvol_entry: float, slot: int):
        self.spec = spec
        self.module_key = module_key
        self.ticker = ticker
        self.date = date
        self.m_entry = int(m_entry)
        self.direction = int(direction)
        self.S_entry = float(S_entry)
        self.iv_entry = float(iv_entry)
        self.score = float(score)
        self.rvol_entry = float(rvol_entry)
        self.slot = int(slot)
        self.n_bars = n_bars

        T_entry = minutes_to_T(self.m_entry)
        self.struct = Structure(ticker, spec.structure, self.direction, S_entry, iv_entry, T_entry, spec.target_delta)
        self.cost_basis = self.struct.cost_basis
        self.m_end = min(n_bars - 5, self.m_entry + spec.max_hold_mins)

        self.peak_roi = 0.0
        self.half_scaled = False
        self.locked_half1 = 0.0
        self.dyn_sl = -spec.sl_pct
        self.closed = False
        self.last_roi_close: Optional[float] = None

    # -- exit loop body (one bar) -------------------------------------------------
    def on_bar(self, m: int, H: float, L: float, Cl: float, iv: float) -> Tuple[List[Dict], Optional[Dict]]:
        """Process bar m (> m_entry). Returns (events, closed_trade_or_None)."""
        assert not self.closed and m > self.m_entry
        is_call = (self.direction == 1)
        S = np.array([L if is_call else H,      # worst
                      H if is_call else L,      # best
                      Cl])                      # close
        T = minutes_to_T(m)
        rw, rb, rc = (self.struct.value(S, T, iv) - self.cost_basis) / self.cost_basis
        rw, rb, rc = float(rw), float(rb), float(rc)
        self.last_roi_close = rc
        tp1 = self.spec.tp1_pct
        events: List[Dict] = []

        if (not self.half_scaled) and (rb >= tp1):
            self.half_scaled = True
            self.locked_half1 = tp1
            self.dyn_sl = tp1 * C.AFTER_TP1_FLOOR_FRAC
            events.append({'kind': 'TP1', 'minute': m, 'model_roi': rb, 'm_entry': self.m_entry})

        if rb > self.peak_roi:
            self.peak_roi = rb
        if self.half_scaled and self.peak_roi > tp1:
            self.dyn_sl = max(self.dyn_sl, self.peak_roi * C.TRAIL_PCT_OF_PEAK)

        if self.half_scaled and rw <= self.dyn_sl:
            return events, self._close(m, 0.5 * self.locked_half1 + 0.5 * self.dyn_sl, 'profit_trail')
        if (not self.half_scaled) and rc <= self.dyn_sl:
            return events, self._close(m, rc, 'stop_loss')
        if m >= self.m_end:
            final = 0.5 * self.locked_half1 + 0.5 * rc if self.half_scaled else rc
            return events, self._close(self.m_end, final, 'time_stop')
        return events, None

    def _close(self, m_exit: int, roi: float, reason: str) -> Dict:
        self.closed = True
        return {
            'ticker': self.ticker, 'date': self.date, 'm_entry': self.m_entry, 'm_exit': int(m_exit),
            'hold_mins': int(m_exit - self.m_entry), 'direction': self.direction,
            'cost_basis': self.cost_basis, 'roi': float(roi), 'peak_roi': float(self.peak_roi),
            'exit_reason': reason, 'rvol_entry': self.rvol_entry, 'module': self.spec.name,
            'phase': self.spec.phase, 'score': self.score, 'slot': self.slot,
        }

    # -- plan shown in the entry alert -------------------------------------------
    def plan(self) -> Dict:
        sp = self.spec
        return {
            'stop_roi': -sp.sl_pct, 'tp1_roi': sp.tp1_pct, 'after_tp1_floor': sp.tp1_pct * C.AFTER_TP1_FLOOR_FRAC,
            'trail_pct_of_peak': C.TRAIL_PCT_OF_PEAK, 'm_end': self.m_end, 'max_hold': sp.max_hold_mins,
        }
