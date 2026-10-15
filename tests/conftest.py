import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apex_chronos import config as C          # noqa: E402
from apex_chronos import replay                # noqa: E402
from verification.fixtures import extract      # noqa: E402


@pytest.fixture(scope='session')
def data_dir(tmp_path_factory):
    return extract(os.path.join(ROOT, 'verification', 'fixtures.tar.gz'), str(tmp_path_factory.mktemp('fixtures')))


@pytest.fixture(scope='session')
def days(data_dir):
    """(dates, {date: {ticker: TickerDay}}) for the 12 strategy tickers, exactly as the original loads them."""
    return replay.build_days(data_dir, list(C.TICKERS))


@pytest.fixture(scope='session')
def fast_run(days):
    """(trades DataFrame, candidates DataFrame, [(date, Event)]) from the bot, fast mode."""
    df, events = replay.run_all(None, list(C.TICKERS), mode='fast', days_cache=days)
    return df, replay.run_all.last_candidates, events
