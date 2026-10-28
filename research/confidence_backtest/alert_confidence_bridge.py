"""Score forward trades with the lateness the live bot actually posted (the one execution input that exists in the record)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apex_chronos import alert_confidence as AC, config as C   # noqa: E402


def score_as_sent(df):
    return [AC.score_alert({'module': C.MODULES[r.module].name, 'ticker': r.ticker, 'late_s': r.late_s_sent,
                            'data_age_s': 900.0, 'contracts': None}).score for r in df.itertuples()]
