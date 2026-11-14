"""2026-11-14 verdict controls (apex_chronos/verdict.py): retirement, listed-expiry gate, fair-vol grading,
kill switch, sizing default, bar archive. None of them may change a model trade or the P2 rule."""
import csv
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from apex_chronos import alerts, config as C, phase_review as pr, replay, runner, state, verdict
from apex_chronos.calendar_nyse import NY, session_open
from apex_chronos.features import compute_features

ROOT = Path(__file__).resolve().parent.parent
P2_NAME = C.MODULES['P2'].name


class Recorder:
    def __init__(self):
        self.texts = []

    def __call__(self, webhook, text, dry_run=False, retries=4):
        self.texts.append(text)
        return True


def test_p2_is_byte_for_byte_the_frozen_configuration():
    s = C.MODULES['P2']
    assert (s.structure, s.target_delta, s.sl_pct, s.tp1_pct, s.max_hold_mins) == ('credit_spread', 0.32, 0.20, 0.16, 80)
    assert C.PHASE_LIMITS == {'P1': 4, 'P2': 2, 'P3': 1} and C.N_SLOTS == 3
    assert pr.RULE['start_date'] == '2026-11-16' and pr.RULE['n_required'] == 100 and pr.RULE['seed'] == 20261113


def test_production_settings_turn_the_controls_on(monkeypatch):
    for k in ('MUTED_PHASES', 'SLOT_FRACTION'):
        monkeypatch.delenv(k, raising=False)
    cfg = runner.Settings.from_env()
    assert cfg.verdict_controls and cfg.slot_fraction == pytest.approx(0.10)
    assert cfg.retired_phases == {'P1', 'P3'} and cfg.muted_phases == frozenset()
    monkeypatch.setenv('SLOT_FRACTION', '0.05')
    assert runner.Settings.from_env().slot_fraction == pytest.approx(0.05)
    assert runner.Settings().retired_phases == frozenset()            # harness/legacy path unchanged


def test_static_expiry_schedule():
    wed, thu, fri = date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)
    assert all(verdict.static_same_day_expiry('SPY', d) for d in (wed, thu, fri))
    assert verdict.static_same_day_expiry('NVDA', wed) and not verdict.static_same_day_expiry('NVDA', thu)
    assert not verdict.static_same_day_expiry('COIN', wed) and not verdict.static_same_day_expiry('COIN', thu)
    assert verdict.static_same_day_expiry('COIN', fri) and verdict.static_same_day_expiry('MSTR', fri)


def test_listing_uses_the_committed_nasdaq_catalog_and_falls_back_without_one(tmp_path):
    cat = ROOT / 'state' / 'cash_options'
    if not (cat / '2026-11-12').exists():
        pytest.skip('committed catalog snapshots pruned')
    noon = datetime(2026, 11, 12, 12, 0, tzinfo=NY)
    assert verdict.listed_same_day('SPY', date(2026, 11, 12), noon, cat) == (True, 'Nasdaq options directory')
    assert verdict.listed_same_day('COIN', date(2026, 11, 12), noon, cat)[0] is False
    assert verdict.listed_same_day('NVDA', date(2026, 11, 12), noon, cat)[0] is False      # Thursday
    fri = datetime(2026, 11, 13, 12, 0, tzinfo=NY)
    assert verdict.listed_same_day('COIN', date(2026, 11, 13), fri, cat)[0] is True
    # no snapshot at all -> the static schedule decides, and says so
    ok, src = verdict.listed_same_day('NVDA', date(2026, 11, 12), noon, tmp_path)
    assert ok is False and src.startswith('static schedule')


def _features(td):
    return compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)


def test_regrade_at_scale_one_reproduces_every_p2_model_trade(days, fast_run):
    _, by_day = days
    p2 = fast_run[0][fast_run[0]['module'] == P2_NAME]
    assert len(p2) > 20
    for t in p2.to_dict('records'):
        td = by_day[t['date']][t['ticker']]
        g = verdict.regrade(t, td.H, td.L, td.C, _features(td).iv, 1.0, len(td.C))
        assert g['roi'] == t['roi'] and g['m_exit'] == t['m_exit'] and g['exit_reason'] == t['exit_reason']
        assert g['cost_basis'] == t['cost_basis']


def test_fair_vol_grading_writes_a_side_file_and_leaves_the_ledger_alone(tmp_path, days, fast_run):
    _, by_day = days
    p2 = fast_run[0][fast_run[0]['module'] == P2_NAME]
    d = p2['date'].iloc[0]
    trades = fast_run[0][fast_run[0]['date'] == d].to_dict('records')
    state.replace_day_in_ledger(str(tmp_path), date.fromisoformat(d), trades)
    before = (tmp_path / 'trades.csv').read_bytes()
    dts = sorted(by_day)
    # nothing archived yet -> no fair grade, never a guess
    rows = verdict.grade_p2_day(date.fromisoformat(d), by_day[d], trades, tmp_path / 'none', session_open(date.fromisoformat(d)),
                                state_dir=str(tmp_path))
    assert rows and all(r['roi_fair'] is None and r['rv_sessions'] == 0 for r in rows)
    # archive five earlier sessions (later fixture days stand in for history; only the bars are read)
    for x in [x for x in dts if x != d][:6]:
        verdict.save_bars(str(tmp_path), date(2000, 1, 1) + timedelta(days=dts.index(x)), by_day[x])
    rows = verdict.grade_p2_day(date.fromisoformat(d), by_day[d], trades, tmp_path / 'none', session_open(date.fromisoformat(d)),
                                state_dir=str(tmp_path))
    assert all(r['roi_fair'] is not None and r['rv_sessions'] >= verdict.FAIR_MIN_SESSIONS for r in rows)
    assert all(0 < r['iv_fair_entry'] < r['iv_model_entry'] * 3 for r in rows)
    verdict.write_grading(str(tmp_path), date.fromisoformat(d), rows)
    verdict.write_grading(str(tmp_path), date.fromisoformat(d), rows)          # idempotent per day
    with open(tmp_path / 'p2_grading.csv', newline='') as fh:
        assert len(list(csv.DictReader(fh))) == len(rows)
    assert (tmp_path / 'trades.csv').read_bytes() == before
    line = verdict.grading_lines(str(tmp_path), start='2000-01-01')[0]
    assert 'fair-vol' in line and 'listed same-day expiry only' in line


def test_rest_of_day_forecast_needs_five_sessions():
    flat = {'tickers': {'SPY': {'C': list(100 * np.exp(np.cumsum(np.full(390, 0.0005))))}}}
    assert np.isnan(verdict.forecast_rest_of_day_vol([flat] * 4, 'SPY', 100)[0])
    v, n = verdict.forecast_rest_of_day_vol([flat] * 6, 'SPY', 100)
    assert n == 6 and v == pytest.approx(0.0005 * np.sqrt(390 * 252), rel=1e-6)


def test_kill_switch_math():
    day = lambda d, roi, ph='P2': {'date': d, 'roi': str(roi), 'phase': ph}
    calm = [day('2026-11-16', 0.05), day('2026-11-17', -0.10), day('2026-11-17', 0.02, 'P1')]
    s = verdict.kill_switch_status(calm, 0.10)
    assert not s['tripped'] and s['sessions'] == 2
    two_stops = calm + [day('2026-11-18', -0.20), day('2026-11-18', -0.20)]
    s = verdict.kill_switch_status(two_stops, 0.10)
    assert s['tripped'] and any('session' in r for r in s['reasons'])
    old = [day('2026-11-13', -5.0)]                                              # before the forward start: ignored
    assert not verdict.kill_switch_status(old + calm, 0.10)['tripped']
    bleed = [day(f'2026-11-{16 + i:02d}', -0.25) for i in range(8)]              # -2.5% a day, never -3%, deep drawdown
    s = verdict.kill_switch_status(bleed, 0.10)
    assert s['tripped'] and any('drawdown' in r for r in s['reasons']) and not any('session' in r for r in s['reasons'])


@pytest.fixture(scope='module')
def all_events(days, fast_run):
    _, by_day = days
    return {d: replay.run_session(d, by_day[d], list(C.TICKERS), mode='fast')[1] for d in sorted(fast_run[0]['date'].unique())}


def _session(tmp_path, d, monkeypatch, controls=True):
    rec = Recorder()
    monkeypatch.setattr(alerts, 'post_discord', rec)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'), webhook='https://x',
                          dry_run=False, verdict_controls=controls)
    return runner.LiveSession(cfg, date.fromisoformat(d)), rec


def _run(sess, ev):
    for e in ev:
        minute = e.minute if e.kind != 'EXIT' else e.data['trade']['m_exit']
        sess.dispatch([e], session_open(sess.date) + timedelta(minutes=minute + 1, seconds=7))


def test_live_alerts_only_p2_on_listed_contracts_and_still_records_everything(tmp_path, monkeypatch, all_events):
    names_p13 = {s.name for s in C.MODULES.values() if s.phase in ('P1', 'P3')}
    checked = 0
    for d, ev in all_events.items():
        entries = [e for e in ev if e.kind == 'ENTRY']
        if not entries:
            continue
        sess, rec = _session(tmp_path / d, d, monkeypatch)
        assert {'P1', 'P3'} <= sess.muted                                       # retired even with MUTED_PHASES unset
        _run(sess, ev)
        posted = [t for t in rec.texts if 'APEX-CHRONOS ENTRY' in t]
        want = [e for e in entries if e.module == P2_NAME and verdict.static_same_day_expiry(e.ticker, sess.date)]
        assert len(posted) == len(want)
        assert all('same-day expiry listed today' in t for t in posted)
        assert not any(m in t for t in rec.texts for m in names_p13)
        sent = state.load_sent(sess.cfg.state_dir, sess.date)
        assert sum(1 for k in sent if '|ENTRY|' in k) == len(entries)           # reconcile still sees every entry
        assert all(p[1] == P2_NAME for p in sess.posted_entries)
        checked += 1
    assert checked > 5


def test_a_tripped_kill_switch_mutes_every_phase(tmp_path, monkeypatch, all_events):
    d = next(d for d, ev in all_events.items() if any(e.kind == 'ENTRY' and e.module == P2_NAME for e in ev))
    verdict.save_kill_switch(str(tmp_path / 'state'), {'tripped': True, 'reasons': ['test']})
    sess, rec = _session(tmp_path, d, monkeypatch)
    assert sess.muted == frozenset(C.PHASE_LIMITS)
    sess.prepare = lambda: None
    _run(sess, all_events[d])
    assert not any('APEX-CHRONOS ENTRY' in t for t in rec.texts)


def test_bar_archive_round_trips(tmp_path, days):
    dts, by_day = days
    d = dts[-1]
    p = verdict.save_bars(str(tmp_path), date.fromisoformat(d), by_day[d])
    back = verdict.load_bars(str(tmp_path), date.fromisoformat(d))
    assert set(back['tickers']) == set(by_day[d])
    td = by_day[d]['SPY']
    assert np.allclose(back['tickers']['SPY']['C'], td.C, atol=1e-4) and len(back['VIX']) == len(td.vix)
    assert os.path.getsize(p) < 400_000


def test_eod_writes_grading_kill_switch_and_bars_without_touching_the_trade_ledger(tmp_path, monkeypatch, days, fast_run):
    dts, by_day = days
    d = next(x for x in dts if (fast_run[0][fast_run[0]['date'] == x]['module'] == P2_NAME).any())
    rec = Recorder()
    monkeypatch.setattr(alerts, 'post_discord', rec)
    monkeypatch.setattr(runner, 'final_day_inputs', lambda http, cfg, dd: (by_day[d], {}))
    monkeypatch.setattr(runner, 'is_full_session', lambda dd: True)
    monkeypatch.setattr(verdict, 'KILL_START', '2000-01-01')
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'), webhook='https://x',
                          dry_run=False, verdict_controls=True, slot_fraction=0.10)
    assert runner.run_eod(cfg, date.fromisoformat(d)) == 0
    ledger = state.read_ledger(cfg.ledger_dir)
    expect = fast_run[0][fast_run[0]['date'] == d]
    assert len(ledger) == len(expect)
    assert (tmp_path / 'ledger' / 'p2_grading.csv').exists()
    assert (tmp_path / 'state' / 'bars' / f'{d}.json.gz').exists()
    assert (tmp_path / 'state' / 'kill-switch.json').exists()
    summary = next(t for t in rec.texts if 'end of day' in t)
    assert 'Kill switch' in summary and 'P2 grading since 2000-01-01' in summary
