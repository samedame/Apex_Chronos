import numpy as np
import pytest

from apex_chronos import config as C
from apex_chronos.pricing import (Structure, bs_option_price_and_delta, half_spread_for, minutes_to_T, select_strike)

R = C.RISK_FREE


def test_put_call_parity():
    S, K, T, sig = 100.0, 95.0, 0.02, 0.35
    c, _ = bs_option_price_and_delta(S, K, T, R, sig, True)
    p, _ = bs_option_price_and_delta(S, 105.0, T, R, sig, False)
    c2, _ = bs_option_price_and_delta(S, 105.0, T, R, sig, True)
    assert c - bs_option_price_and_delta(S, K, T, R, sig, False)[0] == pytest.approx(S - K * np.exp(-R * T), abs=1e-9)
    assert c2 - p == pytest.approx(S - 105.0 * np.exp(-R * T), abs=1e-9)


def test_delta_signs_and_price_floor():
    _, dc = bs_option_price_and_delta(100.0, 100.0, 0.01, R, 0.3, True)
    _, dp = bs_option_price_and_delta(100.0, 100.0, 0.01, R, 0.3, False)
    assert 0 < dc < 1 and -1 < dp < 0
    p, _ = bs_option_price_and_delta(100.0, 150.0, 0.001, R, 0.2, True)   # deep OTM
    assert p == 0.01                                                      # the original's 1-cent floor


def test_minutes_to_T_matches_original_formula():
    assert minutes_to_T(0) == pytest.approx((390 + 25) / (390 * 252))
    assert minutes_to_T(389) == pytest.approx((1 + 25) / (390 * 252))
    assert minutes_to_T(100) > minutes_to_T(101)


@pytest.mark.parametrize('ticker,S,step', [('SPY', 650.0, 1.0), ('NVDA', 220.0, 1.0), ('META', 700.0, 2.5), ('TSLA', 300.0, 1.0)])
def test_strike_grid(ticker, S, step):
    K = select_strike(S, minutes_to_T(30), 0.30, 0.45, True, ticker)
    assert K / step == pytest.approx(round(K / step))


def test_strike_delta_is_close_to_target():
    S, T, sig = 100.0, minutes_to_T(20), 0.40
    K = select_strike(S, T, sig, 0.45, True, 'SPY')
    _, d = bs_option_price_and_delta(S, K, T, R, sig, True)
    assert d == pytest.approx(0.45, abs=0.06)


def test_half_spread_buckets():
    assert half_spread_for('SPY') == C.HALF_SPREAD_TIGHT
    assert half_spread_for('MSTR') == C.HALF_SPREAD_WIDE


@pytest.mark.parametrize('structure,delta', [('single', 0.45), ('debit_spread', 0.5), ('credit_spread', 0.32)])
def test_structures_are_priced_sanely(structure, delta):
    S, iv, T = 650.0, 0.22, minutes_to_T(15)
    for direction in (1, -1):
        st = Structure('SPY', structure, direction, S, iv, T, delta)
        assert st.cost_basis > 0
        roi_now = float(st.roi(np.array([S]), T, iv)[0])
        assert roi_now < 0.05                      # you cannot make money on the entry bar itself (spread + cushion)
        if structure != 'single':
            assert st.K1 != st.K2
        if structure == 'credit_spread':
            assert st.cost_basis == pytest.approx(max(st.width - st.credit, 0.25))
            assert 0 < st.credit < st.width


def test_long_call_gains_when_underlying_rises():
    st = Structure('SPY', 'single', 1, 650.0, 0.2, minutes_to_T(10), 0.45)
    lo, hi = st.value(np.array([648.0, 653.0]), minutes_to_T(15), 0.2)
    assert hi > lo


def test_credit_spread_is_a_bet_against_the_move():
    st = Structure('SPY', 'credit_spread', -1, 650.0, 0.2, minutes_to_T(90), 0.32)   # direction -1 -> short call spread
    up, down = st.value(np.array([655.0, 645.0]), minutes_to_T(100), 0.2)
    assert down > up
