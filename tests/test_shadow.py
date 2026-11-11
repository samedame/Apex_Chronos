import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from apex_chronos import config as C, replay, runner, shadow
from apex_chronos.engine import SessionEngine


def test_shadow_logs_all_candidates_and_changes_a_contested_slot(days, tmp_path):
    day = '2026-09-18'
    tickers = list(C.TICKERS)
    session = days[1][day]
    baseline, _ = replay.run_session(day, session, tickers)
    module = C.MODULES['P2'].name
    ledger = [
        {'date': '2026-09-17', 'ticker': 'COIN', 'module': module, 'roi': -1.0},
        {'date': '2026-09-17', 'ticker': 'MSTR', 'module': module, 'roi': 1.0},
        {'date': day, 'ticker': 'COIN', 'module': module, 'roi': 100.0},
        {'date': '2026-09-21', 'ticker': 'MSTR', 'module': module, 'roi': -100.0},
    ]
    report = shadow.compare(day, session, tickers, baseline, ledger)
    assert report['training_trades'] == 2
    assert len(report['candidates']) >= len(baseline.candidate_log)
    assert any(row['current'] is row['shadow'] is None for row in report['candidates'])
    coin, mstr = sorted(
        (row for row in report['candidates'] if row['m_entry'] == 82),
        key=lambda row: row['ticker'],
    )
    assert coin['ticker'] == 'COIN' and mstr['ticker'] == 'MSTR'
    assert coin['expected_model_roi'] == pytest.approx(-1 / 21)
    assert mstr['expected_model_roi'] == pytest.approx(1 / 21)
    assert coin['current'] is not None and coin['shadow'] is None
    assert mstr['shadow'] is not None and mstr['current'] is None
    assert report['current_only'] == report['shadow_only'] == 1
    assert report['delta_sum_model_roi'] == pytest.approx(mstr['shadow']['roi'] - coin['current']['roi'])
    shadow.save_report(tmp_path, report)
    shadow.save_report(tmp_path, report)
    assert json.loads((tmp_path / 'shadow' / f'{day}.json').read_text()) == report


def test_eod_shadow_failure_leaves_alert_and_ledger_intact(days, tmp_path, monkeypatch):
    day = date(2026, 9, 18)
    session = days[1][day.isoformat()]
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(runner, 'final_day_inputs', lambda *_: (session, {}))
    posts = []
    monkeypatch.setattr(runner.alerts, 'post_discord', lambda _, message, __: posts.append(message))
    monkeypatch.setattr(runner.state, 'git_commit_push', lambda *_: None)
    assert runner.run_eod(cfg, day) == 0
    ledger = (tmp_path / 'ledger' / 'trades.csv').read_bytes()
    report = json.loads((tmp_path / 'ledger' / 'shadow' / f'{day}.json').read_text())
    assert report['training_trades'] == 0
    assert report['current'] == report['shadow']
    assert report['current_only'] == report['shadow_only'] == 0
    assert report['combined_p1a_skip_p1c_p3']['entry_filters'] == {
        'p1a_max_rvol': 1.0, 'p1a_max_retrace': 1.0,
    }
    assert report['combined_p1a_skip_p1c_p3']['excluded_modules'] == ['P1C', 'P3']

    def fail(*_):
        raise ValueError('shadow failed')

    monkeypatch.setattr(shadow, 'compare', fail)
    assert runner.run_eod(cfg, day) == 0
    assert posts[0] == posts[1]
    assert (tmp_path / 'ledger' / 'trades.csv').read_bytes() == ledger


def test_skip_policy_replays_freed_capacity_and_retains_excluded_candidates(days):
    day = '2026-09-18'
    tickers = list(C.TICKERS)
    session = days[1][day]
    baseline, _ = replay.run_session(day, session, tickers)
    original_trades = list(baseline.trades)
    report = shadow.compare(day, session, tickers, baseline, [])
    filtered = report['skip_p1c_p3']
    assert baseline.trades == original_trades
    assert report['current']['selected'] == 6
    assert filtered['selected'] == 4
    assert filtered['unclosed'] == 0
    assert filtered['current_only'] == 3
    assert filtered['shadow_only'] == 1
    excluded = {C.MODULES[key].name for key in ('P1C', 'P3')}
    candidates = report['candidates']
    assert len(candidates) >= len(baseline.candidate_log)
    assert {row['module'] for row in candidates if row['module'] in excluded} == excluded
    assert all(row['skip_p1c_p3'] is None for row in candidates if row['module'] in excluded)
    assert any(row['current'] is None and row['skip_p1c_p3'] is not None for row in candidates)
    selected = [row['skip_p1c_p3'] for row in candidates if row['skip_p1c_p3'] is not None]
    assert len(selected) == filtered['selected']
    total = sum(row['roi'] for row in selected)
    assert filtered['sum_model_roi'] == pytest.approx(total)
    assert filtered['mean_model_roi'] == pytest.approx(total / len(selected))
    assert filtered['delta_sum_model_roi'] == pytest.approx(total - report['current']['sum_model_roi'])


def test_p1a_filters_are_scoped_and_keep_the_original_minute_limit():
    f = SimpleNamespace(O=np.full(80, 101.0), C=np.full(80, 100.2), prev_c=100.0,
                        rvol_cum=np.ones(80), iv=np.full(80, 0.25))
    current = SessionEngine('2026-11-10', ['X'], n_bars=80)
    alternative = SessionEngine('2026-11-10', ['X'], n_bars=80,
                                p1a_max_rvol=1.0, p1a_max_retrace=1.0)
    assert current._on_signal(0, 'X', 'P1A', 10, 1, f) is None
    assert alternative._on_signal(0, 'X', 'P1A', 10, 1, f) is not None
    f.C[10], f.rvol_cum[10] = 100.5, 1.1
    assert current._on_signal(0, 'X', 'P1A', 10, 1, f) is not None
    assert alternative._on_signal(0, 'X', 'P1A', 10, 1, f) is None
    f.rvol_cum[10] = 1.0
    assert alternative._on_signal(0, 'X', 'P1A', 10, 1, f) is not None
    f.C[10] = 100.0
    assert alternative._on_signal(0, 'X', 'P1A', 10, 1, f) is None
    assert alternative._on_signal(0, 'X', 'P1A', 35, 1, f) is not None
    assert alternative._on_signal(0, 'X', 'P1A', 36, 1, f) is None
    assert (C.P1A_MAX_RVOL, C.P1A_MAX_RETRACE) == (1.25, 0.75)
    assert (current.p1a_max_rvol, current.p1a_max_retrace) == (1.25, 0.75)


def test_p1a_policy_logs_new_candidates_and_preserves_current_events(days):
    day = '2026-09-22'
    tickers = list(C.TICKERS)
    session = days[1][day]
    baseline, events = replay.run_session(day, session, tickers)
    alternative, _ = replay.run_session(day, session, tickers, engine_kwargs={
        'p1a_max_rvol': 1.0, 'p1a_max_retrace': 1.0,
    })
    report = shadow.compare(day, session, tickers, baseline, [])
    policy = report['p1a_rvol1_retrace1']
    key = lambda row: (row['ticker'], row['module'], row['m_entry'])
    original = {key(row) for row in baseline.candidate_log}
    revised = {key(row) for row in alternative.candidate_log}
    logged = {key(row) for row in report['candidates']}
    assert revised - original
    assert logged == original | revised
    selected = {key(row) for row in alternative.trades}
    assert {key(row) for row in report['candidates'] if row['p1a_rvol1_retrace1']} == selected
    assert policy['selected'] == len(selected) == 6
    assert policy['current_only'] == policy['shadow_only'] == 2
    total = sum(row['roi'] for row in alternative.trades)
    assert policy['sum_model_roi'] == pytest.approx(total)
    assert policy['mean_model_roi'] == pytest.approx(total / len(selected))
    assert policy['delta_sum_model_roi'] == pytest.approx(total - report['current']['sum_model_roi'])
    after, after_events = replay.run_session(day, session, tickers)
    assert baseline.trades == after.trades
    assert events == after_events


def test_combined_policy_replays_interactions_and_logs_every_selection(days):
    day = '2026-10-02'
    tickers = list(C.TICKERS)
    baseline, _ = replay.run_session(day, days[1][day], tickers)
    original_trades = list(baseline.trades)
    report = shadow.compare(day, days[1][day], tickers, baseline, [])
    name = 'combined_p1a_skip_p1c_p3'
    combined = report[name]
    assert baseline.trades == original_trades
    assert combined['selected'] == 6
    assert combined['unclosed'] == 0
    assert combined['current_only'] == 4
    assert combined['shadow_only'] == 3
    selected = [row for row in report['candidates'] if row[name] is not None]
    assert len(selected) == combined['selected']
    assert all(not row['module'].startswith(('Phase1C_', 'Phase3_')) for row in selected)
    assert all(row['rvol_entry'] <= 1.0 for row in selected if row['module'].startswith('Phase1A_'))
    key = lambda row: (row['ticker'], row['module'], row['m_entry'])
    separate_selections = {
        key(row) for row in report['candidates']
        if row['p1a_rvol1_retrace1'] is not None or row['skip_p1c_p3'] is not None
    }
    assert {key(row) for row in selected} - separate_selections == {
        ('NVDA', C.MODULES['P2'].name, 117),
        ('SPY', C.MODULES['P1B'].name, 59),
    }
    total = sum(row[name]['roi'] for row in selected)
    assert combined['sum_model_roi'] == pytest.approx(total)
    assert combined['mean_model_roi'] == pytest.approx(total / len(selected))
    assert combined['delta_sum_model_roi'] == pytest.approx(total - report['current']['sum_model_roi'])
