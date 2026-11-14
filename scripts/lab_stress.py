#!/usr/bin/env python3
"""Score a trade list from a stress-session replay against the deployment gate.

    python scripts/lab_stress.py trades.csv [--slot-fraction 0.10]          # CSV columns: date,ticker,direction,m_entry,m_exit,roi
    python scripts/lab_stress.py --synthetic                                # fixture: three index spreads stopped out the same session
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apex_chronos import verdict                                  # noqa: E402
from apex_chronos.lab import stress                               # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('csv', nargs='?')
    ap.add_argument('--slot-fraction', type=float, default=verdict.DEFAULT_SLOT_FRACTION)
    ap.add_argument('--synthetic', action='store_true')
    a = ap.parse_args(argv)
    if a.synthetic:
        trades = stress.synthetic_crash_trades()
    elif a.csv:
        with open(a.csv, newline='') as fh:
            trades = [dict(r, m_entry=int(r['m_entry']), m_exit=int(r['m_exit']), roi=float(r['roi'])) for r in csv.DictReader(fh)]
    else:
        ap.error('give a trades CSV or --synthetic')
    res = stress.score(trades, a.slot_fraction)
    for label, r in res.items():
        print(f"{label}: trades {r['trades']}, worst session {r['worst'] * 100:+.1f}%, CVaR95 {r['tail']['cvar95'] * 100:+.1f}%, "
              f"session-loss gate {'ok' if r['gate_session_loss_ok'] else 'FAIL'}, kill switch fires after a loss session "
              f"{'yes' if r['kill_fires_after_loss'] else 'NO'} -> {'PASS' if r['pass'] else 'FAIL'}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
