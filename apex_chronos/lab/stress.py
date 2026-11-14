"""Stress-day harness (catalogue item 107).

Input is a list of P2 trades from ANY replay of a stress session (date, ticker, direction, m_entry, m_exit, roi). The repo archive
starts on 2026-10-16, so the real stress sessions (2018-02-05, 2020-03, 2024-08-05, 2025-04-03..09) need bars from elsewhere; this
module scores whatever trade list a replay of those bars produces. Nothing here fetches data.

Deployment gate before acting on any KEEP verdict (docs/LAB_PREREG.md item 15 of the research list):
  (1) no session loses more than 10% of equity at the chosen slot fraction;
  (2) after any session at or below the kill-switch session threshold, the kill switch is tripped for the next session.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List

import numpy as np

from .. import verdict
from . import stats

INDEX = ('SPY', 'QQQ', 'IWM')
MAX_SESSION_LOSS = -0.10


def apply_index_cap(trades: Iterable[Dict]) -> List[Dict]:
    """Static removal of a trade that overlaps an open index-ETF trade (one index slot at a time)."""
    kept: List[Dict] = []
    for t in sorted(trades, key=lambda x: (x['date'], int(x['m_entry']))):
        if t['ticker'] in INDEX and any(o['date'] == t['date'] and o['ticker'] in INDEX and int(o['m_entry']) < int(t['m_entry']) <= int(o['m_exit'])
                                        for o in kept):
            continue
        kept.append(t)
    return kept


def session_returns(trades: Iterable[Dict], slot_fraction: float) -> Dict[str, float]:
    by: Dict[str, float] = defaultdict(float)
    for t in trades:
        by[t['date']] += slot_fraction * float(t['roi'])
    return dict(sorted(by.items()))


def score(trades: List[Dict], slot_fraction: float = verdict.DEFAULT_SLOT_FRACTION) -> Dict:
    out = {}
    for label, tr in (('uncapped', trades), ('index_cap', apply_index_cap(trades))):
        rets = session_returns(tr, slot_fraction)
        vals = list(rets.values())
        rows = [{'date': d, 'phase': 'P2', 'roi': r / slot_fraction} for d, r in rets.items()]
        trips = {}
        running: List[Dict] = []
        for r in rows:
            running.append(r)
            trips[r['date']] = verdict.kill_switch_status(running, slot_fraction, start='0000-00-00')['tripped']
        days = list(rets)
        fired_after_loss = all(trips[d] for d in days if rets[d] <= verdict.KILL_SESSION_LOSS)
        out[label] = {'sessions': rets, 'tail': stats.tail_summary(vals), 'worst': min(vals) if vals else float('nan'),
                      'gate_session_loss_ok': bool(vals) and min(vals) >= MAX_SESSION_LOSS, 'kill_fires_after_loss': fired_after_loss,
                      'trades': len(tr)}
        out[label]['pass'] = out[label]['gate_session_loss_ok'] and fired_after_loss
    return out


def synthetic_crash_trades(day: str = '2099-01-02') -> List[Dict]:
    """Fixture: three concurrent index credit spreads all stopped out for a full loss (the worst a defined-risk book can do in
    one session) plus one ordinary winning day before it."""
    win = [{'date': '2099-01-01', 'ticker': 'AMD', 'direction': 1, 'm_entry': 100, 'm_exit': 150, 'roi': 0.12}]
    loss = [{'date': day, 'ticker': t, 'direction': 1, 'm_entry': 100 + i * 5, 'm_exit': 140, 'roi': -0.95} for i, t in enumerate(INDEX)]
    return win + loss
