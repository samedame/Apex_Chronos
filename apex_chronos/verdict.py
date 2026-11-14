"""Production controls from the 2026-11-14 three-study verdict.

Source: project doc claude/apex-chronos-final-verdict-three-studies-2026-11-14.md (summary in docs/VERDICT_2026-11-14.md).
Three independent optimization studies found no strategy change worth adding; every candidate failed an integrity
check. What the evidence does support is implemented here, and nothing in this module changes a signal, an exit,
the scheduler, the model trades written to ledger/trades.csv, or the frozen Phase 2 rule (phase_review.RULE):

  * P1 and P3 are retired from alerting in code (no repo variable can re-enable them).
  * A P2 entry is only alerted when a same-day expiry is actually listed for that ticker.
  * Every P2 model trade is re-graded at a "fair" implied vol next to the model price (ledger/p2_grading.csv).
  * A kill switch mutes all alerts after a 15% model drawdown or a -3% model session.
  * Default sizing is 10% of the account per slot (3 slots -> at most 30% in same-day options).
  * The final one-minute bars of every session are archived (state/bars/), so the study can be re-run later.

All returns remain MODEL returns (Black-Scholes prices), not fills.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from . import config as C

# ---- settings frozen by the verdict ------------------------------------------------------------------------------
RETIRED_PHASES = frozenset({'P1', 'P3'})   # unanimous across the three studies: no edge, worst forward/live losers
DEFAULT_SLOT_FRACTION = 0.10               # was 0.25 (3 x 25% = 75% of the account in same-day options)
KILL_DRAWDOWN = 0.15                       # model equity more than 15% below its high-water mark
KILL_SESSION_LOSS = -0.03                  # or the latest session lost 3% of model equity
KILL_START = '2026-11-16'                  # same start as the P2 rule: the forward book being judged
FAIR_VRP = 1.2                             # fair IV = 1.2 x realised vol (study B's primary variance premium)
FAIR_IV_FLOOR, FAIR_IV_CAP = 0.05, 2.20

# Same-day expiries seen in the Nasdaq options directory snapshots of 11-13 Nov 2026 (state/cash_options/).
# Used only when no causal catalog snapshot is available.
DAILY_EXPIRY = frozenset({'SPY', 'QQQ', 'IWM'})
FRIDAY_ONLY = frozenset({'COIN', 'MSTR'})
MWF = (0, 2, 4)

GRADING_COLUMNS = ['date', 'ticker', 'm_entry', 'm_exit_model', 'roi_model', 'exit_model', 'listed_same_day',
                   'listing_source', 'iv_model_entry', 'rv_forecast', 'rv_sessions', 'iv_fair_entry', 'roi_fair',
                   'exit_fair', 'm_exit_fair']
FAIR_LOOKBACK, FAIR_MIN_SESSIONS = 14, 5    # study B: mean of the previous 14 sessions' same-window realised variance


# ---- same-day expiry ---------------------------------------------------------------------------------------------
def static_same_day_expiry(ticker: str, d: date) -> bool:
    wd = d.weekday()
    if ticker in DAILY_EXPIRY:
        return True
    if ticker in FRIDAY_ONLY:
        return wd == 4
    return wd in MWF


def listed_same_day(ticker: str, d: date, now: datetime, catalog_dir) -> Tuple[bool, str]:
    """(is a same-day expiry listed for ticker, where the answer came from). Causal: only snapshots observed before now."""
    from . import cash_catalog
    try:
        snapshots = cash_catalog.load_snapshots(catalog_dir, d) if catalog_dir else []
        contracts, _snap, err = cash_catalog.contracts_at(snapshots, ticker, now)
    except Exception as e:                                     # noqa: BLE001 - a damaged catalog falls back, never crashes
        print(f'listing catalog unreadable ({type(e).__name__}); using the static expiry schedule')
        err, contracts = 'missing_causal_catalog', []
    if err == 'missing_causal_catalog':
        return static_same_day_expiry(ticker, d), 'static schedule (no catalog snapshot)'
    if err == 'ticker_absent_from_catalog':
        return False, 'Nasdaq options directory'
    return any(c.get('expiry') == d.isoformat() for c in contracts), 'Nasdaq options directory'


# ---- fair-vol re-grading of P2 -------------------------------------------------------------------------------------
def window_realised_var(close, m_from: int) -> float:
    """Annualised realised variance of 1-minute log returns from bar m_from to the close."""
    c = np.asarray(close[m_from:], dtype=float)
    r = np.diff(np.log(c))
    r = r[np.isfinite(r)]
    if len(r) < 10:
        return float('nan')
    return float(np.sum(r * r) * C.MINUTES_PER_YEAR_DEN / len(r))


def archived_sessions(state_dir: str, before: date, lookback: int = FAIR_LOOKBACK) -> List[Dict]:
    folder = os.path.join(state_dir, 'bars')
    if not os.path.isdir(folder):
        return []
    names = sorted(n for n in os.listdir(folder) if n.endswith('.json.gz') and n[:10] < before.isoformat())
    out = []
    for n in names[-lookback:]:
        b = load_bars(state_dir, date.fromisoformat(n[:10]))
        if b:
            out.append(b)
    return out


def forecast_rest_of_day_vol(prior: List[Dict], ticker: str, m_entry: int) -> Tuple[float, int]:
    """Causal forecast of realised vol from m_entry to the close: sqrt of the mean same-window realised variance over
    the archived prior sessions (study B's estimator). (nan, n) if fewer than FAIR_MIN_SESSIONS sessions exist."""
    v = [window_realised_var(b['tickers'][ticker]['C'], m_entry) for b in prior if ticker in b.get('tickers', {})]
    v = [x for x in v if np.isfinite(x)]
    if len(v) < FAIR_MIN_SESSIONS:
        return float('nan'), len(v)
    return float(math.sqrt(sum(v) / len(v))), len(v)


def regrade(trade: Dict, H, L, Cl, iv_model, scale: float, n_bars: int) -> Dict:
    """Re-run one model trade from its entry with the implied-vol path multiplied by `scale`.

    Same entry minute, direction, strikes rule and exit ladder (position.Position); only the vol input changes.
    scale == 1.0 reproduces the ledger trade exactly (covered by a test).
    """
    from .position import Position
    key = next(k for k, s in C.MODULES.items() if s.name == trade['module'])
    spec = C.MODULES[key]
    iv = np.clip(np.asarray(iv_model, dtype=float) * scale, FAIR_IV_FLOOR, FAIR_IV_CAP) if scale != 1.0 \
        else np.asarray(iv_model, dtype=float)
    m0 = int(trade['m_entry'])
    pos = Position(spec, key, trade['ticker'], trade['date'], m0, int(trade['direction']), float(Cl[m0]), float(iv[m0]),
                   n_bars, float(trade.get('score', 0.0)), float(trade.get('rvol_entry', 0.0)), int(trade.get('slot', 0)))
    for m in range(m0 + 1, n_bars):
        _, closed = pos.on_bar(m, float(H[m]), float(L[m]), float(Cl[m]), float(iv[m]))
        if closed:
            return closed
    raise RuntimeError('regraded trade never closed')


def grade_p2_day(d: date, days: Dict, trades: Iterable[Dict], catalog_dir, session_open_dt: datetime,
                 state_dir: Optional[str] = None, prior: Optional[List[Dict]] = None) -> List[Dict]:
    from .features import compute_features
    rows = []
    feats = {}
    if prior is None:
        prior = archived_sessions(state_dir, d) if state_dir else []
    for t in trades:
        if t.get('phase') != 'P2' or t['ticker'] not in days:
            continue
        td = days[t['ticker']]
        f = feats.get(t['ticker'])
        if f is None:
            f = feats[t['ticker']] = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
        m0 = int(t['m_entry'])
        listed, source = listed_same_day(t['ticker'], d, session_open_dt + timedelta(minutes=m0 + 1), catalog_dir)
        rv, n_rv = forecast_rest_of_day_vol(prior, t['ticker'], m0)
        iv0 = float(f.iv[m0])
        row = {'date': d.isoformat(), 'ticker': t['ticker'], 'm_entry': m0, 'm_exit_model': int(t['m_exit']),
               'roi_model': round(float(t['roi']), 6), 'exit_model': t['exit_reason'], 'listed_same_day': int(listed),
               'listing_source': source, 'iv_model_entry': round(iv0, 4), 'rv_forecast': None, 'rv_sessions': n_rv,
               'iv_fair_entry': None,
               'roi_fair': None, 'exit_fair': None, 'm_exit_fair': None}
        if np.isfinite(rv) and rv > 0 and iv0 > 0:
            scale = FAIR_VRP * rv / iv0
            g = regrade(t, td.H, td.L, td.C, f.iv, scale, len(td.C))
            row.update(rv_forecast=round(rv, 4), iv_fair_entry=round(min(max(iv0 * scale, FAIR_IV_FLOOR), FAIR_IV_CAP), 4),
                       roi_fair=round(float(g['roi']), 6), exit_fair=g['exit_reason'], m_exit_fair=int(g['m_exit']))
        rows.append(row)
    return rows


def write_grading(ledger_dir: str, d: date, rows: List[Dict]) -> str:
    p = os.path.join(ledger_dir, 'p2_grading.csv')
    old = []
    if os.path.exists(p):
        with open(p, newline='') as fh:
            old = [r for r in csv.DictReader(fh) if r['date'] != d.isoformat()]
    allrows = old + rows
    allrows.sort(key=lambda r: (r['date'], int(r['m_entry']), r['ticker']))
    os.makedirs(ledger_dir, exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=GRADING_COLUMNS)
        w.writeheader()
        w.writerows(allrows)
    os.replace(tmp, p)
    return p


def grading_lines(ledger_dir: str, start: Optional[str] = None) -> List[str]:
    start = start or KILL_START
    p = os.path.join(ledger_dir, 'p2_grading.csv')
    if not os.path.exists(p):
        return []
    with open(p, newline='') as fh:
        rows = [r for r in csv.DictReader(fh) if r["date"] >= start]
    if not rows:
        return []
    def mean(xs):
        xs = [float(x) for x in xs if x not in ('', None)]
        return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)
    mm, n = mean(r['roi_model'] for r in rows)
    ml, nl = mean(r['roi_model'] for r in rows if r['listed_same_day'] == '1')
    mf, nf = mean(r['roi_fair'] for r in rows)
    mlf, nlf = mean(r['roi_fair'] for r in rows if r['listed_same_day'] == '1')
    f = lambda v: 'n/a' if v is None else f'{v * 100:+.2f}%'
    return [f"P2 grading since {start}: model {f(mm)} ({n}) · listed same-day expiry only {f(ml)} ({nl}) · "
            f"fair-vol {f(mf)} ({nf}) · listed + fair-vol {f(mlf)} ({nlf}). The P2 rule still judges the model column."]


# ---- kill switch -----------------------------------------------------------------------------------------------------
def kill_switch_status(rows: Iterable[Dict], slot_fraction: float, alerted_phases=frozenset({'P2'}),
                       start: Optional[str] = None) -> Dict:
    start = start or KILL_START
    by_day: Dict[str, float] = {}
    for r in rows:
        if r.get('date', '') < start:
            continue
        by_day.setdefault(r['date'], 0.0)
        if r.get('phase') in alerted_phases:
            by_day[r['date']] += float(r['roi'])
    eq = hwm = 1.0
    dd = 0.0
    last_day, last_ret = None, 0.0
    for d in sorted(by_day):
        ret = slot_fraction * by_day[d]
        eq *= max(1.0 + ret, 0.0)
        hwm = max(hwm, eq)
        dd = 1.0 - eq / hwm
        last_day, last_ret = d, ret
    reasons = []
    if dd > KILL_DRAWDOWN:
        reasons.append(f'model drawdown {dd * 100:.1f}% > {KILL_DRAWDOWN * 100:.0f}%')
    if last_day and last_ret <= KILL_SESSION_LOSS:
        reasons.append(f'{last_day} model session {last_ret * 100:+.1f}% <= {KILL_SESSION_LOSS * 100:.0f}%')
    return {'tripped': bool(reasons), 'reasons': reasons, 'equity': round(eq, 6), 'drawdown': round(dd, 6),
            'last_session': last_day, 'last_session_return': round(last_ret, 6), 'sessions': len(by_day),
            'slot_fraction': slot_fraction, 'start': start}


def _ks_path(state_dir: str) -> str:
    return os.path.join(state_dir, 'kill-switch.json')


def save_kill_switch(state_dir: str, status: Dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    with open(_ks_path(state_dir), 'w') as fh:
        json.dump(status, fh, indent=1, sort_keys=True)


def load_kill_switch(state_dir: str) -> Dict:
    try:
        with open(_ks_path(state_dir)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {'tripped': False, 'reasons': []}


def kill_switch_line(status: Dict) -> str:
    if status.get('tripped'):
        return ("🛑 Kill switch TRIPPED (" + '; '.join(status['reasons']) + "): every alert is muted next session; "
                "the ledger keeps recording. It clears by itself at the first evening review where neither trigger holds.")
    return (f"Kill switch armed: model equity {status.get('equity', 1.0):.3f}, drawdown {status.get('drawdown', 0) * 100:.1f}% "
            f"(trips at {KILL_DRAWDOWN * 100:.0f}% or a {KILL_SESSION_LOSS * 100:.0f}% session; {status.get('sessions', 0)} sessions since {status.get('start', KILL_START)}).")


# ---- raw bar archive -------------------------------------------------------------------------------------------------
def save_bars(state_dir: str, d: date, days: Dict) -> str:
    """Final 1-minute bars of the session (prices to 4 dp, integer volume) plus the aligned VIX series."""
    os.makedirs(os.path.join(state_dir, 'bars'), exist_ok=True)
    payload = {'date': d.isoformat(), 'grid': '09:30..15:59 ET, one row per minute', 'tickers': {}}
    vix = None
    for t, td in sorted(days.items()):
        payload['tickers'][t] = {k: [round(float(x), 4) for x in getattr(td, k)] for k in ('O', 'H', 'L', 'C')}
        payload['tickers'][t]['V'] = [int(x) if np.isfinite(x) else 0 for x in td.V]
        if vix is None:
            vix = [round(float(x), 4) for x in td.vix]
    payload['VIX'] = vix
    p = os.path.join(state_dir, 'bars', f'{d.isoformat()}.json.gz')
    with gzip.open(p, 'wt', encoding='utf-8') as fh:
        json.dump(payload, fh, separators=(',', ':'))
    return p


def load_bars(state_dir: str, d: date) -> Optional[Dict]:
    p = os.path.join(state_dir, 'bars', f'{d.isoformat()}.json.gz')
    if not os.path.exists(p):
        return None
    with gzip.open(p, 'rt', encoding='utf-8') as fh:
        return json.load(fh)
