import copy
import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from apex_chronos import config as C, replay, runner, shadow, shadow_costs as costs


def event(kind, minute, data):
    return SimpleNamespace(kind=kind, ticker='X', module='test', minute=minute, data=data)


def metadata():
    return {'basis': 1.5, 'tp': 0.5, 'legs': 1, 'entry_sell': 0.0,
            'unit_value': np.array([1.49, 2.1, 1.65]), 'exit_sell': np.array([149, 210, 165])}


def test_robinhood_fees_include_rounding_and_date_specific_taf():
    november = date(2026, 11, 10)
    assert costs.regulatory_fees(1, 1, 0, november) == pytest.approx(0.04)
    assert costs.regulatory_fees(1, 1, 100, november) == pytest.approx(0.05)
    assert costs.regulatory_fees(2, 2, 100, november) == pytest.approx(0.17)
    assert costs.regulatory_fees(100, 2, 100, november) == pytest.approx(8.27)
    assert costs.regulatory_fees(100, 2, 100, date(2026, 9, 30)) == pytest.approx(8.60)
    assert costs.regulatory_fees(100, 2, 100, date(2027, 1, 1)) == pytest.approx(8.60)
    assert costs.regulatory_fees(1, 1, 100, date(2026, 4, 3)) == pytest.approx(0.04)
    with pytest.raises(ValueError, match='2026-01-01'):
        costs.regulatory_fees(1, 1, 100, date(2025, 12, 31))


@pytest.mark.parametrize('sizing', costs.SIZING)
def test_partial_exits_reconcile_and_drawdown_state_carries_between_days(sizing):
    account = costs.Account('test', sizing, 0.01, date(2026, 11, 9))
    key = ('X', 'test', 0)
    meta = {key: metadata()}
    account.process([event('ENTRY', 0, {})], meta, 0)
    qty = account.open[key]['quantity']
    account.process([event('TP1', 1, {'m_entry': 0})], meta, 1)
    assert account.open[key]['remaining'] == pytest.approx(qty / 2)
    account.process([event('EXIT', 2, {'trade': {'m_entry': 0, 'roi': 0.3}})], meta, 2)
    first = account.summary()
    assert first['gross_pnl'] == pytest.approx(qty * 150 * 0.3)
    assert first['additional_slippage_dollars'] == pytest.approx(2 * qty)
    assert first['ending_equity'] == pytest.approx(1500 + qty * 150 * 0.3 - account.fees - 2 * qty)
    assert first['net_pnl'] == pytest.approx(first['trades'][0]['net_dollar_pnl'])
    assert first['max_minute_close_drawdown'] > 0
    second = costs.Account('test', sizing, 0.01, date(2026, 11, 10), prior=first)
    second.process([], {}, 0)
    carried = second.summary()
    assert carried['starting_equity'] == carried['ending_equity'] == first['ending_equity']
    assert carried['net_pnl'] == 0
    assert carried['cumulative_net_pnl'] == first['net_pnl']
    assert carried['equity_peak'] == first['equity_peak']
    assert carried['max_minute_close_drawdown'] == first['max_minute_close_drawdown']
    assert carried['day_max_minute_close_drawdown'] == 0
    if sizing == 'whole_even_contracts':
        assert qty == 2


def test_whole_lots_skip_one_contract_and_open_positions_cannot_be_committed():
    key = ('X', 'test', 0)
    meta = metadata()
    meta['basis'] = 3.0
    account = costs.Account('test', 'whole_even_contracts', 0.01, date(2026, 11, 10))
    account.process([event('ENTRY', 0, {})], {key: meta}, 0)
    result = account.summary()
    assert result['skipped_for_funding'] == 1
    assert result['one_contract_affordable'] == 1 and result['two_contracts_affordable'] == 0
    assert result['ending_equity'] == 1500 and result['trades'] == []
    funded = costs.Account('test', 'fractional_equivalent', 0.01, date(2026, 11, 10))
    funded.process([event('ENTRY', 0, {})], {key: meta}, 0)
    with pytest.raises(ValueError, match='open positions'):
        funded.summary()


def empty_report(days, session, ledger_dir, **kwargs):
    return costs.evaluate(session, days[1][session], list(C.TICKERS),
                          dict.fromkeys(costs.POLICIES, []), ledger_dir=ledger_dir, **kwargs)


def save(ledger_dir, report):
    shadow.save_report(ledger_dir, {'date': report['date'], 'execution_costs': report})


def test_report_history_is_idempotent_causal_and_has_all_scenarios(days, tmp_path):
    first = empty_report(days, '2026-09-16', tmp_path)
    save(tmp_path, first)
    second = empty_report(days, '2026-09-17', tmp_path)
    save(tmp_path, second)
    future = empty_report(days, '2026-09-18', tmp_path)
    save(tmp_path, future)
    assert empty_report(days, '2026-09-17', tmp_path) == second
    assert second['parent'] == {'date': first['date'], 'digest': costs.digest(first)}
    assert second['cohort_start'] == '2026-09-16' and second['reset_reason'] is None
    assert set(second['policies']) == set(costs.POLICIES)
    for scenarios in second['policies'].values():
        assert len(scenarios) == 8
        assert all(result['net_pnl'] == 0 and result['ending_equity'] == 1500 for result in scenarios.values())
    json.dumps(second, allow_nan=False)


def test_missing_day_or_changed_ancestor_cannot_silently_compound(days, tmp_path):
    first = empty_report(days, '2026-09-16', tmp_path)
    save(tmp_path, first)
    with pytest.raises(ValueError, match='previous full session'):
        empty_report(days, '2026-09-18', tmp_path)
    second = empty_report(days, '2026-09-17', tmp_path)
    save(tmp_path, second)
    altered = copy.deepcopy(first)
    altered['reset_reason'] = 'edited'
    save(tmp_path, altered)
    with pytest.raises(ValueError, match='changed'):
        empty_report(days, '2026-09-18', tmp_path)


def test_configuration_changes_start_an_explicit_new_cohort(days, tmp_path):
    first = empty_report(days, '2026-09-16', tmp_path)
    save(tmp_path, first)
    changed = empty_report(days, '2026-09-17', tmp_path, capital=2000)
    assert changed['cohort_start'] == '2026-09-17' and changed['parent'] is None
    assert changed['reset_reason'] == 'settings_changed'
    assert all(result['ending_equity'] == 2000
               for policy in changed['policies'].values() for result in policy.values())


def test_invalid_state_and_prior_failures_are_visible(days, tmp_path):
    first = empty_report(days, '2026-09-16', tmp_path)
    first['policies']['current']['fractional_equivalent|0.00']['ending_equity'] -= 1
    save(tmp_path, first)
    with pytest.raises(ValueError, match='cash flows'):
        empty_report(days, '2026-09-17', tmp_path)
    shadow.save_report(tmp_path, {
        'date': '2026-09-16',
        'execution_costs': {'status': 'error', 'date': '2026-09-16', 'error': 'failed'},
    })
    with pytest.raises(ValueError, match='previous cost report failed'):
        empty_report(days, '2026-09-17', tmp_path)


@pytest.mark.parametrize('capital,fraction', [(0, 0.25), (float('nan'), 0.25), (1500, 0), (1500, 1.1)])
def test_invalid_settings_do_not_create_account_results(capital, fraction):
    with pytest.raises(ValueError):
        costs.settings_for(capital, fraction, list(C.TICKERS))


def test_daily_cost_report_has_correct_policy_counts_and_preserves_eod_alerts(days, tmp_path, monkeypatch):
    session = date(2026, 9, 18)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(runner, 'final_day_inputs', lambda *_: (days[1][session.isoformat()], {}))
    posts = []
    monkeypatch.setattr(runner.alerts, 'post_discord', lambda _, text, __: posts.append(text))
    monkeypatch.setattr(runner.state, 'git_commit_push', lambda *_: None)
    assert runner.run_eod(cfg, session) == 0
    path = tmp_path / 'ledger' / 'shadow' / f'{session}.json'
    report = json.loads(path.read_text())
    execution = report['execution_costs']
    assert execution['status'] == 'ok'
    assert execution['settings']['starting_capital'] == 1500
    for policy, scenarios in execution['policies'].items():
        for scenario in scenarios.values():
            assert scenario['paper_trades'] == report[policy]['selected']
            assert scenario['account_trades'] == len(scenario['trades'])
            if scenario['sizing'] == 'whole_even_contracts':
                assert all(trade['contracts'] >= 2 and trade['contracts'] % 2 == 0 for trade in scenario['trades'])

    def fail(*args, **kwargs):
        raise ValueError('cost failure')

    monkeypatch.setattr(costs, 'evaluate', fail)
    assert runner.run_eod(cfg, session) == 0
    failed = json.loads(path.read_text())
    assert failed['execution_costs']['status'] == 'error'
    assert failed['execution_costs']['error'] == 'cost failure'
    assert failed['current'] == report['current']
    assert posts[0] == posts[1]
