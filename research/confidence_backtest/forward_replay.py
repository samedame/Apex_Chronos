"""Rebuild the live forward sessions from real 1-minute bars and replay them through the bot's own engine.

Why: the forward ledger (ledger/trades.csv) records only the trades. To test anything about *why* a trade won or lost
we need the bars, and Yahoo only keeps ~30 days of 1-minute history, so this snapshots them to
research/confidence_backtest/data/forward_1m.csv.gz while they still exist.

    python research/confidence_backtest/forward_replay.py --fetch     # download + snapshot the bars (needs network)
    python research/confidence_backtest/forward_replay.py             # replay from the snapshot, compare with the ledger

The replay uses exactly the end-of-day reconcile path (runner.final_day_inputs): same baselines from state/history,
same daily context, same engine. If it reproduces ledger/trades.csv trade for trade, the snapshot IS the bot's data.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apex_chronos import config as C, data, history, replay   # noqa: E402
from apex_chronos.replay import TickerDay                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, 'data', 'forward_1m.csv.gz')
SYMS = list(C.TICKERS) + ['^VIX']


def ledger_dates(as_of: str = '9999-12-31'):
    with open(os.path.join(ROOT, 'ledger', 'trades.csv')) as fh:
        return sorted({r['date'] for r in csv.DictReader(fh) if r['date'] <= as_of})


def fetch(dates):
    import requests
    http = requests.Session()
    lo, hi = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    frames = []
    for s in SYMS:
        df = data.fetch_1m_range(http, s, lo, hi)
        df = df[df['timestamp'].dt.strftime('%Y-%m-%d').isin(dates)].copy()      # drops the stray in-progress bar of "today"
        df['sym'] = s
        frames.append(df)
        print(f'{s:6s} {len(df):6d} bars over {df["timestamp"].dt.date.nunique()} sessions')
    out = pd.concat(frames, ignore_index=True)
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    out.to_csv(SNAP, index=False, compression='gzip')
    print('saved', SNAP, len(out))


def load_snapshot() -> pd.DataFrame:
    df = pd.read_csv(SNAP)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert(data.NY)
    return df


def build_forward_days(dates, http=None):
    """-> {date: {ticker: TickerDay}} for the forward sessions, built exactly like runner.final_day_inputs."""
    snap = load_snapshot()
    by_sym = {s: g.drop(columns='sym').reset_index(drop=True) for s, g in snap.groupby('sym')}
    daily = {}
    import requests
    http = http or requests.Session()
    for t in C.TICKERS:
        daily[t] = data.fetch_1d(http, t)
    out = {}
    for ds in dates:
        d = date.fromisoformat(ds)
        hist = history.load_history(os.path.join(ROOT, 'state'), list(C.TICKERS), d, http=None, allow_rebuild=False)
        ctxs, excluded = history.build_contexts(hist, daily, list(C.TICKERS), d)
        vix_df = by_sym['^VIX']
        day = {}
        for t in C.TICKERS:
            if t not in ctxs:
                continue
            sb = data.to_grid(by_sym[t], d, t, C.SESSION_BARS - 1)
            if not sb.ok or len(sb.C) < C.MIN_SESSION_BARS:
                continue
            sb = data.pad_to(sb, C.SESSION_BARS)
            vix, _ = data.align_vix(vix_df, d, len(sb.C))
            day[t] = TickerDay(ctxs[t], sb.O, sb.H, sb.L, sb.C, sb.V, vix)
        out[ds] = day
    return out


def replay_forward(dates, days=None):
    days = days or build_forward_days(dates)
    trades, events = [], []
    for ds in dates:
        eng, ev = replay.run_session(ds, days[ds], list(C.TICKERS), mode='fast')
        trades += eng.trades
        events += [(ds, e) for e in ev]
    return pd.DataFrame(trades), events, days


def compare_with_ledger(trades: pd.DataFrame, dates) -> dict:
    with open(os.path.join(ROOT, 'ledger', 'trades.csv')) as fh:
        led = [r for r in csv.DictReader(fh) if r['date'] in set(dates)]
    k = lambda r: (r['date'], r['ticker'], r['module'], int(r['m_entry']))      # noqa: E731
    a = {k(r): r for r in led}
    b = {k(r): r for r in trades.to_dict('records')}
    both = set(a) & set(b)
    worst = max((abs(float(a[x]['roi']) - float(b[x]['roi'])) for x in both), default=0.0)
    return {'ledger': len(a), 'replay': len(b), 'matched': len(both), 'only_ledger': sorted(set(a) - set(b)),
            'only_replay': sorted(set(b) - set(a)), 'max_abs_roi_diff': worst}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--fetch', action='store_true')
    ap.add_argument('--as-of', default='9999-12-31')
    a = ap.parse_args()
    dts = ledger_dates(a.as_of)
    if a.fetch:
        fetch(dts)
    tr, ev, _ = replay_forward(dts)
    print(compare_with_ledger(tr, dts))
