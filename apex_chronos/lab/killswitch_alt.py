"""Alternative kill-switch rules, evaluated and logged, never applied (catalogue item 106).

The live kill switch (verdict.kill_switch_status) is untouched. These rules are scored on the same ledger so the February
verdict can say whether any of them would have cut the tail without a significant mean loss. Judge them on worst session and
CVaR95 of session P&L, not on mean return.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

NAN = float('nan')


def session_pnl(rows: Iterable[Dict], phases=('P2',), slot_fraction: float = 0.10) -> Dict[str, float]:
    by: Dict[str, float] = {}
    for r in rows:
        if r.get('phase') in phases:
            by[r['date']] = by.get(r['date'], 0.0) + slot_fraction * float(r['roi'])
    return dict(sorted(by.items()))


def rule_consecutive_stops(trades: Sequence[Dict], n: int = 3) -> bool:
    """Tripped when the last n P2 trades (ledger order) all ended in a stop-out."""
    p2 = [t for t in trades if t.get('phase') == 'P2']
    return len(p2) >= n and all(t.get('exit_reason') == 'stop_loss' for t in p2[-n:])


def rule_equity_below_mean(trades: Sequence[Dict], window: int = 20) -> bool:
    """Tripped when cumulative P2 ROI sits below its own trailing `window`-trade mean (equity curve under its average)."""
    p2 = [float(t['roi']) for t in trades if t.get('phase') == 'P2']
    if len(p2) < window + 1:
        return False
    eq = np.cumsum(p2)
    return bool(eq[-1] < np.mean(eq[-window:]))


def rule_vix1d_spike(vix1d_at_open: float, median20: float, k: float = 1.5) -> bool:
    return bool(math.isfinite(vix1d_at_open) and math.isfinite(median20) and median20 > 0 and vix1d_at_open > k * median20)


def rule_vix_term_inverted(vix: float, vix3m: float) -> bool:
    return bool(math.isfinite(vix) and math.isfinite(vix3m) and vix3m > 0 and vix / vix3m > 1.0)


def evaluate_all(trades: Sequence[Dict], vix1d: float = NAN, vix1d_median20: float = NAN, vix: float = NAN, vix3m: float = NAN) -> Dict[str, bool]:
    return {'consecutive_stops_3': rule_consecutive_stops(trades, 3), 'equity_below_20trade_mean': rule_equity_below_mean(trades, 20),
            'vix1d_gt_1_5x_median20': rule_vix1d_spike(vix1d, vix1d_median20), 'vix_over_vix3m_gt_1': rule_vix_term_inverted(vix, vix3m)}


def replay_rule(session_pnl_by_day: Dict[str, float], tripped_by_day: Dict[str, bool]) -> Dict[str, float]:
    """Tail outcome if every session flagged `tripped` the evening before (flag for day d is known before d) had been skipped.
    Reports the original and rule-adjusted worst session, CVaR95 and mean so the trade-off is explicit."""
    from .stats import tail_summary
    days = sorted(session_pnl_by_day)
    base = [session_pnl_by_day[d] for d in days]
    kept = [0.0 if tripped_by_day.get(d, False) else session_pnl_by_day[d] for d in days]
    b, k = tail_summary(base), tail_summary(kept)
    return {'base_mean': b['mean'], 'base_worst': b['worst'], 'base_cvar95': b['cvar95'], 'rule_mean': k['mean'], 'rule_worst': k['worst'],
            'rule_cvar95': k['cvar95'], 'sessions_skipped': float(sum(1 for d in days if tripped_by_day.get(d, False)))}


def log_status(state_dir: str, date_iso: str, status: Dict[str, bool]) -> str:
    p = Path(state_dir) / 'lab' / 'kill_alt.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if p.exists():
        try:
            cur = json.loads(p.read_text())
        except ValueError:
            cur = {}
    cur[date_iso] = status
    p.write_text(json.dumps(cur, sort_keys=True, indent=1))
    return str(p)
