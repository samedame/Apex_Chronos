"""Prior-session baselines (noise-area move profile and cumulative-volume profile).

The backtest rolled these forward one completed session at a time and used the
last 14. Here each completed session is stored as one small gzip-JSON file
(state/history/YYYY-MM-DD.json.gz); python floats round-trip exactly through
JSON, so a profile loaded tomorrow is bit-identical to the one computed today.
Missing days are rebuilt from Yahoo 1-minute history (available for ~30 days).
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import config as C
from .calendar_nyse import prev_full_sessions
from .data import fetch_1m_range, to_grid
from .features import TickerContext, session_profiles


def _path(state_dir: str, d: date) -> str:
    return os.path.join(state_dir, 'history', f'{d.isoformat()}.json.gz')


def save_session(state_dir: str, d: date, profiles: Dict[str, tuple]) -> str:
    """profiles: {ticker: (moves_array, cumv_array)}"""
    os.makedirs(os.path.join(state_dir, 'history'), exist_ok=True)
    payload = {t: {'moves': np.asarray(mv, dtype=float).tolist(), 'cumv': np.asarray(cv, dtype=float).tolist()}
               for t, (mv, cv) in profiles.items()}
    p = _path(state_dir, d)
    with gzip.open(p, 'wt', encoding='utf-8') as fh:
        json.dump(payload, fh)
    return p


def load_session(state_dir: str, d: date) -> Optional[Dict[str, tuple]]:
    p = _path(state_dir, d)
    if not os.path.exists(p):
        return None
    with gzip.open(p, 'rt', encoding='utf-8') as fh:
        raw = json.load(fh)
    return {t: (np.array(v['moves'], dtype=float), np.array(v['cumv'], dtype=float)) for t, v in raw.items()}


def rebuild_session(session, tickers: List[str], d: date) -> Optional[Dict[str, tuple]]:
    """Fetch one past session from Yahoo and build its profiles (None if the feed no longer has it)."""
    out = {}
    for t in tickers:
        df = fetch_1m_range(session, t, d, d)
        sb = to_grid(df, d, t, C.SESSION_BARS - 1)
        if not sb.ok or len(sb.C) < C.MIN_SESSION_BARS:
            return None
        out[t] = session_profiles(sb.O, sb.C, sb.V)
    return out


def load_history(state_dir: str, tickers: List[str], session_date: date, http=None,
                 allow_rebuild: bool = True) -> Dict[str, Dict[str, List[np.ndarray]]]:
    """-> {ticker: {'moves': [...14 arrays...], 'cumv': [...]}} oldest first, for sessions before session_date."""
    moves = {t: [] for t in tickers}
    cumv = {t: [] for t in tickers}
    missing = []
    for d in prev_full_sessions(session_date, C.HISTORY_SESSIONS):
        prof = load_session(state_dir, d)
        if prof is None and allow_rebuild and http is not None:
            prof = rebuild_session(http, tickers, d)
            if prof is not None:
                save_session(state_dir, d, prof)
        if prof is None:
            missing.append(d.isoformat())
            continue
        for t in tickers:
            if t in prof:
                moves[t].append(prof[t][0])
                cumv[t].append(prof[t][1])
    return {t: {'moves': moves[t], 'cumv': cumv[t], 'missing': missing} for t in tickers}


def daily_context(df1d: pd.DataFrame, session_date: date):
    """(prev_close, rvol20) from COMPLETED daily bars before session_date (same rule as the backtest's shift(1))."""
    d = df1d[df1d['date'] < session_date.isoformat()]
    close = d['close'].reset_index(drop=True)
    if len(close) < 22:
        return None, None
    ret = np.log(close / close.shift(1))
    rv = ret.rolling(20).std().iloc[-1] * np.sqrt(252)
    rv = float(rv) if pd.notna(rv) and rv > 0 else 0.20
    return float(close.iloc[-1]), rv


def build_contexts(hist, daily: Dict[str, pd.DataFrame], tickers: List[str], session_date: date):
    """-> ({ticker: TickerContext}, {ticker: reason_excluded})"""
    ctxs, excluded = {}, {}
    for t in tickers:
        prev_c, rv = daily_context(daily[t], session_date) if t in daily else (None, None)
        if prev_c is None:
            excluded[t] = 'no daily history'
            continue
        h = hist[t]
        if len(h['moves']) < C.MIN_HISTORY_SESSIONS:
            excluded[t] = f"only {len(h['moves'])} prior sessions available (need {C.MIN_HISTORY_SESSIONS})"
            continue
        ctxs[t] = TickerContext(t, prev_c, rv, h['moves'], h['cumv'])
    return ctxs, excluded
