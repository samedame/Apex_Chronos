"""Fill and latency measurement (catalogue items 96, 97).

The bot is alert-only, so the only way to know what a human gets is to record it. After acting on an alert, run
    python -m apex_chronos.lab.fills add --key "<entry key>" --price 0.31 --time 12:04:15 [--side entry|exit] [--qty 1]
The row lands in ledger/lab/fills.csv. `fill_model` then turns the rows plus the quote ledger into an empirical distribution
of (fill price - mid, seconds after alert) by spread width, which replaces the 1-minute-late-costs-19% stress with a measurement.
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

NAN = float('nan')
COLUMNS = ['recorded_at', 'key', 'side', 'price', 'qty', 'fill_time_et', 'note']


def fills_path(ledger_dir: str = 'ledger') -> Path:
    return Path(ledger_dir) / 'lab' / 'fills.csv'


def add_fill(ledger_dir: str, key: str, price: float, fill_time_et: str, side: str = 'entry', qty: int = 1, note: str = '') -> str:
    if side not in ('entry', 'exit'):
        raise ValueError('side must be entry or exit')
    datetime.strptime(fill_time_et, '%H:%M:%S')
    p = fills_path(ledger_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, 'a', newline='') as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        w.writerow([datetime.now().isoformat(timespec='seconds'), key, side, price, qty, fill_time_et, note])
    return str(p)


def read_fills(ledger_dir: str = 'ledger') -> List[Dict]:
    p = fills_path(ledger_dir)
    if not p.exists():
        return []
    with open(p, newline='') as fh:
        return list(csv.DictReader(fh))


def fill_model(fills: List[Dict], quote_recs: List[Dict], alert_clock_et: Dict[str, str]) -> List[Dict]:
    """One row per ENTRY fill: latency = fill time - alert time (seconds), slippage = fill - mid credit at the alert capture,
    expressed per share and as a fraction of the half-spread. `alert_clock_et[key]` is the HH:MM:SS the alert was posted
    (from state/sent-*.json)."""
    from . import quote_ledger as QL
    out = []
    for f in fills:
        if f['side'] != 'entry' or f['key'] not in alert_clock_et:
            continue
        t0 = datetime.strptime(alert_clock_et[f['key']], '%H:%M:%S')
        t1 = datetime.strptime(f['fill_time_et'], '%H:%M:%S')
        m = QL.entry_metrics(quote_recs, f['key'], 1.0)
        mid = m['mid_credit']
        out.append({'key': f['key'], 'latency_s': (t1 - t0).total_seconds(), 'fill': float(f['price']), 'mid_credit': mid,
                    'slippage': (mid - float(f['price'])) if np.isfinite(mid) else NAN,
                    'slippage_over_half_spread': ((mid - float(f['price'])) / m['half_spread_sum']) if np.isfinite(mid) and m['half_spread_sum'] else NAN})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add')
    a.add_argument('--key', required=True)
    a.add_argument('--price', type=float, required=True)
    a.add_argument('--time', required=True, help='HH:MM:SS Eastern')
    a.add_argument('--side', default='entry')
    a.add_argument('--qty', type=int, default=1)
    a.add_argument('--note', default='')
    a.add_argument('--ledger-dir', default='ledger')
    args = ap.parse_args(argv)
    print(add_fill(args.ledger_dir, args.key, args.price, args.time, args.side, args.qty, args.note))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
