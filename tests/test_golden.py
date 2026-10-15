"""The bot reproduces the ORIGINAL backtest code's output (tests/golden = that output on the fixtures)."""
import os

import pandas as pd

from apex_chronos import config as C
from apex_chronos import replay
from verification.verify_against_backtest import compare_candidates, compare_trades, scheduler_invariants

GOLD = os.path.join(os.path.dirname(__file__), 'golden')


def test_trades_match_original_trade_for_trade(fast_run):
    gold = pd.read_csv(os.path.join(GOLD, 'oracle_trades_12.csv'))
    cmp = compare_trades(gold, fast_run[0])
    assert len(gold) == 110
    assert cmp['decisions_equal'], (cmp['only_oracle'][:3], cmp['only_bot'][:3])
    assert cmp['numeric_ok'], cmp['max_abs_diff']


def test_candidates_match_original(fast_run):
    gold = pd.read_csv(os.path.join(GOLD, 'oracle_candidates_12.csv'))
    cmp = compare_candidates(gold, fast_run[1])
    assert cmp['ok'], (cmp['only_oracle'][:3], cmp['only_bot'][:3])


def test_scheduler_invariants_hold(fast_run):
    assert scheduler_invariants(fast_run[0])['ok']


def test_live_path_equals_fast_path_on_busy_days(days, fast_run):
    """Rebuilding features minute by minute from only the bars seen so far (what a live poll does) changes nothing."""
    dts, by_day = days
    busiest = fast_run[0].groupby('date').size().sort_values(ascending=False).index[:3]
    sub = (dts, {d: by_day[d] for d in busiest})
    live, _ = replay.run_all(None, list(C.TICKERS), mode='livepath', days_cache=sub)
    ref = fast_run[0][fast_run[0]['date'].isin(busiest)]
    cmp = compare_trades(ref, live)
    assert cmp['ok'], cmp


def test_burst_polling_equals_minute_polling(days, fast_run):
    dts, by_day = days
    d = fast_run[0].groupby('date').size().idxmax()
    sub = (dts, {d: by_day[d]})
    burst, _ = replay.run_all(None, list(C.TICKERS), mode='burst', burst=11, days_cache=sub)
    assert compare_trades(fast_run[0][fast_run[0]['date'] == d], burst)['ok']
