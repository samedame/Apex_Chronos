"""Real NBBO ledger for every P2 entry (catalogue items 1, 61-63, 94, 96-99, 104).

For each P2 entry, alerted or not, the ledger records the bid/ask/size/greeks/quote-timestamps of the short leg, the long
leg and (for SPY) the XSP equivalents:
    alert (immediately), +30 s, +60 s, then every minute while the paper position is open, at its exit, and at 15:59.
Records are appended to state/lab/quote_ledger/<date>.jsonl, which the live job's normal state commit publishes.

Safety: the tracker runs in ONE daemon thread, every call is wrapped so that nothing can raise into the live loop, and the
Discord follow-up is a SEPARATE message posted after the original alert; the original alert text and timing are untouched.
Tradier bidsize/asksize units are unverified, so records carry size_unit='unverified' and no gate uses sizes.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .. import tradier
from ..calendar_nyse import NY, minute_clock, session_open
from . import options_math as om
from .snapshots import SIZE_UNIT, occ_symbol

NAN = float('nan')
FOLLOW_UP_OFFSETS_S = (0.0, 30.0, 60.0)
TRACK_EVERY_S = 60.0
SPREAD_GATES = (0.15, 0.25)
STALE_S = 5.0


def p2_leg_symbols(ev_data: Dict, ticker: str, d: date) -> Dict[str, Dict]:
    """Option symbols for the P2 credit spread of an ENTRY event. direction +1 = bullish = PUT credit spread (short K1 below
    spot, long K2 further below); direction -1 = CALL credit spread."""
    is_put = int(ev_data['direction']) == 1
    out = {'short': {'sym': occ_symbol(ticker, d, not is_put, ev_data['K1']), 'k': float(ev_data['K1'])},
           'long': {'sym': occ_symbol(ticker, d, not is_put, ev_data['K2']), 'k': float(ev_data['K2'])},
           'is_put': is_put}
    return out


def xsp_equivalent_strike(k_spy: float, spy_spot: float, xsp_spot: float) -> float:
    """Same moneyness on XSP (strikes are whole dollars): K_xsp = round(K_spy * xsp_spot / spy_spot)."""
    return float(round(k_spy * xsp_spot / spy_spot))


def _norm_quote(sym: str, q: Dict, now_epoch: float) -> Dict:
    g = q.get('greeks') or {}
    stamps = [q.get(k) for k in ('bid_date', 'ask_date') if q.get(k)]
    age = max(0.0, now_epoch - max(stamps) / 1000.0) if stamps else NAN
    return {'sym': sym, 'b': _f(q.get('bid')), 'a': _f(q.get('ask')), 'bs': _i(q.get('bidsize')), 'as': _i(q.get('asksize')),
            'last': _f(q.get('last')), 'bd': _i(q.get('bid_date')), 'ad': _i(q.get('ask_date')), 'age_s': age,
            'iv': _f(g.get('mid_iv')), 'd': _f(g.get('delta')), 'g': _f(g.get('gamma')), 'vg': _f(g.get('vega')),
            'v': _i(q.get('volume')), 'oi': _i(q.get('open_interest')), 'size_unit': SIZE_UNIT}


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


def fetch_option_quotes(symbols: List[str], env=None, clock: Callable[[], float] = time.time) -> Dict[str, Dict]:
    """Real-time option quotes with greeks for up to ~50 OCC symbols; symbols Tradier does not know are simply absent."""
    if not symbols:
        return {}
    j = tradier._get('/markets/quotes', {'symbols': ','.join(sorted(set(symbols))), 'greeks': 'true'}, env=env)
    now = clock()
    out = {}
    for q in tradier._as_list((j.get('quotes') or {}).get('quote')):
        if q.get('symbol'):
            out[q['symbol'].upper()] = _norm_quote(q['symbol'].upper(), q, now)
    return out


class QuoteTracker:
    """Schedules and writes the quote captures. `fetch(symbols) -> {sym: normalised quote}` is injectable for tests."""

    def __init__(self, state_dir: str, d: date, fetch: Callable[[List[str]], Dict[str, Dict]] = fetch_option_quotes,
                 clock: Callable[[], float] = time.time, post: Optional[Callable[[str, str], None]] = None,
                 spot_lookup: Optional[Callable[[str], float]] = None, autostart: bool = True):
        self.autostart = autostart
        self.state_dir, self.date = state_dir, d
        self.fetch, self.clock, self.post = fetch, clock, post
        self.spot_lookup = spot_lookup
        self.lock = threading.Lock()
        self.active: Dict[str, Dict] = {}          # key -> {legs, ticker, minute, next_track}
        self.due: List[Tuple[float, str, str]] = []
        self.first: Dict[str, Dict] = {}
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.path = Path(state_dir) / 'lab' / 'quote_ledger' / f'{d.isoformat()}.jsonl'
        self.errors = 0

    # -- public hooks (never raise) ----------------------------------------------------------------------------------
    def on_entry(self, ev_key: str, ticker: str, minute: int, ev_data: Dict, alerted: bool, spy_spot: Optional[float] = None) -> None:
        try:
            legs = p2_leg_symbols(ev_data, ticker, self.date)
            xsp = None
            if ticker == 'SPY' and self.spot_lookup:
                xsp_spot, spy_s = self.spot_lookup('XSP'), spy_spot or ev_data.get('S_entry')
                if xsp_spot and spy_s:
                    xk1 = xsp_equivalent_strike(legs['short']['k'], spy_s, xsp_spot)
                    xk2 = xsp_equivalent_strike(legs['long']['k'], spy_s, xsp_spot)
                    if xk1 != xk2:
                        xsp = {'short': {'sym': occ_symbol('XSP', self.date, not legs['is_put'], xk1), 'k': xk1},
                               'long': {'sym': occ_symbol('XSP', self.date, not legs['is_put'], xk2), 'k': xk2}}
            now = self.clock()
            with self.lock:
                self.active[ev_key] = {'legs': legs, 'xsp': xsp, 'ticker': ticker, 'minute': minute, 'alerted': alerted,
                                       'credit_model': ev_data.get('credit'), 'width': ev_data.get('width'),
                                       'next_track': now + 90.0 + 0.0}
                for off, tag in zip(FOLLOW_UP_OFFSETS_S, ('alert', '+30s', '+60s')):
                    self.due.append((now + off, ev_key, tag))
            self._ensure_thread()
        except Exception as e:                                       # noqa: BLE001
            self._err('on_entry', e)

    def on_exit(self, ev_key: str, tag: str = 'exit') -> None:
        try:
            with self.lock:
                if ev_key in self.active:
                    self.due.append((self.clock(), ev_key, tag))
                    self.active[ev_key]['closing'] = True
            self._ensure_thread()
        except Exception as e:                                       # noqa: BLE001
            self._err('on_exit', e)

    # -- engine ------------------------------------------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if not self.autostart:
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, name='lab-quote-ledger', daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.run_due()
            except Exception as e:                                   # noqa: BLE001
                self._err('loop', e)
            with self.lock:
                if not self.active and not self.due:
                    return
            self.stop.wait(1.0)

    def run_due(self) -> int:
        """Process every capture that is due now. Returns the number of captures written (testable without threads)."""
        now = self.clock()
        with self.lock:
            for key, a in list(self.active.items()):
                if not a.get('closing') and now >= a['next_track'] and not any(k == key and t == 'track' and due > now for due, k, t in self.due):
                    self.due.append((now, key, 'track'))
                    a['next_track'] = now + TRACK_EVERY_S
            ready = [x for x in self.due if x[0] <= now]
            self.due = [x for x in self.due if x[0] > now]
        n = 0
        for _, key, tag in sorted(ready):
            with self.lock:
                a = self.active.get(key)
            if not a:
                continue
            try:
                n += self._capture(key, a, tag)
            except Exception as e:                                   # noqa: BLE001
                self._err('capture', e)
            if tag in ('exit', 'close'):
                with self.lock:
                    self.active.pop(key, None)
                    self.due = [x for x in self.due if x[1] != key]
        return n

    def close_all(self, tag: str = 'close') -> None:
        with self.lock:
            for key in list(self.active):
                self.due.append((self.clock(), key, tag))
                self.active[key]['closing'] = True

    def _capture(self, key: str, a: Dict, tag: str) -> int:
        syms = [a['legs']['short']['sym'], a['legs']['long']['sym']]
        if a.get('xsp'):
            syms += [a['xsp']['short']['sym'], a['xsp']['long']['sym']]
        got = self.fetch(syms)
        now = self.clock()
        iso = datetime.fromtimestamp(now, NY).isoformat(timespec='seconds')
        recs = []
        for role, sym in zip(('short', 'long', 'xsp_short', 'xsp_long'), syms):
            q = got.get(sym)
            recs.append({'key': key, 'date': self.date.isoformat(), 'ticker': a['ticker'], 'minute': a['minute'], 'tag': tag,
                         'role': role, 'at': iso, 'epoch': round(now, 3), 'alerted': a['alerted'],
                         **(q if q else {'sym': sym, 'not_found': True})})
        self._append(recs)
        if tag == 'alert':
            self.first[key] = {r['role']: r for r in recs}
            if self.post and a['alerted']:
                try:
                    self.post(key, format_followup(a, self.first[key]))
                except Exception as e:                                # noqa: BLE001
                    self._err('post', e)
            if all(r.get('not_found') for r in recs[:2]):
                with self.lock:                                       # nothing to track: the contract does not exist
                    a['closing'] = True
                    self.due.append((now, key, 'close'))
        return len(recs)

    def _append(self, recs: List[Dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self.path, 'a') as fh:
            for r in recs:
                fh.write(json.dumps(r, separators=(',', ':'), allow_nan=True) + '\n')

    def _err(self, where: str, e: Exception) -> None:
        self.errors += 1
        print(f'lab quote ledger {where} failed: {type(e).__name__}: {str(e)[:100]}')


# ---- summaries ------------------------------------------------------------------------------------------------------
def load_ledger(state_dir: str, d: date) -> List[Dict]:
    p = Path(state_dir) / 'lab' / 'quote_ledger' / f'{d.isoformat()}.jsonl'
    if not p.exists():
        return []
    out = []
    with open(p) as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def entry_metrics(recs: List[Dict], key: str, width: float, tag: str = 'alert', prefix: str = '') -> Dict[str, float]:
    """Credit at mid and taker, half-spread cost as % of credit, worst quote age and the pre-declared gate verdicts for one
    entry at one capture tag. `prefix` = 'xsp_' to read the XSP legs."""
    s = next((r for r in recs if r['key'] == key and r['tag'] == tag and r['role'] == f'{prefix}short'), None)
    l = next((r for r in recs if r['key'] == key and r['tag'] == tag and r['role'] == f'{prefix}long'), None)
    blank = {k: NAN for k in ('mid_credit', 'taker_credit', 'half_spread_sum', 'spread_pct_credit', 'credit_over_width',
                              'worst_age_s', 'short_iv', 'long_iv', 'short_delta', 'gate_015', 'gate_025', 'stale')}
    if not s or not l or s.get('not_found') or l.get('not_found') or s.get('b') is None or l.get('b') is None:
        return blank
    m = om.credit_spread_metrics(s, l, width)
    ages = [x for x in (s.get('age_s'), l.get('age_s')) if x is not None and np.isfinite(x)]
    worst = float(max(ages)) if ages else NAN
    stale = float(worst > STALE_S) if np.isfinite(worst) else NAN
    sp = m['spread_pct_credit']
    return {**m, 'worst_age_s': worst, 'short_iv': s.get('iv') if s.get('iv') is not None else NAN,
            'long_iv': l.get('iv') if l.get('iv') is not None else NAN, 'short_delta': s.get('d') if s.get('d') is not None else NAN,
            'gate_015': float(np.isfinite(sp) and sp <= SPREAD_GATES[0] and stale == 0.0) if np.isfinite(sp) else NAN,
            'gate_025': float(np.isfinite(sp) and sp <= SPREAD_GATES[1] and stale == 0.0) if np.isfinite(sp) else NAN,
            'stale': stale}


def spread_path(recs: List[Dict], key: str) -> List[Tuple[float, float]]:
    """(epoch, spread value at the ask-for-long / bid-for-short taker price) for each capture: the cost to close the short
    spread at the touch. Used to evaluate profit-take levels and spread-value stops on real quotes (items 98, 100)."""
    by: Dict[float, Dict[str, Dict]] = {}
    for r in recs:
        if r['key'] == key and r['role'] in ('short', 'long') and not r.get('not_found'):
            by.setdefault(r['epoch'], {})[r['role']] = r
    out = []
    for ep, d in sorted(by.items()):
        s, l = d.get('short'), d.get('long')
        if s and l and s.get('a') is not None and l.get('b') is not None:
            out.append((ep, float(s['a'] - l['b'])))
    return out


def format_followup(a: Dict, first: Dict[str, Dict]) -> str:
    """Observation-only Discord line posted AFTER the entry alert. States numbers, never a recommendation."""
    s, l = first.get('short', {}), first.get('long', {})
    if s.get('not_found') or l.get('not_found'):
        return (f"📊 lab (observation only) {a['ticker']} P2: Tradier has no quote for one of the two legs "
                f"({'short' if s.get('not_found') else 'long'} {s.get('sym') if s.get('not_found') else l.get('sym')}); "
                "the contract may not exist today. Recorded for the ledger.")
    width = a.get('width') or abs(a['legs']['short']['k'] - a['legs']['long']['k'])
    m = om.credit_spread_metrics(s, l, width)
    age = max([x for x in (s.get('age_s'), l.get('age_s')) if x is not None and np.isfinite(x)] or [NAN])
    fmt = lambda v, f='{:.2f}': 'n/a' if v is None or not np.isfinite(v) else f.format(v)
    line = (f"📊 lab (observation only) {a['ticker']} P2 real quotes: short {s['b']}/{s['a']}, long {l['b']}/{l['a']}; "
            f"credit mid ${fmt(m['mid_credit'])}, at the touch ${fmt(m['taker_credit'])} (model ${fmt(a.get('credit_model'))}); "
            f"half-spreads = {fmt(m['spread_pct_credit'] * 100 if np.isfinite(m['spread_pct_credit']) else NAN, '{:.0f}')}% of credit; "
            f"quote age {fmt(age, '{:.0f}')} s.")
    if np.isfinite(m['spread_pct_credit']):
        line += f" Spread gate 15%: {'pass' if m['spread_pct_credit'] <= 0.15 else 'fail'}; 25%: {'pass' if m['spread_pct_credit'] <= 0.25 else 'fail'}."
    return line + " Not a recommendation; nothing here changes the alert above."
