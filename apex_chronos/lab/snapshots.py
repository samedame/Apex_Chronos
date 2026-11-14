"""Option-chain snapshots (catalogue items 2, 12-14, 22-24, 26, 28, 29, 62, 66, 88-94 data).

At fixed times the live session stores, for every ticker that lists a same-day expiry, the same-day chain (and the next
expiry for term slope, plus XSP next to SPY): strike, bid/ask and sizes, volume, open interest, greeks, and the quote
timestamps. Prior-close open interest (for the GEX proxy) is the 09:45 snapshot, taken before the day's trading has changed it
materially. Read-only: only /markets/ endpoints are called (tradier.py refuses anything else).
"""
from __future__ import annotations

import gzip
import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .. import config as C, tradier, verdict
from ..calendar_nyse import NY

SNAPSHOT_LABELS = ['0945', '1000', '1130', '1230', '1330', '1430', '1530']   # ET clock times; 0925 is the cross-asset capture
BAND = {'default': 0.03, 'COIN': 0.08, 'MSTR': 0.08, 'TSLA': 0.06, 'NVDA': 0.05, 'AMD': 0.05, 'META': 0.05, 'AMZN': 0.05}
SIZE_UNIT = 'unverified'        # Tradier option bidsize/asksize units are NOT confirmed; flip to 'contracts' only after a check


def occ_symbol(root: str, expiry: date, is_call: bool, strike: float) -> str:
    return f"{root.upper()}{expiry:%y%m%d}{'C' if is_call else 'P'}{int(round(strike * 1000)):08d}"


def compact_option(o: Dict) -> Optional[Dict]:
    try:
        g = o.get('greeks') or {}
        row = {'k': float(o['strike']), 't': 'c' if str(o['option_type']).lower().startswith('c') else 'p',
               'b': _f(o.get('bid')), 'a': _f(o.get('ask')), 'bs': _i(o.get('bidsize')), 'as': _i(o.get('asksize')),
               'v': _i(o.get('volume')), 'oi': _i(o.get('open_interest')), 'last': _f(o.get('last')),
               'iv': _f(g.get('mid_iv')), 'biv': _f(g.get('bid_iv')), 'aiv': _f(g.get('ask_iv')),
               'd': _f(g.get('delta')), 'g': _f(g.get('gamma')), 'th': _f(g.get('theta')), 'vg': _f(g.get('vega')),
               'bd': _i(o.get('bid_date')), 'ad': _i(o.get('ask_date')), 's': o.get('symbol')}
        return row
    except (KeyError, TypeError, ValueError):
        return None


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def snapshot_ticker(root: str, today: date, n_expiries: int = 2, env=None, band: Optional[float] = None) -> Dict:
    """{'spot', 'quote', 'expiries': {iso: [rows]}} for one root. Raises tradier.TradierError on a hard failure."""
    q = tradier.quotes([root], env=env).get(root.upper())
    if not q:
        raise tradier.TradierError(f'no quote for {root}')
    spot = _f(q.get('last')) or _f(q.get('bid')) or _f(q.get('close'))
    exps = [e for e in tradier.expirations(root, env=env) if e >= today.isoformat()]
    chosen = exps[:n_expiries]
    b = band if band is not None else BAND.get(root.upper(), BAND['default'])
    out = {'spot': spot, 'quote': {k: q.get(k) for k in ('last', 'bid', 'ask', 'bidsize', 'asksize', 'trade_date', 'bid_date', 'ask_date')},
           'expiries': {}, 'has_same_day': today.isoformat() in chosen}
    for e in chosen:
        rows = []
        for o in tradier.chain(root, e, greeks=True, env=env):
            r = compact_option(o)
            if r and spot and abs(r['k'] / spot - 1.0) <= b:
                rows.append(r)
        out['expiries'][e] = rows
    return out


def snapshot_roots(tickers: Iterable[str]) -> List[str]:
    roots = list(tickers)
    if 'SPY' in roots and 'XSP' not in roots:
        roots.append('XSP')                    # 94: side-by-side with every SPY alert
    return roots


def path_for(state_dir: str, d: date, label: str) -> Path:
    return Path(state_dir) / 'lab' / 'chains' / d.isoformat() / f'{label}.json.gz'


def take_snapshot(state_dir: str, d: date, label: str, tickers: Iterable[str], env=None, force: bool = False,
                  now: Optional[datetime] = None) -> Dict:
    """Write one snapshot file with every root. Per-root failures are recorded in `errors` and never raise."""
    p = path_for(state_dir, d, label)
    if p.exists() and not force:
        return {'path': str(p), 'skipped': 'exists'}
    now = now or datetime.now(NY)
    payload = {'date': d.isoformat(), 'label': label, 'taken_at': now.isoformat(timespec='seconds'), 'size_unit': SIZE_UNIT,
               'roots': {}, 'errors': {}}
    for root in snapshot_roots(tickers):
        if root != 'XSP' and not verdict.static_same_day_expiry(root, d):
            payload['errors'][root] = 'no same-day expiry by schedule'
            continue
        try:
            payload['roots'][root] = snapshot_ticker(root, d, env=env)
        except Exception as e:                                          # noqa: BLE001 - lab data never breaks a session
            payload['errors'][root] = f'{type(e).__name__}: {str(e)[:80]}'
    if not payload['roots']:
        return {'path': None, 'errors': payload['errors']}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + '.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
        json.dump(payload, fh, separators=(',', ':'))
    os.replace(tmp, p)
    return {'path': str(p), 'roots': sorted(payload['roots']), 'errors': payload['errors']}


def load_snapshot(state_dir: str, d: date, label: str) -> Optional[Dict]:
    p = path_for(state_dir, d, label)
    if not p.exists():
        return None
    with gzip.open(p, 'rt', encoding='utf-8') as fh:
        return json.load(fh)


def labels_available(state_dir: str, d: date) -> List[str]:
    folder = Path(state_dir) / 'lab' / 'chains' / d.isoformat()
    return sorted(p.name.split('.')[0] for p in folder.glob('*.json.gz')) if folder.is_dir() else []


def label_for_minute(m: int) -> str:
    t = 9 * 60 + 30 + m
    return f'{t // 60:02d}{t % 60:02d}'


def latest_label_at_or_before(state_dir: str, d: date, m: int) -> Optional[str]:
    """Most recent snapshot whose `taken_at` is at or before the close of bar m (never a later one: causality)."""
    from ..calendar_nyse import session_open
    from datetime import timedelta
    cap = session_open(d) + timedelta(minutes=m + 1)
    best = None
    for label in labels_available(state_dir, d):
        snap = load_snapshot(state_dir, d, label)
        try:
            taken = datetime.fromisoformat(snap['taken_at'])
        except (KeyError, TypeError, ValueError):
            continue
        if taken <= cap:
            best = label
    return best


def same_day_rows(snap: Dict, root: str, d: date) -> List[Dict]:
    return (snap or {}).get('roots', {}).get(root, {}).get('expiries', {}).get(d.isoformat(), [])


def next_expiry_rows(snap: Dict, root: str, d: date) -> List[Dict]:
    ex = (snap or {}).get('roots', {}).get(root, {}).get('expiries', {})
    later = sorted(e for e in ex if e > d.isoformat())
    return ex[later[0]] if later else []
