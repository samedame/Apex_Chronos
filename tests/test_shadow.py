import json
from datetime import date

import pytest

from apex_chronos import config as C, replay, runner, shadow


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
    assert len(report['candidates']) == len(baseline.candidate_log)
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
    assert len(candidates) == len(baseline.candidate_log)
    assert {row['module'] for row in candidates if row['module'] in excluded} == excluded
    assert all(row['skip_p1c_p3'] is None for row in candidates if row['module'] in excluded)
    assert any(row['current'] is None and row['skip_p1c_p3'] is not None for row in candidates)
    selected = [row['skip_p1c_p3'] for row in candidates if row['skip_p1c_p3'] is not None]
    assert len(selected) == filtered['selected']
    total = sum(row['roi'] for row in selected)
    assert filtered['sum_model_roi'] == pytest.approx(total)
    assert filtered['mean_model_roi'] == pytest.approx(total / len(selected))
    assert filtered['delta_sum_model_roi'] == pytest.approx(total - report['current']['sum_model_roi'])
