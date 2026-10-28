"""Regenerate the setup-record table used by apex_chronos/alert_confidence.py.

    python scripts/refit_confidence.py                 # print the table
    python scripts/refit_confidence.py --as-of 2026-10-28

Two samples, kept apart on purpose:
  bt  - the 110 backtest trades, replayed from verification/fixtures.tar.gz (offline, deterministic)
  fwd - the live forward ledger (ledger/trades.csv), rows dated on or before --as-of

Nothing here fits anything beyond counts: (n, wins, mean ROI) per module and sample. The score's shrinkage and
anchors live in alert_confidence.py and are not tuned on these numbers. It also prints the within-module checks
(RVOL, entry minute, rank score vs ROI, backtest against forward) that were run and rejected as score factors.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apex_chronos import config as C   # noqa: E402

SHORT = {spec.name: key for key, spec in C.MODULES.items()}


def backtest_rows():
    from apex_chronos import replay
    from verification.fixtures import extract
    d = extract(os.path.join(ROOT, 'verification', 'fixtures.tar.gz'), tempfile.mkdtemp())
    days = replay.build_days(d, list(C.TICKERS))
    df, _ = replay.run_all(None, list(C.TICKERS), mode='fast', days_cache=days)
    return df.to_dict('records')


def forward_rows(as_of: str):
    with open(os.path.join(ROOT, 'ledger', 'trades.csv')) as fh:
        return [r for r in csv.DictReader(fh) if r['date'] <= as_of]


def table(rows):
    out = {}
    for r in rows:
        k = SHORT[r['module']]
        roi = float(r['roi'])
        n, w, s = out.get(k, (0, 0, 0.0))
        out[k] = (n + 1, w + (roi > 0), s + roi)
    return {k: (n, w, round(s / n, 4)) for k, (n, w, s) in out.items()}


def factor_checks(bt, fwd):
    from scipy.stats import spearmanr   # analysis only; not a runtime dependency
    print('\nWithin-module Spearman(ROI, factor)  [rho, p]  backtest | forward')
    for mod in ('P1A', 'P2'):
        for f in ('rvol_entry', 'm_entry', 'score'):
            cols = []
            for rows in (bt, fwd):
                sub = [r for r in rows if SHORT[r['module']] == mod]
                rho, p = spearmanr([float(r[f]) for r in sub], [float(r['roi']) for r in sub])
                cols.append(f'n={len(sub):>3} rho={rho:+.2f} p={p:.2f}')
            print(f'  {mod:<4}{f:<11}{cols[0]}  |  {cols[1]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', default=None, help='last ledger date to include (default: all rows)')
    ap.add_argument('--checks', action='store_true', help='also print the within-module factor checks')
    a = ap.parse_args()
    bt = backtest_rows()
    fwd = forward_rows(a.as_of or '9999-12-31')
    as_of = a.as_of or max(r['date'] for r in fwd)
    print(f"AS_OF = '{as_of}'")
    print('RECORD = {')
    tb, tf = table(bt), table(fwd)
    for k in C.MODULE_ORDER:
        print(f"    {k!r}: {{'bt': {tb.get(k, (0, 0, 0.0))}, 'fwd': {tf.get(k, (0, 0, 0.0))}}},")
    print('}')
    if a.checks:
        factor_checks(bt, fwd)


if __name__ == '__main__':
    main()
