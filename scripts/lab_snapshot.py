#!/usr/bin/env python3
"""Take lab captures by hand (the live session does this on a schedule).

    python scripts/lab_snapshot.py chains [--label 1130]     # option-chain snapshot of every same-day root
    python scripts/lab_snapshot.py crossasset                # VIX family + cross-asset symbols at this moment
    python scripts/lab_snapshot.py cboe                      # refresh the Cboe daily histories
    python scripts/lab_snapshot.py calendar                  # earnings / dividend calendars for today and the next week
Needs TRADIER_API_KEY for `chains`. Output goes to state/lab/.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apex_chronos import config as C                              # noqa: E402
from apex_chronos.calendar_nyse import NY                         # noqa: E402
from apex_chronos.lab import crossasset, events, snapshots        # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=['chains', 'crossasset', 'cboe', 'calendar'])
    ap.add_argument('--label', default=None)
    ap.add_argument('--state-dir', default='state')
    a = ap.parse_args(argv)
    now = datetime.now(NY)
    d = now.date()
    http = requests.Session()
    if a.what == 'chains':
        label = a.label or now.strftime('%H%M')
        print(snapshots.take_snapshot(a.state_dir, d, label, list(C.TICKERS), force=True))
    elif a.what == 'crossasset':
        print(crossasset.save_capture(a.state_dir, d, a.label or now.strftime('%H%M'), crossasset.capture(http, now)))
    elif a.what == 'cboe':
        print(crossasset.refresh_cboe_history(http, a.state_dir))
    else:
        days = [d + timedelta(days=i) for i in range(-1, 8)]
        syms = set(C.TICKERS) | set(events.MEGACAPS)
        print({k: events.refresh_calendar(http, a.state_dir, k, days, syms) for k in ('earnings', 'dividends')})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
