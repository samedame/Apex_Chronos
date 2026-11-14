"""Lab: unit tests for the pure functions, with known answers."""
import math
import time
from datetime import date

import numpy as np
import pytest

from apex_chronos.lab import (events as EV, fills, indicators as I, killswitch_alt as KS, options_math as om, quote_ledger as QL,
                              regrade_quotes as RQ, snapshots as SN, stats)

NAN = float('nan')


# ---- indicators -----------------------------------------------------------------------------------------------------------
def test_realised_var_of_known_path():
    c = 100 * np.exp(np.cumsum(np.full(60, 0.001)))
    assert I.realised_var(c, 0, 59) == pytest.approx(59 * 0.001 ** 2, rel=1e-9)


def test_estimators_are_zero_or_nan_not_negative_on_flat_prices():
    flat = np.full(100, 50.0)
    assert I.realised_var(flat) == 0.0
    assert I.parkinson(flat, flat) == 0.0


def _ar1_path(phi, n=3000, seed=2):
    rng = np.random.default_rng(seed)
    e = rng.normal(0, 0.001, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return 100 * np.exp(np.cumsum(r))


def test_variance_ratio_separates_trending_from_mean_reverting():
    rng = np.random.default_rng(1)
    rw = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 4000)))
    assert 0.85 < I.variance_ratio(rw, 5) < 1.15
    assert I.variance_ratio(_ar1_path(0.5), 5) > 1.3          # positive autocorrelation: trending
    assert I.variance_ratio(_ar1_path(-0.5), 5) < 0.7         # negative autocorrelation: mean reverting (the P2-friendly state)


def test_qlike_minimised_at_truth():
    vals = [I.qlike(f, 1.0) for f in (0.5, 0.9, 1.0, 1.1, 2.0)]
    assert vals[2] == 0.0 and all(v > 0 for i, v in enumerate(vals) if i != 2)
    assert math.isnan(I.qlike(-1.0, 1.0))


def test_noise_area_symmetry_and_inside():
    o = np.full(390, 100.0)
    prior = [(o, np.full(390, 101.0)) for _ in range(14)]            # |C_m/O_0 - 1| = 1% every day
    na = I.noise_area(prior, 100.0, 100.0, 60)
    assert na['sigma'] == pytest.approx(0.01)
    assert na['upper'] == pytest.approx(101.0) and na['lower'] == pytest.approx(99.0)
    assert I.inside_noise_area(100.5, na) == 1.0 and I.inside_noise_area(102.0, na) == 0.0
    assert I.strike_beyond_band(100.0, 103.0, na) == pytest.approx(2.0)      # (103 - 101) / 1.0
    assert I.strike_beyond_band(100.0, 98.0, na) == pytest.approx(1.0)       # (99 - 98) / 1.0


def test_noise_area_needs_history():
    assert math.isnan(I.noise_area([(np.ones(390), np.ones(390))] * 2, 1.0, 1.0, 10)['upper'])


# ---- options math -----------------------------------------------------------------------------------------------------------
def test_implied_vol_round_trip_and_put_call_parity_sign():
    T = om.T_years(180)
    for is_call in (True, False):
        px = om.bs_price(100.0, 102.0, T, 0.4, is_call)
        assert om.implied_vol(px, 100.0, 102.0, T, is_call) == pytest.approx(0.4, abs=1e-4)


def test_touch_probability_properties():
    near = om.touch_probability(100.0, 101.0, 0.3, 180)
    far = om.touch_probability(100.0, 105.0, 0.3, 180)
    hi_vol = om.touch_probability(100.0, 101.0, 0.6, 180)
    assert 0 <= far < near <= 1 and hi_vol > near
    assert om.touch_probability(100.0, 100.0, 0.3, 180) == 1.0


def test_straddle_mid_is_the_implied_move_not_haircut():
    assert om.implied_move_from_straddle(2.5, 1.5) == 4.0
    assert om.implied_sigma_move(2.5, 1.5) == pytest.approx(4.0 / math.sqrt(2 / math.pi))
    assert math.isnan(om.implied_move_from_straddle(0.0, 1.5))


def test_credit_spread_metrics():
    s = {'b': 1.00, 'a': 1.10}
    l = {'b': 0.20, 'a': 0.30}
    m = om.credit_spread_metrics(s, l, 5.0)
    assert m['mid_credit'] == pytest.approx(0.80) and m['taker_credit'] == pytest.approx(0.70)
    assert m['half_spread_sum'] == pytest.approx(0.05 + 0.05)
    assert m['spread_pct_credit'] == pytest.approx(0.10 / 0.80)


def test_gex_sign_convention_and_zero_gamma_finite():
    rows = [{'k': 100.0, 't': 'c', 'g': 0.05, 'oi': 1000}, {'k': 100.0, 't': 'p', 'g': 0.05, 'oi': 400}]
    g = om.gex_from_chain(rows, 100.0)
    assert g['sign'] == 1 and g['gex'] == pytest.approx((1000 - 400) * 0.05 * 100 * 100 * 100 * 0.01)
    assert math.isnan(om.gex_from_chain([], 100.0)['gex'])


# ---- events -----------------------------------------------------------------------------------------------------------------
def test_calendar_facts():
    assert EV.third_friday(2026, 11) == date(2026, 11, 20)
    assert EV.monthly_opex(date(2026, 11, 20)) and not EV.monthly_opex(date(2026, 11, 13))
    assert EV.triple_witching(date(2026, 12, 18)) and not EV.triple_witching(date(2026, 11, 20))
    assert EV.macro_flags(date(2026, 12, 9))['fomc_day'] == 1.0
    assert EV.macro_flags(date(2026, 12, 10))['cpi_day'] == 1.0


def test_fomc_mute_only_from_noon_on_decision_day():
    assert EV.fomc_mute(date(2026, 12, 9), 149) == 0.0 and EV.fomc_mute(date(2026, 12, 9), 150) == 1.0
    assert EV.fomc_mute(date(2026, 12, 8), 300) == 0.0


# ---- stats ----------------------------------------------------------------------------------------------------------------------
def test_clustered_bounds_are_deterministic_and_ordered():
    rng = np.random.default_rng(3)
    v = rng.normal(0.01, 0.05, 60)
    s = [f'd{i // 3}' for i in range(60)]
    a, b = stats.clustered_mean_lb(v, s), stats.clustered_mean_lb(v, s)
    assert a == b and a < v.mean()
    assert math.isnan(stats.clustered_mean_lb(v[:6], s[:6]))                    # fewer than 3 sessions


def test_clustered_diff_detects_a_real_gap():
    rng = np.random.default_rng(4)
    a = rng.normal(0.05, 0.02, 80); b = rng.normal(-0.05, 0.02, 80)
    s = [f'd{i // 2}' for i in range(80)]
    r = stats.clustered_diff(a, s, b, s)
    assert r['diff'] > 0.08 and r['p'] < 0.01 and r['lb95'] > 0


def test_holm_is_monotone_and_never_below_raw():
    adj = stats.holm({'a': 0.01, 'b': 0.03, 'c': 0.04, 'd': NAN})
    assert adj['a'] == pytest.approx(0.03) and adj['b'] == pytest.approx(0.06) and adj['c'] == pytest.approx(0.06)
    assert math.isnan(adj['d'])


def test_tail_summary_and_cvar():
    t = stats.tail_summary([0.01] * 18 + [-0.10, -0.20])
    assert t['worst'] == -0.20 and t['cvar95'] == pytest.approx(-0.20)


# ---- kill-switch alternatives -----------------------------------------------------------------------------------------------
def test_alt_kill_rules():
    stops = [{'phase': 'P2', 'exit_reason': 'stop_loss', 'roi': -0.3}] * 3
    assert KS.rule_consecutive_stops(stops, 3) and not KS.rule_consecutive_stops(stops[:2], 3)
    assert KS.rule_vix1d_spike(30.0, 15.0) and not KS.rule_vix1d_spike(20.0, 15.0) and not KS.rule_vix1d_spike(NAN, 15.0)
    assert KS.rule_vix_term_inverted(25.0, 22.0) and not KS.rule_vix_term_inverted(18.0, 21.0)
    falling = [{'phase': 'P2', 'roi': 0.1}] * 20 + [{'phase': 'P2', 'roi': -0.5}] * 3
    assert KS.rule_equity_below_mean(falling, 20)


def test_replay_rule_reports_the_tradeoff():
    pnl = {'d1': 0.01, 'd2': 0.01, 'd3': -0.05, 'd4': 0.01}
    r = KS.replay_rule(pnl, {'d3': True})
    assert r['sessions_skipped'] == 1.0 and r['rule_worst'] == 0.0 and r['base_worst'] == -0.05


# ---- real-quote regrade ------------------------------------------------------------------------------------------------------------
def test_regrade_trade_on_synthetic_quotes():
    d = date(2026, 11, 13)
    s_sym, l_sym = SN.occ_symbol('AMD', d, False, 450.0), SN.occ_symbol('AMD', d, False, 440.0)       # put credit: short 450 / long 440
    q = {(s_sym, RQ._clock(d, 11)): (1.00, 1.10), (l_sym, RQ._clock(d, 11)): (0.20, 0.30),
         (s_sym, RQ._clock(d, 51)): (0.40, 0.50), (l_sym, RQ._clock(d, 51)): (0.05, 0.10)}
    r = RQ.regrade_trade(d, 'AMD', 1, 450.0, 440.0, 10, 50, q)
    assert r['status'] == 'ok'
    assert r['credit_mid'] == pytest.approx(0.80) and r['credit_taker'] == pytest.approx(0.70)
    assert r['close_mid'] == pytest.approx(0.375) and r['close_taker'] == pytest.approx(0.45)
    assert r['roi_mid'] == pytest.approx((0.80 - 0.375) / (10 - 0.80))
    assert RQ.regrade_trade(d, 'AMD', 1, 450.0, 440.0, 10, 60, q)['status'] == 'missing_quotes'


# ---- fills ----------------------------------------------------------------------------------------------------------------------------
def test_fill_logger_round_trip_and_validation(tmp_path):
    led = str(tmp_path)
    fills.add_fill(led, 'k1', 0.31, '12:04:15')
    assert fills.read_fills(led)[0]['price'] == '0.31'
    with pytest.raises(ValueError):
        fills.add_fill(led, 'k1', 0.31, '12:04:15', side='bogus')
    with pytest.raises(ValueError):
        fills.add_fill(led, 'k1', 0.31, 'noon')


# ---- quote tracker (fake clock, fake quotes) ------------------------------------------------------------------------------------------
def test_tracker_captures_follow_up_and_exit(tmp_path):
    d = date(2026, 11, 13)
    now = [1_800_000_000.0]
    posted = []

    def fetch(syms):
        return {s: {'sym': s, 'b': 1.0, 'a': 1.1, 'bs': 5, 'as': 5, 'age_s': 1.0, 'iv': 0.5, 'd': 0.3, 'g': 0.01, 'oi': 10, 'v': 3} for s in syms}

    tr = QL.QuoteTracker(str(tmp_path), d, fetch=fetch, clock=lambda: now[0], post=lambda k, t: posted.append((k, t)), autostart=False)
    data = {'K1': 450.0, 'K2': 440.0, 'direction': 1, 'S_entry': 460.0, 'credit': 0.8, 'width': 10.0, 'phase': 'P2'}
    key = f'{d.isoformat()}|ENTRY|AMD|Phase2_X|100'
    tr.on_entry(key, 'AMD', 100, data, alerted=True)
    n = tr.run_due()
    assert n >= 1
    now[0] += 31; tr.run_due(); now[0] += 31; tr.run_due()
    tr.on_exit(key); tr.run_due()
    recs = QL.load_ledger(str(tmp_path), d)
    tags = {r['tag'] for r in recs}
    assert {'alert', '+30s', '+60s', 'exit'} <= tags
    assert key not in tr.active
    assert len(posted) == 1 and 'observation only' in posted[0][1]
    m = QL.entry_metrics(recs, key, 10.0)
    assert m['mid_credit'] == pytest.approx(0.0) and not math.isnan(m['worst_age_s'])


# ---- stress harness ------------------------------------------------------------------------------------------------------------------------
def test_stress_gate_flags_a_three_index_blowup_and_the_cap_helps():
    from apex_chronos.lab import stress
    trades = stress.synthetic_crash_trades()
    r = stress.score(trades, 0.10)
    assert r['uncapped']['worst'] == pytest.approx(-0.285)             # 3 slots x 10% x -95%
    assert not r['uncapped']['pass']                                    # breaches the -10% session gate
    assert r['index_cap']['worst'] == pytest.approx(-0.095) and r['index_cap']['gate_session_loss_ok']
    assert r['index_cap']['kill_fires_after_loss']                      # -9.5% session <= -3% trips the switch
    assert r['index_cap']['trades'] == 2                                # AMD win + one index trade


def test_index_cap_keeps_non_overlapping_index_trades():
    from apex_chronos.lab import stress
    t = [{'date': 'd', 'ticker': 'SPY', 'm_entry': 10, 'm_exit': 50, 'roi': 0.1}, {'date': 'd', 'ticker': 'QQQ', 'm_entry': 60, 'm_exit': 90, 'roi': 0.1},
         {'date': 'd', 'ticker': 'AMD', 'm_entry': 20, 'm_exit': 80, 'roi': 0.1}]
    assert len(stress.apply_index_cap(t)) == 3
