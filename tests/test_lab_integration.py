"""Lab: shadow evaluator end to end on a synthetic session, a synthetic chain snapshot and a synthetic quote ledger."""
import gzip
import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from apex_chronos.calendar_nyse import NY, session_open
from apex_chronos.lab import options_math as om, quote_ledger as QL, shadow_eval as SE, snapshots as SN

D = date(2026, 11, 13)
M = 120


def _session(seed, n=390, s0=100.0):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.0004, n)
    c = s0 * np.exp(np.cumsum(r))
    o = np.concatenate([[s0], c[:-1]])
    h = np.maximum(o, c) * 1.0002
    l = np.minimum(o, c) * 0.9998
    return {'O': o, 'H': h, 'L': l, 'C': c, 'V': rng.integers(1000, 5000, n).astype(float)}


def _prior(n_days=6):
    out = []
    for i in range(n_days):
        s = _session(i)
        out.append({'date': f'2026-11-0{i + 1}', 'tickers': {'AMD': {k: v.tolist() for k, v in s.items()}}, 'VIX': [18.0] * 390})
    return out


def _chain(spot, minutes_left, iv=0.55):
    T = om.T_years(minutes_left)
    rows = []
    for k in np.arange(round(spot * 0.85), round(spot * 1.15), 1.0):
        for is_call in (True, False):
            px = om.bs_price(spot, k, T, iv, is_call)
            rows.append({'k': float(k), 't': 'c' if is_call else 'p', 'b': round(px - 0.05, 2), 'a': round(px + 0.05, 2), 'bs': 10, 'as': 10,
                         'v': 100, 'oi': 1000, 'iv': iv, 'd': 0.3, 'g': float(om.bs_gamma(spot, k, T, iv)), 'th': -0.1, 'vg': 0.1,
                         'last': round(px, 2), 's': f'AMD{k}'})
    return rows


def _write_snapshot(state_dir, label, taken_at, spot):
    p = SN.path_for(state_dir, D, label)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {'date': D.isoformat(), 'label': label, 'taken_at': taken_at.isoformat(), 'size_unit': 'unverified', 'errors': {},
               'roots': {'AMD': {'spot': spot, 'quote': {}, 'has_same_day': True,
                                 'expiries': {D.isoformat(): _chain(spot, om.minutes_left(M)), '2026-11-20': _chain(spot, 7000, 0.5)}}}}
    with gzip.open(p, 'wt') as fh:
        json.dump(payload, fh)


@pytest.fixture
def world(tmp_path):
    state = str(tmp_path / 'state')
    today = _session(99)
    today['vix'] = np.full(390, 18.0)
    today['prev_c'] = 100.0
    spot = float(today['C'][M])
    t_open = session_open(D)
    _write_snapshot(state, '0945', t_open.replace(hour=9, minute=40), spot)                 # before entry close
    _write_snapshot(state, '1130', t_open.replace(hour=11, minute=30, second=0) , spot)    # AFTER the entry close at 11:31? no: bar 120 closes 11:31
    return state, today, spot


def _trade(direction=1):
    return {'ticker': 'AMD', 'm_entry': M, 'm_exit': 200, 'direction': direction, 'roi': 0.05, 'exit_reason': 'time', 'module': 'Phase2_X', 'phase': 'P2'}


def test_evaluator_is_causal_and_fills_chain_fields(world):
    state, today, spot = world
    row = SE.evaluate_trade(D, _trade(), today, _prior(), state_dir=state, iv_model=0.6, roi_fair=0.04, day_trades=[_trade()])
    assert row['snapshot_label'] == '1130'        # 11:30 snapshot is taken at 11:30:00 < bar-120 close (11:31:00), so it is usable
    assert math.isfinite(row['atm_iv']) and abs(row['atm_iv'] - 0.55) < 0.05
    assert math.isfinite(row['vrp_ratio'])
    assert row['f1_vrp_ge_1_0'] in (0.0, 1.0)
    assert math.isfinite(row['gex']) and row['f2_gex_pos'] in (0.0, 1.0)
    assert math.isfinite(row['implied_move']) and row['implied_move'] > 0
    assert row['f5_index_cap_pass'] == 1.0 and row['f3_fomc_pass'] == 1.0
    assert 'roi_fair' in row and row['roi_fair'] == 0.04


def test_snapshot_after_entry_is_never_used(tmp_path):
    state = str(tmp_path / 'state')
    spot = 100.0
    _write_snapshot(state, '1230', session_open(D).replace(hour=12, minute=30), spot)           # entry bar 120 closes 11:31
    assert SN.latest_label_at_or_before(state, D, M) is None
    today = _session(5); today['vix'] = np.full(390, 18.0); today['prev_c'] = 100.0
    row = SE.evaluate_trade(D, _trade(), today, _prior(), state_dir=state, iv_model=0.6)
    assert row['snapshot_label'] == '' and math.isnan(row['atm_iv']) and math.isnan(row['f1_vrp_ge_1_0'])


def test_outcome_never_changes_features(world):
    state, today, _ = world
    a = SE.evaluate_trade(D, dict(_trade(), roi=0.5, m_exit=300), today, _prior(), state_dir=state, iv_model=0.6, roi_fair=0.5)
    b = SE.evaluate_trade(D, dict(_trade(), roi=-0.9, m_exit=130), today, _prior(), state_dir=state, iv_model=0.6, roi_fair=-0.9)
    skip = {'roi_model', 'roi_fair', 'm_exit', 'exit_reason', 'open_after_1530'}
    for k in a:
        if k in skip:
            continue
        x, y = a[k], b[k]
        assert (isinstance(x, float) and math.isnan(x) and math.isnan(y)) or x == y, k


def test_future_bars_do_not_change_features(world):
    state, today, _ = world
    a = SE.evaluate_trade(D, _trade(), today, _prior(), state_dir=state, iv_model=0.6)
    mut = {k: (v.copy() if hasattr(v, 'copy') else v) for k, v in today.items()}
    for k in ('O', 'H', 'L', 'C'):
        mut[k][M + 1:] *= 1.5
    b = SE.evaluate_trade(D, _trade(), mut, _prior(), state_dir=state, iv_model=0.6)
    for k in a:
        x, y = a[k], b[k]
        assert (isinstance(x, float) and math.isnan(x) and math.isnan(y)) or x == y, k


def test_index_slot_conflict(world):
    state, today, _ = world
    t = dict(_trade(), ticker='SPY')
    other = dict(_trade(), ticker='QQQ', m_entry=100, m_exit=300)
    for tk in ('SPY',):
        today2 = dict(today)
        row = SE.evaluate_trade(D, t, today2, _prior(), state_dir=state, iv_model=0.6, day_trades=[t, other])
    # SPY has no prior archived series in the synthetic archive; the conflict flag only needs the trade list
    assert row['index_slot_conflict'] == 1.0 and row['f5_index_cap_pass'] == 0.0


def test_quote_ledger_metrics_feed_gates(world, tmp_path):
    state, today, spot = world
    key = f'{D.isoformat()}|ENTRY|AMD|Phase2_X|{M}'
    recs = [{'key': key, 'tag': 'alert', 'role': 'short', 'b': 1.00, 'a': 1.10, 'age_s': 1.0, 'iv': 0.5, 'd': 0.3},
            {'key': key, 'tag': 'alert', 'role': 'long', 'b': 0.20, 'a': 0.25, 'age_s': 2.0, 'iv': 0.5, 'd': 0.1}]
    em = QL.entry_metrics(recs, key, 5.0)
    assert abs(em['mid_credit'] - (1.05 - 0.225)) < 1e-9
    assert abs(em['taker_credit'] - (1.00 - 0.25)) < 1e-9
    row = SE.evaluate_trade(D, _trade(), today, _prior(), state_dir=state, iv_model=0.6, ledger_recs=recs, ev_key=key)
    assert row['real_mid_credit'] == pytest.approx(em['mid_credit'])
    assert row['f4_gate_025'] in (0.0, 1.0)


def test_write_rows_replaces_the_day_and_keeps_others(tmp_path):
    led = str(tmp_path / 'ledger')
    r1 = {'date': '2026-11-12', 'ticker': 'AMD', 'm_entry': 10, 'x': 1.0}
    r2 = {'date': '2026-11-13', 'ticker': 'AMD', 'm_entry': 10, 'x': 2.0}
    SE.write_rows(led, date(2026, 11, 12), [r1])
    SE.write_rows(led, date(2026, 11, 13), [r2])
    SE.write_rows(led, date(2026, 11, 13), [dict(r2, x=3.0, y=9.0)])           # same-day rerun replaces, new column appended
    rows = SE.read_rows(str(SE.shadow_path(led)))
    assert [(r['date'], r['x']) for r in rows] == [('2026-11-12', '1.0'), ('2026-11-13', '3.0')]
    assert 'y' in rows[0]


# ---- the lab hook never changes an alert, and a failing hook never breaks the session ----------------------------------------------------------
def test_dispatch_texts_identical_with_and_without_lab(tmp_path, monkeypatch, days, fast_run):
    from datetime import timedelta
    from apex_chronos import alerts, runner
    from apex_chronos.lab import runtime as LR
    from tests.test_alerts_and_runner import Recorder, busiest_date, events_for

    d = busiest_date(fast_run)
    ev = events_for(days, d)

    def play(with_lab, sub):
        rec = Recorder()
        monkeypatch.setattr(alerts, 'post_discord', rec)
        cfg = runner.Settings(state_dir=str(tmp_path / sub / 'state'), ledger_dir=str(tmp_path / sub / 'ledger'), webhook='https://x')
        sess = runner.LiveSession(cfg, date.fromisoformat(d))
        if with_lab:
            sess.lab = LR.LabRuntime(cfg.state_dir, sess.date, cfg.tickers, http=None, post=None)
            sess.lab.tracker.autostart = False
            sess.lab.tracker.fetch = lambda syms: (_ for _ in ()).throw(RuntimeError('network down'))
        for e in ev:
            minute = e.minute if e.kind != 'EXIT' else e.data['trade']['m_exit']
            sess.dispatch([e], session_open(sess.date) + timedelta(minutes=minute + 1, seconds=7))
        if with_lab:
            sess.lab.tracker.run_due()
            sess.lab.close()
        return rec.texts

    assert play(True, 'a') == play(False, 'b')
