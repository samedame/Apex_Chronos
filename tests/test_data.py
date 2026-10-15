from datetime import date

import numpy as np
import pandas as pd
import pytest

from apex_chronos import data
from apex_chronos.calendar_nyse import NY

D = date(2026, 10, 15)


def frame(minutes, extra=None, base=100.0):
    ts = [pd.Timestamp(f'2026-10-15 09:30:00', tz=NY) + pd.Timedelta(minutes=m) for m in minutes]
    if extra:
        ts += [pd.Timestamp(e, tz=NY) for e in extra]
    n = len(ts)
    px = base + np.arange(n) * 0.1
    return pd.DataFrame({'timestamp': ts, 'open': px, 'high': px + 0.2, 'low': px - 0.2, 'close': px + 0.05, 'volume': np.full(n, 1000.0)})


def test_completed_bars_land_on_the_clock_grid():
    sb = data.to_grid(frame(range(0, 12)), D, 'SPY', 11)
    assert sb.ok and len(sb.C) == 12 and sb.holes == [] and sb.last_real == 11


def test_the_in_progress_bar_is_dropped():
    df = frame(range(0, 6), extra=['2026-10-15 09:36:37'])          # Yahoo stamps the forming bar with the last-trade time
    sb = data.to_grid(df, D, 'SPY', 10)
    assert sb.ok and len(sb.C) == 6 and sb.last_real == 5


def test_a_missing_minute_is_forward_filled_and_reported():
    sb = data.to_grid(frame([0, 1, 2, 3, 5, 6]), D, 'SPY', 6)
    assert sb.holes == [4] and len(sb.C) == 7
    assert sb.C[4] == sb.C[3] and sb.O[4] == sb.C[3] and sb.V[4] == 0.0


def test_no_opening_bar_means_no_trading_that_ticker():
    sb = data.to_grid(frame(range(1, 12)), D, 'SPY', 11)
    assert not sb.ok and 'missing' in sb.reason
    assert not data.to_grid(pd.DataFrame(), D, 'SPY', 5).ok


def test_bars_after_the_target_minute_are_not_used():
    sb = data.to_grid(frame(range(0, 30)), D, 'SPY', 9)
    assert len(sb.C) == 10


def test_lagging_ticker_is_padded_flat():
    sb = data.to_grid(frame(range(0, 5)), D, 'SPY', 9)
    sb = data.pad_to(sb, 10)
    assert len(sb.C) == 10 and np.all(sb.C[5:] == sb.C[4]) and np.all(sb.V[5:] == 0) and sb.last_real == 4


def test_vix_alignment_ffills_and_reports_staleness():
    v = frame(range(0, 20), base=18.0)
    v = v[v['timestamp'] <= pd.Timestamp('2026-10-15 09:44:00', tz=NY)]      # feed stops at 09:44
    arr, stale = data.align_vix(v, D, 30)
    assert len(arr) == 30 and stale == 29 - 14
    assert arr[20] == arr[14]                                                 # forward-filled, the original's behaviour
    arr, stale = data.align_vix(pd.DataFrame(), D, 5)
    assert stale == 999 and np.all(arr == 18.0)


def test_chart_payload_with_null_bars_is_cleaned():
    payload = {'chart': {'result': [{'timestamp': [1760535000, 1760535060, 1760535120],
                                     'indicators': {'quote': [{'open': [1.0, None, 3.0], 'high': [1.1, None, 3.1], 'low': [0.9, None, 2.9],
                                                               'close': [1.05, None, 3.05], 'volume': [10, None, 30]}]}}]}}
    df = data._frame(payload)
    assert len(df) == 2
    with pytest.raises(data.FeedError):
        data._frame({'chart': {'result': None}})
