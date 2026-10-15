"""Regenerate tests/golden/*.csv: the ORIGINAL backtest code's output on the fixtures (12 tickers).

    python scripts/make_golden.py          (run from the repo root)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apex_chronos import config as C                      # noqa: E402
from verification import reference_runner as rr           # noqa: E402
from verification.fixtures import extract                 # noqa: E402

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data = extract(os.path.join(root, 'verification', 'fixtures.tar.gz'))
ref = rr.run_reference(data, list(C.TICKERS))
os.makedirs(os.path.join(root, 'tests', 'golden'), exist_ok=True)
ref['df_exec'].to_csv(os.path.join(root, 'tests', 'golden', 'oracle_trades_12.csv'), index=False)
ref['df_c'].to_csv(os.path.join(root, 'tests', 'golden', 'oracle_candidates_12.csv'), index=False)
print(len(ref['df_exec']), 'trades,', len(ref['df_c']), 'candidates written')
