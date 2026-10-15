"""The exit ladder, with the option price replaced by a straight line so each rule can be hit exactly."""
import numpy as np
import pytest

from apex_chronos import config as C
from apex_chronos.position import Position

S0 = 100.0


class Linear:
    """value = cost * (1 + k*(S/S0 - 1)):  ROI = k * (S/S0 - 1)."""
    def __init__(self, cost, k=50.0):
        self.cost, self.k = cost, k

    def value(self, S, T, iv):
        return self.cost * (1 + self.k * (np.asarray(S) / S0 - 1))


def make(key='P1B', m_entry=30, n_bars=390):
    spec = C.MODULES[key]
    pos = Position(spec, key, 'SPY', '2026-10-01', m_entry, 1, S0, 0.2, n_bars, 20.0, 1.0, 0)
    pos.struct = Linear(pos.cost_basis)
    return pos, spec


def px(roi):
    return S0 * (1 + roi / 50.0)


def test_stop_loss_uses_the_close_not_the_low():
    pos, spec = make()
    ev, closed = pos.on_bar(31, px(0.0), px(-0.40), px(-0.10), 0.2)     # low far below the stop, close above it
    assert closed is None
    ev, closed = pos.on_bar(32, px(0.0), px(-0.40), px(-spec.sl_pct - 0.01), 0.2)
    assert closed['exit_reason'] == 'stop_loss' and closed['roi'] == pytest.approx(-spec.sl_pct - 0.01)


def test_tp1_then_trail_exit_pays_half_target_plus_half_floor():
    pos, spec = make('P1B')                                             # tp1 = 50%
    ev, closed = pos.on_bar(31, px(0.52), px(0.40), px(0.45), 0.2)      # touches TP1; trail = 0.72*0.52 = 37.4% < low 40%
    assert [e['kind'] for e in ev] == ['TP1'] and closed is None and pos.half_scaled
    ev, closed = pos.on_bar(32, px(0.90), px(0.70), px(0.85), 0.2)      # runs to +90%, trail = 0.72 * 0.90 = 64.8% < low 70%
    assert closed is None and pos.dyn_sl == pytest.approx(0.72 * 0.90)
    ev, closed = pos.on_bar(33, px(0.80), px(0.60), px(0.62), 0.2)      # low 60% <= trail 64.8%
    assert closed['exit_reason'] == 'profit_trail'
    assert closed['roi'] == pytest.approx(0.5 * spec.tp1_pct + 0.5 * 0.72 * 0.90)
    assert closed['peak_roi'] == pytest.approx(0.90)


def test_tp1_and_trail_can_trigger_on_the_same_bar_in_the_original_order():
    pos, spec = make('P1B')
    ev, closed = pos.on_bar(31, px(0.70), px(0.0), px(0.30), 0.2)       # best +70% (TP1 + peak), worst 0% (< 0.72*0.70)
    assert [e['kind'] for e in ev] == ['TP1']
    assert closed['exit_reason'] == 'profit_trail'
    assert closed['roi'] == pytest.approx(0.5 * spec.tp1_pct + 0.5 * 0.72 * 0.70)


def test_time_stop_at_m_end_and_half_scaled_blend():
    pos, spec = make('P1A', m_entry=100)                                # hold 50, tp1 90%
    assert pos.m_end == 150
    for m in range(101, 150):
        ev, closed = pos.on_bar(m, px(0.05), px(-0.05), px(0.0), 0.2)
        assert closed is None
    ev, closed = pos.on_bar(150, px(0.05), px(-0.05), px(0.07), 0.2)
    assert closed['exit_reason'] == 'time_stop' and closed['m_exit'] == 150 and closed['roi'] == pytest.approx(0.07)

    pos, spec = make('P1A', m_entry=100)
    pos.half_scaled, pos.locked_half1, pos.dyn_sl, pos.peak_roi = True, spec.tp1_pct, 0.1, 0.5
    ev, closed = pos.on_bar(150, px(0.4), px(0.3), px(0.35), 0.2)
    assert closed['exit_reason'] == 'time_stop'
    assert closed['roi'] == pytest.approx(0.5 * spec.tp1_pct + 0.5 * 0.35)


def test_session_end_caps_the_hold():
    pos, _ = make('P2', m_entry=370, n_bars=390)
    assert pos.m_end == 385                                             # n_bars - 5


def test_after_tp1_the_stop_is_replaced_by_the_trailing_floor():
    pos, spec = make('P1C')                                             # tp1 45%, sl 28%
    ev, closed = pos.on_bar(31, px(0.50), px(0.10), px(0.40), 0.2)      # TP1 and peak 50% -> floor 36%; low 10% is below it
    assert closed['exit_reason'] == 'profit_trail'                      # not 'stop_loss', even though the close is above -28%


def test_plan_matches_spec():
    pos, spec = make('P3')
    plan = pos.plan()
    assert plan['stop_roi'] == -spec.sl_pct and plan['tp1_roi'] == spec.tp1_pct
    assert plan['after_tp1_floor'] == pytest.approx(spec.tp1_pct * 0.6)
