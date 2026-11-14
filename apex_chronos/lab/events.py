"""Event and calendar flags (catalogue items 31-33, 68-74, 79).

Flags are 1.0 / 0.0, or nan when the answer is unknown (a missing earnings or dividend lookup is `unknown`, never `0`).
Dates in lab/data/events.csv were read from the official schedules on 2026-11-14 (source column); pages did not list
earlier 2026 CPI/NFP/PPI/PCE/GDP dates, so macro coverage before 2026-11-06 is incomplete and rows are not invented.
Retail sales (Census) could not be retrieved and is absent. Weekly claims (every Thursday 08:30) and ISM manufacturing (first
business day 10:00) are standard-practice RULES, marked as such.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from ..calendar_nyse import EARLY_CLOSES, HOLIDAYS, is_full_session, is_trading_day

NAN = float('nan')
DATA = Path(__file__).parent / 'data' / 'events.csv'
MEGACAPS = frozenset({'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'GOOG', 'TSLA', 'AVGO'})
FOMC_MUTE_FROM_MINUTE = 150            # 12:00 ET = 09:30 + 150 minutes


def load_events(path: Optional[Path] = None) -> List[Dict]:
    with open(path or DATA, newline='') as fh:
        return list(csv.DictReader(fh))


def _by_kind(events: Iterable[Dict]) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for e in events:
        out.setdefault(e['kind'], set()).add(e['date'])
    return out


# ---- calendar structure -------------------------------------------------------------------------------------------
def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def _prev_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def monthly_opex(d: date) -> bool:
    """Third Friday (or the prior trading day when the exchange is closed)."""
    return _prev_trading_day(third_friday(d.year, d.month)) == d


def triple_witching(d: date) -> bool:
    return d.month in (3, 6, 9, 12) and monthly_opex(d)


def vix_expiry(d: date) -> bool:
    """VIX options/futures settle on the Wednesday 30 days before the third Friday of the FOLLOWING month; if that day is
    an exchange holiday, the preceding business day."""
    ny, nm = (d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1)
    exp = third_friday(ny, nm) - timedelta(days=30)
    return _prev_trading_day(exp) == d


def month_end(d: date) -> bool:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt.month != d.month and is_trading_day(d)


def quarter_end(d: date) -> bool:
    return month_end(d) and d.month in (3, 6, 9, 12)


def turn_of_month(d: date) -> bool:
    """Last trading day of a month or the first three trading days of a month."""
    if not is_trading_day(d):
        return False
    if month_end(d):
        return True
    n, cur = 0, date(d.year, d.month, 1)
    while cur <= d:
        n += is_trading_day(cur)
        cur += timedelta(days=1)
    return n <= 3


def adjacent_closure(d: date) -> Dict[str, float]:
    prev, nxt = d - timedelta(days=1), d + timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return {'half_day': float(d in EARLY_CLOSES), 'day_after_holiday': float(prev in HOLIDAYS or (d - prev).days > 3),
            'day_before_holiday': float(nxt in HOLIDAYS or nxt in EARLY_CLOSES or (nxt - d).days > 3),
            'full_session': float(is_full_session(d))}


# ---- macro ---------------------------------------------------------------------------------------------------------
def macro_flags(d: date, events: Optional[List[Dict]] = None) -> Dict[str, float]:
    ev = _by_kind(events if events is not None else load_events())
    iso = d.isoformat()
    first_bd = date(d.year, d.month, 1)
    while not is_trading_day(first_bd):
        first_bd += timedelta(days=1)
    return {'fomc_day': float(iso in ev.get('fomc', ())), 'cpi_day': float(iso in ev.get('cpi', ())),
            'nfp_day': float(iso in ev.get('nfp', ())), 'ppi_day': float(iso in ev.get('ppi', ())),
            'pce_day': float(iso in ev.get('pce', ())), 'gdp_day': float(iso in ev.get('gdp', ())),
            'claims_day_rule': float(d.weekday() == 3), 'ism_mfg_day_rule': float(d == first_bd),
            'any_macro_0830': float(any(iso in ev.get(k, ()) for k in ('cpi', 'nfp', 'ppi', 'pce', 'gdp')) or d.weekday() == 3)}


def fomc_mute(d: date, m_entry: int, events: Optional[List[Dict]] = None) -> float:
    """F3: 1.0 when the P2 entry would be withheld by the FOMC rule (decision day, entry at or after 12:00 ET)."""
    return float(macro_flags(d, events)['fomc_day'] and m_entry >= FOMC_MUTE_FROM_MINUTE)


def calendar_flags(d: date, events: Optional[List[Dict]] = None) -> Dict[str, float]:
    out = {'monthly_opex': float(monthly_opex(d)), 'triple_witching': float(triple_witching(d)), 'vix_expiry': float(vix_expiry(d)),
           'month_end': float(month_end(d)), 'quarter_end': float(quarter_end(d)), 'turn_of_month': float(turn_of_month(d)),
           'weekday': float(d.weekday())}
    out.update(adjacent_closure(d))
    out.update(macro_flags(d, events))
    return out


# ---- earnings and dividends (Nasdaq public calendars, cached) ----------------------------------------------------------
NASDAQ_EARNINGS = 'https://api.nasdaq.com/api/calendar/earnings'
NASDAQ_DIVIDENDS = 'https://api.nasdaq.com/api/calendar/dividends'
_UA = {'User-Agent': 'Mozilla/5.0 (apex-chronos-lab)', 'Accept': 'application/json'}
COVERAGE = '*'


def parse_earnings(payload: Dict, d: date, symbols: Iterable[str]) -> List[Dict]:
    """Rows for the requested symbols from a Nasdaq earnings-calendar payload. The response shape (data.rows[].symbol,
    .time) is the public API's documented layout; any other shape raises ValueError so the caller records `unknown`."""
    rows = ((payload or {}).get('data') or {}).get('rows')
    if rows is None:
        raise ValueError('unexpected earnings payload')
    want = {s.upper() for s in symbols}
    out = []
    for r in rows:
        sym = str(r.get('symbol', '')).upper()
        if sym in want:
            t = str(r.get('time', ''))
            when = 'bmo' if 'pre' in t else ('amc' if 'after' in t else 'unknown')
            out.append({'date': d.isoformat(), 'symbol': sym, 'when': when})
    return out


def parse_dividends(payload: Dict, d: date, symbols: Iterable[str]) -> List[Dict]:
    rows = (((payload or {}).get('data') or {}).get('calendar') or {}).get('rows')
    if rows is None:
        raise ValueError('unexpected dividend payload')
    want = {s.upper() for s in symbols}
    return [{'date': d.isoformat(), 'symbol': str(r.get('symbol', '')).upper(), 'when': 'exdiv'}
            for r in rows if str(r.get('symbol', '')).upper() in want]


def _cache_path(state_dir: str, kind: str) -> Path:
    return Path(state_dir) / 'lab' / f'{kind}.csv'


def load_cache(state_dir: str, kind: str) -> List[Dict]:
    p = _cache_path(state_dir, kind)
    if not p.exists():
        return []
    with open(p, newline='') as fh:
        return list(csv.DictReader(fh))


def save_cache(state_dir: str, kind: str, rows: List[Dict]) -> None:
    p = _cache_path(state_dir, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted({(r['date'], r['symbol'], r['when']) for r in rows})
    tmp = str(p) + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['date', 'symbol', 'when'])
        w.writerows(rows)
    os.replace(tmp, p)


def refresh_calendar(http, state_dir: str, kind: str, days: Iterable[date], symbols: Iterable[str]) -> int:
    """Fetch the Nasdaq calendar for each day not yet covered. Each fetched day gets a coverage marker row, so a day with
    no relevant rows reads as 'covered, none' and a day never fetched reads as 'unknown'. Returns days fetched."""
    url = NASDAQ_EARNINGS if kind == 'earnings' else NASDAQ_DIVIDENDS
    parse = parse_earnings if kind == 'earnings' else parse_dividends
    rows = load_cache(state_dir, kind)
    covered = {r['date'] for r in rows if r['symbol'] == COVERAGE}
    n = 0
    for d in days:
        if d.isoformat() in covered or d.weekday() >= 5:
            continue
        try:
            r = http.get(url, params={'date': d.isoformat()}, headers=_UA, timeout=15)
            if r.status_code != 200:
                continue
            rows += parse(r.json(), d, symbols)
            rows.append({'date': d.isoformat(), 'symbol': COVERAGE, 'when': 'covered'})
            n += 1
        except Exception as e:                                    # noqa: BLE001 - lab data never breaks a session
            print(f'lab {kind} fetch failed for {d}: {type(e).__name__}')
    if n:
        save_cache(state_dir, kind, rows)
    return n


def earnings_flags(ticker: str, d: date, rows: List[Dict]) -> Dict[str, float]:
    """earnings_window: reports on d (before open), on d-1 after the close (reaction day), or on d after the close (the
    release is hours after a midday P2 and the next day is also in the window). qqq_spillover: QQQ/SPY on a mega-cap reaction
    day. unknown (nan) when the calendar was not fetched for d or d-1."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    cov = {r['date'] for r in rows if r['symbol'] == COVERAGE}
    if d.isoformat() not in cov or prev.isoformat() not in cov:
        return {'earnings_window': NAN, 'megacap_reaction_day': NAN}
    own = [r for r in rows if r['symbol'] == ticker and r['date'] in (d.isoformat(), prev.isoformat())]
    win = any((r['date'] == d.isoformat()) or (r['date'] == prev.isoformat() and r['when'] != 'bmo') for r in own)
    mega = any(r['symbol'] in MEGACAPS and ((r['date'] == d.isoformat() and r['when'] == 'bmo') or
                                            (r['date'] == prev.isoformat() and r['when'] != 'bmo')) for r in rows)
    return {'earnings_window': float(win), 'megacap_reaction_day': float(mega)}


def exdiv_flags(ticker: str, d: date, rows: List[Dict]) -> Dict[str, float]:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    cov = {r['date'] for r in rows if r['symbol'] == COVERAGE}
    if d.isoformat() not in cov or nxt.isoformat() not in cov:
        return {'exdiv_today': NAN, 'exdiv_next_day': NAN}
    mine = {r['date'] for r in rows if r['symbol'] == ticker}
    return {'exdiv_today': float(d.isoformat() in mine), 'exdiv_next_day': float(nxt.isoformat() in mine)}
