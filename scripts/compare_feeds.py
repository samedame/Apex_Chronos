"""Compare today's completed 1-minute bars from Yahoo and Tradier for the bot's tickers, before turning CHRONOS_TRADIER_BARS on.

    TRADIER_API_KEY=... python scripts/compare_feeds.py [TICKER ...]      (run from the repo root, after the close)

For each ticker prints the number of bars each feed has, how many minutes both have, the largest high/low/close difference (as a
share of price) and the volume ratio Tradier/Yahoo. The strategy's thresholds were fitted on Yahoo bars, so a volume ratio far
from 1.0 (RVOL uses volume) or price differences above ~0.05% are reasons to leave the switch off.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apex_chronos import config as C, data, tradier  # noqa: E402


def compare(sym, http):
    y = data._frame(data._get(http, sym, {'interval': '1m', 'range': '1d', 'includePrePost': 'false'}))
    y = y[y['timestamp'].dt.second == 0].set_index('timestamp')
    t = data.tradier_frame(sym).set_index('timestamp')
    both = y.index.intersection(t.index)
    if len(both) == 0:
        return f'{sym:6s} yahoo {len(y):3d} bars | tradier {len(t):3d} bars | no common minutes'
    yy, tt = y.loc[both], t.loc[both]
    worst = max(((yy[c] - tt[c]).abs() / yy[c]).max() for c in ('high', 'low', 'close'))
    vy, vt = yy['volume'].sum(), tt['volume'].sum()
    ratio = (vt / vy) if vy else float('nan')
    return (f'{sym:6s} yahoo {len(y):3d} bars | tradier {len(t):3d} bars | common {len(both):3d} | '
            f'max price diff {worst * 100:.3f}% | volume tradier/yahoo {ratio:.2f}')


def main(argv):
    if not tradier.configured():
        sys.exit('set TRADIER_API_KEY (and TRADIER_BASE_URL for a sandbox key)')
    http = requests.Session()
    for sym in (argv or list(C.TICKERS)):
        try:
            print(compare(sym, http))
        except Exception as e:  # keep going: one bad ticker should not hide the rest
            print(f'{sym:6s} FAILED: {type(e).__name__}: {e}')


if __name__ == '__main__':
    main(sys.argv[1:])
