import numpy as np
import pandas as pd
import pytest

from apex_chronos.features import (InsufficientHistory, TickerContext, avg_profile, compute_features, vol_multiplier)


def one_day(days):
    dts, by_day = days
    d = dts[8]
    return d, by_day[d]['SPY']


def test_vwap_and_ema_follow_their_definitions(days):
    _, td = one_day(days)
    f = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
    typ = (td.H + td.L + td.C) / 3
    assert f.vwap == pytest.approx(np.cumsum(typ * td.V) / np.maximum(np.cumsum(td.V), 1.0))
    assert f.ema9 == pytest.approx(pd.Series(td.C).ewm(span=9, adjust=False).mean().values)
    assert f.ema21 == pytest.approx(pd.Series(td.C).ewm(span=21, adjust=False).mean().values)
    assert (f.vwap_std > 0).all() and (f.iv >= 0.10).all() and (f.iv <= 2.20).all()


@pytest.mark.parametrize('k', [20, 75, 180, 389])
def test_features_never_use_future_bars(days, k):
    """Features computed from the first k bars equal the first k values of the full-day features."""
    for t in ('SPY', 'NVDA', 'COIN'):
        dts, by_date = days
        td = by_date[dts[5]][t]
        full = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
        part = compute_features(td.ctx, td.O[:k], td.H[:k], td.L[:k], td.C[:k], td.V[:k], td.vix[:k])
        for name in ('vwap', 'vwap_std', 'noise_upper', 'rvol_cum', 'ema9', 'ema21', 'iv'):
            assert np.array_equal(getattr(part, name), getattr(full, name)[:k]), (t, name, k)


def test_refuses_to_trade_without_enough_history(days):
    _, td = one_day(days)
    short = TickerContext('SPY', td.ctx.prev_c, td.ctx.rvol20, td.ctx.hist_moves[:3], td.ctx.hist_cumv[:3])
    with pytest.raises(InsufficientHistory):
        compute_features(short, td.O, td.H, td.L, td.C, td.V, td.vix)


def test_baseline_uses_the_last_14_sessions_and_edge_pads_short_ones():
    hist = [np.full(10, float(i)) for i in range(20)]
    out = avg_profile(hist, 10)
    assert out == pytest.approx(np.full(10, np.mean(range(6, 20))))
    mixed = [np.ones(5)] * 5 + [np.full(8, 3.0)] * 5
    assert len(avg_profile(mixed, 8)) == 8


def test_vol_multiplier_rules():
    assert vol_multiplier('SPY', 0.5) == 1.0
    assert vol_multiplier('IWM', 0.05) == 1.25
    assert vol_multiplier('NVDA', 0.16) == 1.0
    assert vol_multiplier('NVDA', 0.48) == pytest.approx(3.0)
    assert vol_multiplier('TSLA', 0.05) == 1.0
