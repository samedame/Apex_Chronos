"""Session engine: signals -> candidates -> 3-slot scheduler -> positions.

This is the heart of the bot AND of the verification: the historical replay and
the live loop both call SessionEngine.step(m, ...) minute by minute. The
scheduling rules are a causal restatement of run_apex_chronos_final.schedule_chronos:

  * candidates are ranked by (entry minute, score desc, original loop order)
  * at most 4 Phase-1, 2 Phase-2, 1 Phase-3 trades per day
  * at most one trade per (ticker, phase) per day
  * 3 capital slots; a slot frees the minute AFTER its trade exits
    (a new entry in the exit minute itself is not allowed -> entries are
    processed before that minute's exits)
  * a ticker/module that fires but is filtered out or blocked is finished for
    the day (the original only ever looked at the first trigger)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import numpy as np

from . import config as C
from .features import Features
from .position import Position
from .signals import DETECTORS


@dataclass
class Event:
    kind: str                  # ENTRY | TP1 | EXIT | SKIP
    ticker: str
    module: str                # ledger module name
    minute: int
    data: Dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.ticker}|{self.module}|{self.minute}"


@dataclass
class _Cand:
    m_entry: int
    score: float
    ti: int
    key: str
    ticker: str
    direction: int
    rvol_entry: float
    S_entry: float
    iv_entry: float


class SessionEngine:
    def __init__(self, date: str, tickers: List[str], n_bars=C.SESSION_BARS,
                 n_slots: int = C.N_SLOTS, phase_limits: Optional[Dict[str, int]] = None):
        self.date = date
        self.tickers = list(tickers)
        # n_bars: int for every ticker, or {ticker: bars} (the original uses each ticker-day's own row count)
        self._nb = dict(n_bars) if isinstance(n_bars, dict) else {t: int(n_bars) for t in self.tickers}
        self.n_bars = max(self._nb.values())
        self.n_slots = n_slots
        self.phase_limits = dict(phase_limits or C.PHASE_LIMITS)
        self.dets: Dict[str, list] = {}
        self.pending: Dict[int, List[tuple]] = {}   # entry minute -> [(ticker, module_key, direction)]
        self.slot_free = [-1] * n_slots             # minute from which each slot may be re-used
        self.slot_pos: List[Optional[Position]] = [None] * n_slots
        self.p_counts = {'P1': 0, 'P2': 0, 'P3': 0}
        self.used_tkr_phase: Set[tuple] = set()
        self.open: List[Position] = []
        self.trades: List[Dict] = []
        self.candidate_log: List[Dict] = []        # every candidate that reached the scheduler (for verification)
        self.last_minute = -1

    # ------------------------------------------------------------------------------
    def step(self, m: int, feats: Dict[str, Features], degraded: Set[str] = frozenset()) -> List[Event]:
        """Advance to minute m (after bar m has closed). Must be called for m = 0,1,2,... in order."""
        if m != self.last_minute + 1:
            raise ValueError(f"engine stepped out of order: got {m}, expected {self.last_minute + 1}")
        self.last_minute = m
        events: List[Event] = []
        cands: List[_Cand] = []

        # 1) detectors (stateful ones need every in-window minute)
        for ti, tkr in enumerate(self.tickers):
            f = feats.get(tkr)
            if f is None or f.n <= m:
                continue
            dets = self.dets.get(tkr)
            if dets is None:
                dets = self.dets[tkr] = [cls(f) for cls in DETECTORS]
            for det in dets:
                if det.done or not det.in_window(m):
                    continue
                fire = det.step(m, f)
                if fire is None:
                    continue
                det.done = True                       # first trigger only, win or lose
                if tkr in degraded:
                    events.append(Event('SKIP', tkr, C.MODULES[det.module].name, m,
                                        {'reason': 'signal fired on a minute with degraded data; skipped (fail-closed)'}))
                    continue
                c = self._on_signal(ti, tkr, det.module, m, fire[1], f)
                if c is not None and c.m_entry == m:
                    cands.append(c)

        # pending candidates (Phase 1B waits for the 5-minute boundary)
        for (tkr, key, direction) in self.pending.pop(m, []):
            f = feats.get(tkr)
            if f is None or f.n <= m:
                continue
            spec = C.MODULES[key]
            if m >= self._nb[tkr] - 15 or m < 1:
                continue
            rvol = float(f.rvol_cum[m])
            cands.append(_Cand(m, spec.score_base + spec.score_sign * rvol, self.tickers.index(tkr), key, tkr,
                               direction, rvol, float(f.C[m]), float(f.iv[m])))

        # 2) schedule (entries BEFORE this minute's exits)
        for c in cands:
            self.candidate_log.append({'ticker': c.ticker, 'date': self.date, 'module': C.MODULES[c.key].name, 'm_entry': c.m_entry,
                                       'direction': c.direction, 'score': c.score, 'rvol_entry': c.rvol_entry})
        cands.sort(key=lambda c: (-c.score, c.ti, C.MODULES[c.key].order))
        for c in cands:
            spec = C.MODULES[c.key]
            ph = spec.phase
            if self.p_counts[ph] >= self.phase_limits[ph]:
                continue
            if (c.ticker, ph) in self.used_tkr_phase:
                continue
            for s in range(self.n_slots):
                if m >= self.slot_free[s]:
                    pos = Position(spec, c.key, c.ticker, self.date, m, c.direction, c.S_entry, c.iv_entry,
                                   self._nb[c.ticker], c.score, c.rvol_entry, s)
                    self.slot_free[s] = 10 ** 9           # occupied until it exits
                    self.slot_pos[s] = pos
                    self.p_counts[ph] += 1
                    self.used_tkr_phase.add((c.ticker, ph))
                    self.open.append(pos)
                    events.append(Event('ENTRY', c.ticker, spec.name, m, self._entry_data(pos, feats[c.ticker])))
                    break

        # 3) exits for positions opened on earlier minutes
        for pos in list(self.open):
            if pos.m_entry >= m:
                continue
            f = feats.get(pos.ticker)
            if f is None or f.n <= m:
                raise RuntimeError(f"missing bar {m} for open position {pos.ticker}")
            evs, closed = pos.on_bar(m, float(f.H[m]), float(f.L[m]), float(f.C[m]), float(f.iv[m]))
            for e in evs:
                events.append(Event(e['kind'], pos.ticker, pos.spec.name, m, {**e, 'slot': pos.slot}))
            if closed is not None:
                self.open.remove(pos)
                self.slot_free[pos.slot] = closed['m_exit'] + C.SLOT_REUSE_DELAY
                self.slot_pos[pos.slot] = None
                self.trades.append(closed)
                events.append(Event('EXIT', pos.ticker, pos.spec.name, closed['m_exit'], {'trade': closed}))
        return events

    # ------------------------------------------------------------------------------
    def _on_signal(self, ti, tkr, key, m, direction, f: Features) -> Optional[_Cand]:
        spec = C.MODULES[key]
        rvol = float(f.rvol_cum[m])
        if key == 'P1A':
            retrace = abs(f.C[m] - f.O[0]) / max(abs(f.O[0] - f.prev_c), 1e-4)
            if not (m <= C.P1A_MAX_MINUTE and retrace < C.P1A_MAX_RETRACE and f.rvol_cum[m] <= C.P1A_MAX_RVOL):
                return None
        elif key == 'P1B':
            m5 = min(self._nb[tkr] - 20, m + (4 - (m % 5)))
            if m5 > m:
                self.pending.setdefault(m5, []).append((tkr, key, direction))
                return None
            m = m5
            rvol = float(f.rvol_cum[m])
        elif key == 'P1C':
            if not (C.P1C_RVOL_MIN <= f.rvol_cum[m] < C.P1C_RVOL_MAX and f.C[m] >= f.vwap[m]):
                return None
        elif key == 'P2':
            if not (f.rvol_cum[m] >= C.P2_MIN_RVOL):
                return None
        # P3: no post-filter
        if m >= self._nb[tkr] - 15 or m < 1:         # sim_trade_vec returns None -> never a candidate
            return None
        return _Cand(m, spec.score_base + spec.score_sign * rvol, ti, key, tkr, direction, rvol,
                     float(f.C[m]), float(f.iv[m]))

    def _entry_data(self, pos: Position, f: Features) -> Dict:
        st = pos.struct
        return {
            'slot': pos.slot, 'direction': pos.direction, 'module_key': pos.module_key, 'phase': pos.spec.phase,
            'structure': st.structure, 'legs': st.describe(), 'K1': st.K1, 'K2': st.K2,
            'S_entry': pos.S_entry, 'iv_entry': pos.iv_entry, 'cost_basis': pos.cost_basis,
            'rvol_entry': pos.rvol_entry, 'score': pos.score, 'plan': pos.plan(),
            'credit': st.credit, 'width': st.width,
        }

    def snapshot(self) -> Dict:
        return {'date': self.date, 'last_minute': self.last_minute,
                'open': [(p.ticker, p.spec.name, p.slot, p.m_entry) for p in self.open],
                'p_counts': dict(self.p_counts), 'trades': len(self.trades)}
