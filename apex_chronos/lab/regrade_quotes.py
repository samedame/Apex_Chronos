"""Regrade archived P2 trades on REAL historical quotes (catalogue item 110; the research's top-ranked data step).

Input: a CSV of one-minute NBBO for the option legs, exported from ThetaData, Massive/Polygon, Databento or any other source:
    timestamp_et,symbol,bid,ask            (timestamp like 2026-11-13 12:04:00, symbol in OCC format)
Nothing here downloads data (that needs a paid subscription); this module only replays the bot's exits on the quotes it is given.

Grading rule for one trade: enter at the first quote at or after the entry bar close (mid, and at the touch), then on each later
minute close the spread when the MODEL position (position.Position on the archived bars) would have exited, and also report the
spread's mark-to-market path. Entry at the touch = sell short at bid, buy long at ask; exit at the touch = buy short at ask,
sell long at bid. ROI is relative to collateral (width - credit), the same denominator as the ledger.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from ..calendar_nyse import NY, session_open
from .snapshots import occ_symbol

NAN = float('nan')


def load_quotes(path: str) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """{(symbol, 'YYYY-MM-DD HH:MM'): (bid, ask)}"""
    out = {}
    with open(path, newline='') as fh:
        for r in csv.DictReader(fh):
            try:
                out[(r['symbol'].upper(), r['timestamp_et'][:16])] = (float(r['bid']), float(r['ask']))
            except (KeyError, ValueError):
                continue
    return out


def _clock(d: date, m: int) -> str:
    t = 9 * 60 + 30 + m
    return f'{d.isoformat()} {t // 60:02d}:{t % 60:02d}'


def regrade_trade(d: date, ticker: str, direction: int, K1: float, K2: float, m_entry: int, m_exit: int,
                  quotes: Dict[Tuple[str, str], Tuple[float, float]]) -> Dict[str, float]:
    is_put = direction == 1
    ssym, lsym = occ_symbol(ticker, d, not is_put, K1), occ_symbol(ticker, d, not is_put, K2)
    qe_s, qe_l = quotes.get((ssym, _clock(d, m_entry + 1))), quotes.get((lsym, _clock(d, m_entry + 1)))
    qx_s, qx_l = quotes.get((ssym, _clock(d, m_exit + 1))), quotes.get((lsym, _clock(d, m_exit + 1)))
    blank = {k: NAN for k in ('credit_mid', 'credit_taker', 'close_mid', 'close_taker', 'roi_mid', 'roi_taker', 'width')}
    if not (qe_s and qe_l and qx_s and qx_l):
        return {**blank, 'status': 'missing_quotes'}
    width = abs(K1 - K2)
    credit_mid = (sum(qe_s) / 2) - (sum(qe_l) / 2)
    credit_taker = qe_s[0] - qe_l[1]
    close_mid = (sum(qx_s) / 2) - (sum(qx_l) / 2)
    close_taker = qx_s[1] - qx_l[0]
    out = {'credit_mid': credit_mid, 'credit_taker': credit_taker, 'close_mid': close_mid, 'close_taker': close_taker, 'width': width,
           'status': 'ok'}
    out['roi_mid'] = (credit_mid - close_mid) / (width - credit_mid) if width > credit_mid > 0 else NAN
    out['roi_taker'] = (credit_taker - close_taker) / (width - credit_taker) if width > credit_taker > 0 else NAN
    return out


def regrade_table(trades: Iterable[Dict], quotes: Dict[Tuple[str, str], Tuple[float, float]]) -> List[Dict]:
    """trades: dicts with date, ticker, direction, K1, K2, m_entry, m_exit (shadow-ledger rows). Rows get the regrade columns."""
    out = []
    for t in trades:
        d = date.fromisoformat(t['date'])
        r = regrade_trade(d, t['ticker'], int(t['direction']), float(t['K1']), float(t['K2']), int(t['m_entry']), int(t['m_exit']), quotes)
        out.append({**t, **{f'real_{k}': v for k, v in r.items()}})
    return out
