"""Historical replay: feeds recorded 1-minute CSV data through the SAME SessionEngine the live bot uses.

Used by the verification harness. The context built here (previous close,
20-day realised vol, 14-session noise/RVOL baselines, 5-minute pre-seed for the
first sessions, VIX alignment) reproduces run_1m_options_lab.prepare_ticker_data
so the replay sees exactly what the original backtest saw.

Two data modes:
  grid=False  rows exactly as stored (the original indexes by row, so a session
              with a missing minute shifts later indices by one)  <- oracle-exact
  grid=True   re-index onto the 390-minute clock grid, forward-filling holes
              <- what the live loader does
Two stepping modes:
  mode='fast'      features computed once per session, engine steps minute by minute
  mode='livepath'  features recomputed at every minute from ONLY the bars seen so
                   far (what a live poll does)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import config as C
from .engine import Event, SessionEngine
from .features import Features, TickerContext, compute_features, session_profiles

NY = 'America/New_York'


def _read(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert(NY)
    return df


def daily_context(df1d: pd.DataFrame, session_date: str):
    """(prev_close, rvol20) as of the open of session_date, from completed daily bars only."""
    d = df1d[df1d['date'] < session_date]
    close = d['close'].reset_index(drop=True)
    if len(close) < 2:
        return None, None
    prev_close = close.shift(1)
    ret = np.log(close / prev_close)
    rvol = ret.rolling(20).std().iloc[-1] * np.sqrt(252)
    prev_c = float(close.iloc[-1])
    rv = float(rvol) if pd.notna(rvol) else float('nan')
    if pd.isna(rv) or rv <= 0:
        rv = 0.20
    return prev_c, rv


def preseed_from_5m(df5m: pd.DataFrame, before_date: str):
    """Noise/RVOL baseline sessions built from 5-minute bars (original: interpolated onto 390 minutes)."""
    moves, cumvs = [], []
    df = df5m.copy()
    df['date'] = df['timestamp'].dt.strftime('%Y-%m-%d')
    df['hm'] = df['timestamp'].dt.strftime('%H:%M')
    df = df[(df['hm'] >= '09:30') & (df['hm'] <= '15:59') & (df['date'] < before_date)]
    for _, g5 in df.groupby('date'):
        if len(g5) >= 70:
            o0 = g5['open'].iloc[0]
            moves_5m = np.abs(g5['close'].values / o0 - 1.0)
            cumv_5m = np.cumsum(g5['volume'].fillna(0).values)
            x_old = np.linspace(0, 389, len(moves_5m))
            x_new = np.arange(390)
            moves.append(np.interp(x_new, x_old, moves_5m))
            cumvs.append(np.interp(x_new, x_old, cumv_5m))
    return moves, cumvs


def align_to_grid(g: pd.DataFrame) -> pd.DataFrame:
    """Re-index one session of 1m bars onto 09:30..15:59, forward-filling holes (flat bar, zero volume)."""
    day = g['timestamp'].iloc[0].normalize()
    grid = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), periods=C.SESSION_BARS, freq='min', tz=NY)
    g = g.set_index('timestamp').reindex(grid)
    g['close'] = g['close'].ffill()
    for col in ('open', 'high', 'low'):
        g[col] = g[col].fillna(g['close'])
    g['volume'] = g['volume'].fillna(0.0)
    return g.reset_index().rename(columns={'index': 'timestamp'})


@dataclass
class TickerDay:
    ctx: TickerContext
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray
    vix: np.ndarray


def build_days(data_dir: str, tickers: List[str], grid: bool = False, dates: Optional[List[str]] = None,
               vix_lag_minutes: int = 0, baseline: str = 'oracle'):
    """-> (sorted session dates, {date: {ticker: TickerDay}}), mirroring prepare_ticker_data.

    baseline='oracle' : 5-minute pre-seed before the first 1-minute day, then completed 1-minute sessions (what the original did)
    baseline='5m'     : every baseline session rebuilt from 5-minute bars (sensitivity test only)"""
    vix_df = _read(os.path.join(data_dir, '1m', 'VIX.csv'))
    vix_s = vix_df.set_index('timestamp')['close'].sort_index()
    out: Dict[str, Dict[str, TickerDay]] = {}
    all_dates = set()
    for tkr in tickers:
        df1m = _read(os.path.join(data_dir, '1m', f'{tkr}.csv')).sort_values('timestamp').reset_index(drop=True)
        df1m['date'] = df1m['timestamp'].dt.strftime('%Y-%m-%d')
        df1m['hm'] = df1m['timestamp'].dt.strftime('%H:%M')
        df1m = df1m[(df1m['hm'] >= '09:30') & (df1m['hm'] <= '15:59')].copy().reset_index(drop=True)
        df1d = _read(os.path.join(data_dir, '1d', f'{tkr}.csv'))
        df1d['date'] = df1d['timestamp'].dt.strftime('%Y-%m-%d')
        df5m = _read(os.path.join(data_dir, '5m', f'{tkr}.csv'))
        days = sorted(df1m['date'].unique())
        hist_moves, hist_cumv = preseed_from_5m(df5m, days[0])
        for d in days:
            g = df1m[df1m['date'] == d].copy().reset_index(drop=True)
            if len(g) < C.MIN_SESSION_BARS:
                continue
            if grid:
                g = align_to_grid(g)
            vix = vix_s.reindex(g['timestamp'] - pd.Timedelta(minutes=vix_lag_minutes), method='ffill').values
            vix = np.where(np.isnan(vix), 18.0, vix)
            O, H, L, C_ = (g[c].values.astype(float) for c in ('open', 'high', 'low', 'close'))
            V = g['volume'].fillna(0).values.astype(float)
            prev_c, rvol20 = daily_context(df1d, d)
            if prev_c is None or pd.isna(prev_c):
                prev_c = float(O[0])
            if rvol20 is None:
                rvol20 = 0.20
            if baseline == '5m':
                bm, bc = preseed_from_5m(df5m, d)
            else:
                bm, bc = hist_moves, hist_cumv
            ctx = TickerContext(tkr, float(prev_c), float(rvol20), list(bm), list(bc))
            if dates is None or d in dates:
                out.setdefault(d, {})[tkr] = TickerDay(ctx, O, H, L, C_, V, vix)
            all_dates.add(d)
            mv, cv = session_profiles(O, C_, V)
            hist_moves.append(mv)
            hist_cumv.append(cv)
    return sorted(all_dates), out


def run_session(date: str, day: Dict[str, TickerDay], tickers: List[str], mode: str = 'fast',
                engine_kwargs: Optional[dict] = None, burst: int = 1):
    """Replay one session. Returns (engine, events)."""
    nb = {t: len(td.C) for t, td in day.items()}
    n_bars = max(nb.values())
    for t in tickers:
        nb.setdefault(t, n_bars)
    eng = SessionEngine(date, tickers, n_bars=nb, **(engine_kwargs or {}))
    events: List[Event] = []
    if mode == 'fast':
        feats = {t: compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix) for t, td in day.items()}
        for m in range(n_bars):
            events += eng.step(m, feats)
    elif mode == 'livepath':
        for m in range(n_bars):
            k = m + 1
            feats = {t: compute_features(td.ctx, td.O[:k], td.H[:k], td.L[:k], td.C[:k], td.V[:k], td.vix[:k])
                     for t, td in day.items() if len(td.C) >= k}
            events += eng.step(m, feats)
    elif mode == 'burst':
        # polls that arrive every `burst` minutes: features from the bars seen so far, several minutes stepped per poll
        m = 0
        while m < n_bars:
            hi = min(n_bars, m + burst)
            k = hi
            feats = {t: compute_features(td.ctx, td.O[:k], td.H[:k], td.L[:k], td.C[:k], td.V[:k], td.vix[:k])
                     for t, td in day.items() if len(td.C) >= k}
            for mm in range(m, hi):
                events += eng.step(mm, feats)
            m = hi
    else:
        raise ValueError(mode)
    return eng, events


def run_all(data_dir: str, tickers: List[str], mode: str = 'fast', grid: bool = False,
            dates: Optional[List[str]] = None, engine_kwargs: Optional[dict] = None, burst: int = 1,
            vix_lag_minutes: int = 0, days_cache=None):
    dts, days = days_cache if days_cache is not None else build_days(data_dir, tickers, grid=grid, dates=dates,
                                                                      vix_lag_minutes=vix_lag_minutes)
    trades: List[Dict] = []
    events: List = []
    cands: List[Dict] = []
    for d in dts:
        if d not in days:
            continue
        eng, ev = run_session(d, days[d], tickers, mode=mode, engine_kwargs=engine_kwargs, burst=burst)
        trades += eng.trades
        cands += eng.candidate_log
        events += [(d, e) for e in ev]
    run_all.last_candidates = pd.DataFrame(cands)
    return pd.DataFrame(trades), events
