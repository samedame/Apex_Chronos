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
    account.process([event('EXIT', 2, {'trade': {'m_entry': 0, 'm_exit': 2,
                                              'exit_reason': 'profit_trail', 'roi': 0.3}})], meta, 2)
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
    assert carried['cumulative_account_trades'] == carried['cumulative_paper_trades'] == 1
    assert carried['cumulative_skipped_for_funding'] == 0
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
        assert len(scenarios) == 16
        assert all(result['net_pnl'] == 0 and result['ending_equity'] == 1500 for result in scenarios.values())
    json.dumps(second, allow_nan=False)
    for policy in costs.POLICIES:
        for slip in costs.SLIPPAGE:
            comparison = second['contract_sizing_comparison'][policy][f'{slip:.2f}']
            assert set(comparison['accounts']) == {'whole_even_contracts', *costs.ONE_CONTRACT_SIZING}
            assert all(value == 0 for delta in comparison['differences_vs_two_contracts'].values()
                       for value in delta.values())


def test_v1_accounts_start_a_common_explicit_v2_cohort(days, tmp_path):
    legacy = empty_report(days, '2026-09-16', tmp_path)
    legacy['schema_version'] = 1
    legacy['settings']['model_version'] = 'robinhood_shadow_accounts_v1'
    legacy['settings']['sizing_modes'] = ['fractional_equivalent', 'whole_even_contracts']
    for policy in legacy['policies']:
        legacy['policies'][policy] = {key: value for key, value in legacy['policies'][policy].items()
                                     if value['sizing'] in legacy['settings']['sizing_modes']}
    save(tmp_path, legacy)
    original_bytes = (tmp_path / 'shadow/2026-09-16.json').read_bytes()
    current = empty_report(days, '2026-09-17', tmp_path)
    assert current['schema_version'] == 2
    assert current['reset_reason'] == 'settings_changed'
    assert current['cohort_start'] == '2026-09-17' and current['parent'] is None
    assert all(a['starting_equity'] == a['ending_equity'] == 1500
               for cases in current['policies'].values() for a in cases.values())
    assert (tmp_path / 'shadow/2026-09-16.json').read_bytes() == original_bytes


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
            if scenario['sizing'] in costs.ONE_CONTRACT_SIZING:
                for trade in scenario['trades']:
                    assert trade['contracts'] >= 1 and int(trade['contracts']) == trade['contracts']
                    assert trade['tp1_contracts'] + trade['runner_contracts'] == trade['contracts']
                    assert trade['tp1_contracts'] == int(trade['tp1_contracts'])
                    assert trade['runner_contracts'] == int(trade['runner_contracts'])
        for slip, comparison in execution['contract_sizing_comparison'][policy].items():
            for sizing, account in comparison['accounts'].items():
                expected = scenarios[f'{sizing}|{slip}']
                assert account == {field: expected[field] for field in account}
            baseline = scenarios[f'whole_even_contracts|{slip}']
            for sizing, delta in comparison['differences_vs_two_contracts'].items():
                actual = scenarios[f'{sizing}|{slip}']
                assert all(value == actual[field] - baseline[field] for field, value in delta.items())

    def fail(*args, **kwargs):
        raise ValueError('cost failure')

    monkeypatch.setattr(costs, 'evaluate', fail)
    assert runner.run_eod(cfg, session) == 0
    failed = json.loads(path.read_text())
    assert failed['execution_costs']['status'] == 'error'
    assert failed['execution_costs']['error'] == 'cost failure'
    assert failed['current'] == report['current']
    assert posts[0] == posts[1]


def sized_metadata(basis):
    return {'basis': basis, 'tp': 0.5, 'legs': 1, 'entry_sell': 0.0,
            'unit_value': np.array([basis, basis * 1.1, basis * 1.1]),
            'exit_sell': np.array([basis, basis * 1.1, basis * 1.1]) * 100}


def closed_event(minute=2):
    return event('EXIT', minute, {'trade': {'m_entry': 0, 'm_exit': minute,
                                          'hold_mins': minute, 'roi': 0.3,
                                          'exit_reason': 'profit_trail'}})


@pytest.mark.parametrize('sizing,basis,gross,tp_qty,runner_qty', [
    ('whole_contracts_keep_runner', 3, 30, 0, 1),
    ('whole_contracts_sell_odd_at_tp1', 3, 150, 1, 0),
    ('whole_contracts_keep_runner', 1, 70, 1, 2),
    ('whole_contracts_sell_odd_at_tp1', 1, 110, 2, 1),
])
def test_odd_quantities_use_integer_tp1_and_unblended_runner_returns(sizing, basis, gross, tp_qty, runner_qty):
    a = costs.Account('test', sizing, 0.01, date(2026, 11, 10))
    key = ('X', 'test', 0)
    meta = {key: sized_metadata(basis)}
    a.process([event('ENTRY', 0, {})], meta, 0)
    qty = a.open[key]['quantity']
    a.process([event('TP1', 1, {'m_entry': 0})], meta, 1)
    assert a.open[key]['remaining'] == runner_qty
    a.process([closed_event()], meta, 2)
    result = a.summary()
    trade = result['trades'][0]
    assert a.gross_pnl == pytest.approx(gross)
    assert a.slippage_paid == pytest.approx(2 * qty)
    assert result['net_pnl'] == pytest.approx(gross - a.fees - 2 * qty)
    assert trade['tp1_contracts'] == tp_qty and trade['runner_contracts'] == runner_qty
    assert trade['roi'] == pytest.approx(gross / (qty * basis * 100))
    assert trade['paper_model_roi'] == 0.3
    assert trade['m_exit'] == (1 if not runner_qty else 2)
    assert trade['paper_m_exit'] == 2
    assert result['single_contract_entries'] == int(qty == 1)
    # Model prices may be NumPy scalars; reports must remain portable JSON.
    json.dumps(result, allow_nan=False)


def test_single_contract_same_bar_tp1_exit_is_not_charged_twice():
    a = costs.Account('test', 'whole_contracts_sell_odd_at_tp1', 0.01, date(2026, 11, 10))
    key = ('X', 'test', 0)
    meta = {key: sized_metadata(3)}
    a.process([event('ENTRY', 0, {})], meta, 0)
    a.process([event('TP1', 1, {'m_entry': 0}), closed_event(1)], meta, 1)
    result = a.summary()
    assert result['gross_pnl'] == pytest.approx(150)
    assert result['robinhood_fees_estimated'] == pytest.approx(0.09)
    assert result['additional_slippage_dollars'] == 2
    assert result['net_pnl'] == pytest.approx(147.91)
    assert result['trades'][0]['exit_reason'] == 'all_at_tp1'


@pytest.mark.parametrize('sizing', costs.ONE_CONTRACT_SIZING)
def test_one_contract_funding_includes_all_opening_costs_and_unsplit_exit(sizing):
    key = ('X', 'test', 0)
    a = costs.Account('test', sizing, 0.01, date(2026, 11, 10))
    a.process([event('ENTRY', 0, {})], {key: sized_metadata(3.75)}, 0)
    assert not a.open and a.summary()['skipped_for_funding'] == 1
    meta = {key: sized_metadata(3)}
    a.process([event('ENTRY', 0, {})], meta, 0)
    assert a.open[key]['quantity'] == 1
    a.process([closed_event()], meta, 2)
    result = a.summary()
    assert result['gross_pnl'] == pytest.approx(90)
    assert result['net_pnl'] == pytest.approx(87.91)
    assert result['trades'][0]['tp1_contracts'] == 0


def test_keeping_single_runner_marks_actual_remaining_quantity_and_close_costs():
    a = costs.Account('test', 'whole_contracts_keep_runner', 0.01, date(2026, 11, 10))
    key = ('X', 'test', 0)
    meta = {key: sized_metadata(3)}
    a.process([event('ENTRY', 0, {})], meta, 0)
    a.process([event('TP1', 1, {'m_entry': 0})], meta, 1)
    assert a.last_equity == pytest.approx(1500 - 301.04 + 330 - 1.05)
