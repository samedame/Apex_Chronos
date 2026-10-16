"""Yahoo Finance data layer (the same source the backtest data came from: fetch_data.py).

Live rules that keep the bot faithful to the backtest:
  * only COMPLETED one-minute bars are used (the in-progress bar Yahoo returns is dropped);
  * bars are placed on the 09:30..15:59 clock grid so index m always means '09:30 + m minutes';
    isolated missing minutes are forward-filled (flat bar, zero volume) and reported;
  * a ticker whose 09:30 bar is missing is excluded for the day (fail closed).
"""
from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from . import config as C
from . import tradier
from .calendar_nyse import NY, session_open

_UA = {'User-Agent': 'Mozilla/5.0 (apex-chronos-bot)'}
_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{sym}'


class FeedError(RuntimeError):
    pass


def _get(session: requests.Session, sym: str, params: dict, retries: int = 3, timeout: float = 15.0) -> dict:
    last = None
    for i in range(retries):
        try:
            r = session.get(_URL.format(sym=requests.utils.quote(sym)), params=params, headers=_UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 500, 502, 503, 504):
                _time.sleep(1.5 * (i + 1))
                continue
            break
        except (requests.RequestException, ValueError) as e:  # network / bad json
            last = repr(e)
            _time.sleep(1.0 * (i + 1))
    raise FeedError(f"{sym}: {last}")


def _frame(payload: dict) -> pd.DataFrame:
    try:
        res = payload['chart']['result'][0]
    except (KeyError, IndexError, TypeError):
        raise FeedError('no chart result')
    ts = res.get('timestamp') or []
    if not ts:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    q = res['indicators']['quote'][0]
    df = pd.DataFrame({
        'timestamp': pd.to_datetime(ts, unit='s', utc=True).tz_convert('America/New_York'),
        'open': q['open'], 'high': q['high'], 'low': q['low'], 'close': q['close'], 'volume': q['volume'],
    })
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    return df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)


def tradier_bars_enabled() -> bool:
    """Opt-in: CHRONOS_TRADIER_BARS=1 AND a Tradier key. Off by default because the strategy's thresholds were fitted on Yahoo
    bars (see docs/LIVE_VS_BACKTEST.md); turn it on only after comparing the two feeds side by side (scripts/compare_feeds.py)."""
    return os.environ.get('CHRONOS_TRADIER_BARS', '').strip() == '1' and tradier.configured()


def tradier_frame(sym: str, now: Optional[datetime] = None) -> pd.DataFrame:
    """Today's regular-session 1-minute bars from Tradier in the same shape _frame() returns. Bars still forming (their minute
    has not ended) are dropped, so only COMPLETED bars reach the engine, exactly as with Yahoo."""
    now = now or datetime.now(NY)
    day = now.astimezone(NY).date()
    rows = tradier.timesales(sym, f'{day:%Y-%m-%d} 09:30', f'{day:%Y-%m-%d} 16:00', interval='1min')
    out = []
    for r in rows:
        try:
            ts = pd.Timestamp(r['time']).tz_localize(NY) if pd.Timestamp(r['time']).tzinfo is None else pd.Timestamp(r['time']).tz_convert(NY)
            out.append((ts, float(r['open']), float(r['high']), float(r['low']), float(r['close']), float(r.get('volume') or 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
    cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    if not out:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(out, columns=cols)
    df = df[df['timestamp'] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
    return df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)


def fetch_1m_today(session: requests.Session, sym: str) -> pd.DataFrame:
    """Yahoo by default. With CHRONOS_TRADIER_BARS=1 and a Tradier key, equities come from Tradier first and fall back to Yahoo
    on any Tradier failure or an empty answer (the VIX index always stays on Yahoo)."""
    if tradier_bars_enabled() and not sym.startswith('^'):
        try:
            df = tradier_frame(sym)
            if not df.empty:
                return df
            print(f'[data] tradier returned no bars for {sym}; using Yahoo')
        except Exception as e:  # never let a Tradier problem silence the bot
            print(f'[data] tradier bars failed for {sym} ({type(e).__name__}: {e}); using Yahoo')
    return _frame(_get(session, sym, {'interval': '1m', 'range': '1d', 'includePrePost': 'false'}))


def fetch_1m_range(session: requests.Session, sym: str, start: date, end: date) -> pd.DataFrame:
    """1-minute bars for [start, end] (Yahoo allows <=7 calendar days per request and ~30 days of history)."""
    frames = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=6))
        p1 = int(datetime.combine(cur, datetime.min.time(), tzinfo=NY).timestamp())
        p2 = int(datetime.combine(stop + timedelta(days=1), datetime.min.time(), tzinfo=NY).timestamp())
        frames.append(_frame(_get(session, sym, {'interval': '1m', 'period1': p1, 'period2': p2,
                                                 'includePrePost': 'false'})))
        cur = stop + timedelta(days=1)
        _time.sleep(0.15)
    if not frames:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)


def fetch_1d(session: requests.Session, sym: str, rng: str = '2y') -> pd.DataFrame:
    df = _frame(_get(session, sym, {'interval': '1d', 'range': rng, 'includePrePost': 'false'}))
    df['date'] = df['timestamp'].dt.strftime('%Y-%m-%d')
    return df


# ---------------------------------------------------------------------------------
@dataclass
class SessionBars:
    """One ticker's bars for the session on the clock grid, indices 0..n-1 (n = bars available so far)."""
    ticker: str
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray
    holes: List[int] = field(default_factory=list)   # forward-filled interior minutes
    last_real: int = -1                              # index of the last real (not filled) bar
    ok: bool = True
    reason: str = ''


def to_grid(df: pd.DataFrame, session_date: date, ticker: str, m_target: int) -> SessionBars:
    """Place real completed bars on the grid up to minute m_target (inclusive)."""
    empty = np.array([], dtype=float)
    if df is None or df.empty:
        return SessionBars(ticker, empty, empty, empty, empty, empty, ok=False, reason='no data')
    day = df[df['timestamp'].dt.strftime('%Y-%m-%d') == session_date.isoformat()]
    # Yahoo stamps the in-progress bar with the last-trade time (e.g. 15:34:37); completed bars sit on :00
    day = day[day['timestamp'].dt.second == 0]
    if day.empty:
        return SessionBars(ticker, empty, empty, empty, empty, empty, ok=False, reason='no bars for session')
    open_dt = pd.Timestamp(session_open(session_date))
    idx = ((day['timestamp'] - open_dt) / pd.Timedelta(minutes=1)).round().astype(int)
    day = day.assign(m=idx.values)
    day = day[(day['m'] >= 0) & (day['m'] <= min(m_target, C.SESSION_BARS - 1))]
    if day.empty or day['m'].min() != 0:
        return SessionBars(ticker, empty, empty, empty, empty, empty, ok=False, reason='09:30 bar missing')
    n = int(day['m'].max()) + 1
    g = day.set_index('m').reindex(range(n))
    holes = [int(i) for i in g.index[g['close'].isna()]]
    g['close'] = g['close'].ffill()
    for col in ('open', 'high', 'low'):
        g[col] = g[col].fillna(g['close'])
    g['volume'] = g['volume'].fillna(0.0)
    # pad to the target minute with the last close if the feed is lagging
    last_real = int(day['m'].max())
    return SessionBars(ticker, g['open'].values.astype(float), g['high'].values.astype(float),
                       g['low'].values.astype(float), g['close'].values.astype(float),
                       g['volume'].values.astype(float), holes=holes, last_real=last_real)


def pad_to(sb: SessionBars, n: int) -> SessionBars:
    """Extend a lagging ticker to n bars with flat forward-filled bars (flagged via last_real)."""
    k = len(sb.C)
    if k >= n or k == 0:
        return sb
    last = sb.C[-1]
    ext = n - k
    sb.O = np.concatenate([sb.O, np.full(ext, last)])
    sb.H = np.concatenate([sb.H, np.full(ext, last)])
    sb.L = np.concatenate([sb.L, np.full(ext, last)])
    sb.C = np.concatenate([sb.C, np.full(ext, last)])
    sb.V = np.concatenate([sb.V, np.zeros(ext)])
    return sb


def align_vix(vix_df: pd.DataFrame, session_date: date, n: int) -> Tuple[np.ndarray, int]:
    """VIX per equity bar: last VIX bar with timestamp <= the equity bar's label (the original ffill); 18.0 if none.
    Returns (array, minutes_stale_at_last_bar)."""
    open_dt = pd.Timestamp(session_open(session_date))
    grid = pd.date_range(open_dt, periods=n, freq='min')
    if vix_df is None or vix_df.empty:
        return np.full(n, 18.0), 999
    s = vix_df[vix_df['timestamp'].dt.second == 0].set_index('timestamp')['close'].sort_index()
    s = s[~s.index.duplicated(keep='last')]
    v = s.reindex(grid, method='ffill').values.astype(float)
    last_vix_ts = s.index[s.index <= grid[-1]]
    stale = int((grid[-1] - last_vix_ts[-1]) / pd.Timedelta(minutes=1)) if len(last_vix_ts) else 999
    return np.where(np.isnan(v), 18.0, v), stale
