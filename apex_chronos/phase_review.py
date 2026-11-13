"""Per-phase forward record and the pre-registered Phase 2 keep/retire rule.

Nothing here changes a signal. It reads ledger/trades.csv (the model trades from each day's final-bar replay)
and answers two questions for the end-of-day post and the live loop:

  * how has each phase done since go-live, and
  * where does Phase 2 stand against the rule frozen in docs/P2_DECISION.md on 2026-11-13.

The rule's numbers live in RULE and are covered by a test, so they cannot drift without a visible diff.
All returns are MODEL ROI on cost or collateral (Black-Scholes prices), not fills.
"""
from __future__ import annotations

import random
from typing import Dict, Iterable, List, Optional

from . import config as C

PHASE_OF_MODULE = {spec.name: spec.phase for spec in C.MODULES.values()}

# Frozen 2026-11-13, before any trade it will be judged on existed. See docs/P2_DECISION.md.
RULE = {
    'phase': 'P2',
    'start_date': '2026-11-16',     # first session after the freeze; the 40 earlier trades picked P2, so they cannot confirm it
    'n_required': 100,              # decide on exactly the first 100 new trades, not sooner and not later
    'keep_min_mean': 0.01,          # +1.0% of collateral: about what crossing the bid/ask twice costs a real spread
    'keep_lower_bound_above': 0.0,  # day-clustered bootstrap, one-sided 95% lower bound of the mean
    'stop_cum_floor': -1.00,        # the new trades sum to -100% of collateral at any point: stop early
    'bootstrap_draws': 10000,
    'seed': 20261113,
}

COLLECTING, KEEP, RETIRE, STOPPED = 'COLLECTING', 'KEEP', 'RETIRE', 'RETIRE (stopped early)'


def phase_of(module: str) -> Optional[str]:
    return PHASE_OF_MODULE.get(module)


def parse_muted(raw: Optional[str]) -> frozenset:
    """'P1,P3' -> {'P1', 'P3'}. Empty, 'none' or 'off' mutes nothing. Unknown names are an error, not a silent no-op."""
    raw = (raw or '').strip()
    if raw.lower() in ('', 'none', 'off', '0', 'false'):
        return frozenset()
    names = {x.strip().upper() for x in raw.split(',') if x.strip()}
    unknown = names - set(C.PHASE_LIMITS)
    if unknown:
        raise ValueError(f"MUTED_PHASES has unknown phase(s) {sorted(unknown)}; valid: {sorted(C.PHASE_LIMITS)}")
    return frozenset(names)


def by_phase(rows: Iterable[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for ph in sorted(C.PHASE_LIMITS):
        r = [float(x['roi']) for x in rows if x.get('phase') == ph]
        out[ph] = {'n': len(r), 'mean': (sum(r) / len(r)) if r else None,
                   'win_rate': (sum(1 for v in r if v > 0) / len(r)) if r else None}
    return out


def _clustered_lower_bound(days: List[List[float]], draws: int, seed: int, level: float = 0.95) -> Optional[float]:
    """One-sided lower bound of the mean ROI per trade, resampling whole sessions so same-day trades stay together."""
    if len(days) < 2:
        return None
    rng = random.Random(seed)
    k = len(days)
    means = []
    for _ in range(draws):
        tot = cnt = 0.0
        for _ in range(k):
            d = days[rng.randrange(k)]
            tot += sum(d)
            cnt += len(d)
        means.append(tot / cnt)
    means.sort()
    return means[int((1 - level) * draws)]


def p2_review(rows: Iterable[dict], rule: dict = RULE) -> dict:
    """Status of the Phase 2 confirmation sample. Deterministic for a given ledger (seeded bootstrap)."""
    new = sorted((x for x in rows if x.get('phase') == rule['phase'] and x['date'] >= rule['start_date']),
                 key=lambda x: (x['date'], int(x['m_entry']), x['ticker']))
    total = len(new)
    sample = new[:rule['n_required']]                    # the verdict is fixed by the first n_required trades
    r = [float(x['roi']) for x in sample]
    cum = low = 0.0
    hit_floor_at = None
    for i, v in enumerate(r, 1):
        cum += v
        low = min(low, cum)
        if hit_floor_at is None and cum <= rule['stop_cum_floor']:
            hit_floor_at = i
    n = len(r)
    mean = (sum(r) / n) if n else None
    res = {'n': n, 'n_required': rule['n_required'], 'trades_since_start': total, 'mean': mean, 'cum': cum, 'worst_cum': low,
           'win_rate': (sum(1 for v in r if v > 0) / n) if n else None, 'lower_bound': None, 'start_date': rule['start_date']}
    if hit_floor_at is not None:
        res['status'] = STOPPED
        res['why'] = (f"the new trades summed to {rule['stop_cum_floor'] * 100:.0f}% of collateral or worse after {hit_floor_at} trades")
        return res
    if n < rule['n_required']:
        res['status'] = COLLECTING
        res['why'] = f"{n} of {rule['n_required']} new trades; no verdict before the full sample"
        return res
    by_day: Dict[str, List[float]] = {}
    for x in sample:
        by_day.setdefault(x['date'], []).append(float(x['roi']))
    lb = _clustered_lower_bound(list(by_day.values()), rule['bootstrap_draws'], rule['seed'])
    res['lower_bound'] = lb
    ok = mean >= rule['keep_min_mean'] and lb is not None and lb > rule['keep_lower_bound_above']
    res['status'] = KEEP if ok else RETIRE
    res['why'] = (f"mean {mean * 100:+.2f}% (needs at least {rule['keep_min_mean'] * 100:+.1f}%), "
                  f"lower bound {'n/a' if lb is None else f'{lb * 100:+.2f}%'} (needs above {rule['keep_lower_bound_above'] * 100:.0f}%)")
    return res


def p2_is_retired(rows: Iterable[dict], rule: dict = RULE) -> bool:
    return p2_review(rows, rule)['status'] in (RETIRE, STOPPED)


def eod_lines(rows: List[dict], muted: frozenset, rule: dict = RULE) -> List[str]:
    """Two lines for the end-of-day post: the record by phase, and where the P2 decision stands."""
    parts = []
    for ph, s in by_phase(rows).items():
        tag = 'muted, logged only' if ph in muted else 'alerted'
        if s['n']:
            parts.append(f"{ph} {s['n']} trades · {s['mean'] * 100:+.1f}% · {s['win_rate'] * 100:.0f}% wins ({tag})")
        else:
            parts.append(f"{ph} 0 trades ({tag})")
    rv = p2_review(rows, rule)
    mean = 'n/a' if rv['mean'] is None else f"{rv['mean'] * 100:+.2f}%"
    line = (f"P2 decision (docs/P2_DECISION.md): {rv['n']} of {rv['n_required']} new model trades since {rv['start_date']} · "
            f"mean {mean} · sum {rv['cum'] * 100:+.0f}% (stop at {rule['stop_cum_floor'] * 100:.0f}%) -> **{rv['status']}**")
    if rv['status'] != COLLECTING:
        line += f" - {rv['why']}."
    if rv['status'] in (RETIRE, STOPPED):
        line += " P2 alerts are muted from the next session; the ledger keeps recording."
    if rv['status'] == KEEP:
        line += " P2 stays a paper alert and may go to the real-quote gate. This is not a signal to fund it."
    return ["By phase since go-live (model prices, not fills): " + " | ".join(parts), line]
