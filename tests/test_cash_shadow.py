import json
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from apex_chronos import cash_catalog as cc, cash_shadow as cs, config as C, runner, shadow


def contract(ticker='SPY', expiry='2026-11-11', strike=600.0, right='C'):
    return {'symbol': cc.option_symbol(ticker, date.fromisoformat(expiry), right, strike),
            'ticker': ticker, 'expiry': expiry, 'strike': strike, 'right': right,
            'multiplier': 100, 'standard': True}


def catalog(contracts=None):
    return {'schema_version': 1, 'observed_at': '2026-11-11T13:00:00+00:00',
            'source': cc.SOURCE_URL, 'contracts': contracts or [contract()]}


def row(ticker='SPY', minute=5):
    return {'date': '2026-11-11', 'ticker': ticker, 'module': C.MODULES['P1A'].name,
            'm_entry': minute, 'direction': 1, 'score': 1.0, 'rvol_entry': 0.7}


@pytest.mark.parametrize('sold,settles', [('2026-11-10', '2026-11-12'),
                                         ('2026-11-11', '2026-11-12'),
                                         ('2026-10-09', '2026-10-13'),
                                         ('2026-11-25', '2026-11-27'),
                                         ('2026-11-13', '2026-11-16')])
def test_t_plus_one_skips_bank_holidays_but_not_early_closes(sold, settles):
    assert str(cs.next_settlement_day(date.fromisoformat(sold))) == settles


def test_fee_rounding_and_october_december_taf_pause():
    assert cs.fees(2, 1, date(2026, 9, 30), sell=True) == pytest.approx(.10)
    assert cs.fees(2, 1, date(2026, 11, 11), sell=True) == pytest.approx(.09)
    assert cs.fees(2, 1, date(2026, 9, 30)) == pytest.approx(.08)
    with pytest.raises(ValueError):
        cs.fees(1.5, 1, date(2026, 11, 11))
    with pytest.raises(ValueError, match='calendar'):
        cs.next_settlement_day(date(2027, 12, 31))


def test_selection_uses_real_earliest_expiry_and_actual_strikes():
    contracts = [contract(strike=k) for k in (595, 600, 605)] + [contract(expiry='2026-11-13')]
    chosen = cs.choose_contract(contracts, date(2026, 11, 11), 5, 'SPY', 1, 600, .2)
    assert chosen['symbol'] in {c['symbol'] for c in contracts}
    assert chosen['expiry'] == '2026-11-11' and chosen['right'] == 'C'
    assert cs.choose_contract(contracts, date(2026, 11, 11), 5, 'SPY', -1, 600, .2) is None
    assert cs.years_to_expiry(date(2026, 11, 11), 5, date(2026, 11, 13)) > 2 / 365


def test_cash_proceeds_stay_unsettled_and_even_lots_close_without_leverage():
    account = cs.CashAccount(date(2026, 11, 11), 1500, .25, .01)
    ask, bid = np.full(86, 1.0), np.full(86, .8)
    account.enter(row(), contract(), catalog(), ask, bid, 5)
    assert account.entries == 1 and account.open[0]['quantity'] == 2
    paid = 202 + .08
    assert account.settled == pytest.approx(1500 - paid)
    account.sell(account.open[0], 2, 6, 'test')
    assert account.settled == pytest.approx(1500 - paid)
    assert account.pending[0]['settles_on'] == '2026-11-12'
    account.mark_and_manage(6, {})
    state = account.summary()
    assert state['net_pnl'] == pytest.approx(-44.17)
    assert state['return'] == pytest.approx(state['net_pnl'] / 1500)
    assert state['net_pnl'] == pytest.approx(state['gross_pnl_before_extra_slippage_and_fees'] - state['fees'] - state['extra_slippage_paid'])
    next_day = cs.CashAccount(date(2026, 11, 12), 1500, .25, .01, state)
    assert not next_day.pending and next_day.settled == pytest.approx(state['ending_equity'])
    assert next_day.max_dd == state['max_drawdown'] > 0


def test_unfunded_candidate_does_not_occupy_a_slot_and_unsettled_cash_cannot_be_spent():
    account = cs.CashAccount(date(2026, 11, 11), 1500, .25, 0)
    account.enter(row(), contract(), catalog(), np.full(86, 2.0), np.ones(86), 5)
    assert account.entries == 0 and account.decisions[-1]['reason'] == 'cannot_fund_two_contracts'
    account.settled = 0
    account.pending = [{'amount': 1500, 'settles_on': '2026-11-12'}]
    account.enter(row(), contract(), catalog(), np.ones(86), np.ones(86), 5)
    assert account.entries == 0
    account.settled, account.pending = 1500, []
    account.enter(row('QQQ'), contract('QQQ'), catalog(), np.ones(86), np.ones(86), 5)
    assert account.entries == 1


def test_tp1_sells_exact_half_then_gap_exit_uses_current_bid():
    account = cs.CashAccount(date(2026, 11, 11), 1500, .25, 0)
    bid = np.ones(86)
    bid[6], bid[7] = 2.0, .3
    account.enter(row(), contract(), catalog(), np.ones(86), bid, 5)
    features = {'SPY': SimpleNamespace(C=np.full(86, 600), V=np.ones(86))}
    account.mark_and_manage(6, features)
    assert account.open[0]['remaining'] == 1 and account.fills[-1]['quantity'] == 1
    account.mark_and_manage(7, features)
    assert not account.open and account.fills[-1]['model_price'] == .3
    assert account.trades[0]['net_pnl'] == pytest.approx(29.82)


def test_zero_bid_closing_fees_are_reserved_and_cannot_make_cash_negative():
    account = cs.CashAccount(date(2026, 9, 30), 1500, .25, .05)
    account.settled = 200.24
    account.pending = [{'amount': 1299.76, 'settles_on': '2026-10-01'}]
    account.enter(row(), contract(), catalog(), np.full(86, .95), np.zeros(86), 5)
    assert account.entries == 1 and account.open[0]['quantity'] == 2
    account.sell(account.open[0], 2, 6, 'zero_bid')
    account.mark_and_manage(6, {})
    assert account.settled >= 0
    assert account.summary()['net_pnl'] == pytest.approx(-200.17)


def test_slippage_changes_exit_paths_not_just_an_end_of_day_cost_subtraction():
    features = {'SPY': SimpleNamespace(C=np.full(86, 600), V=np.ones(86))}
    results = []
    for slip in (0, .01):
        account = cs.CashAccount(date(2026, 11, 11), 1500, .25, slip)
        bids = np.full(86, 1.0)
        bids[6], bids[7] = 1.9, 1.5
        account.enter(row(), contract(), catalog(), np.ones(86), bids, 5)
        for m in range(5, 86):
            account.mark_and_manage(m, features)
        results.append(account.summary())
    assert results[0]['trades'][0]['exit_reason'] == 'profit_trail'
    assert results[1]['trades'][0]['exit_reason'] == 'time_stop'


def test_strict_historical_replay_does_not_invent_catalogs(days):
    session = '2026-09-16'
    report = cs.evaluate(session, days[1][session], list(C.TICKERS), snapshots=[])
    assert report['status'] == 'awaiting_catalog'
    assert report['coverage']['qualified_p1a_candidates'] > 0
    assert all(a['ending_equity'] == 1500 and a['funded_trades'] == 0 for a in report['accounts'].values())
    assert all(c['reason'] == 'missing_causal_catalog' for a in report['accounts'].values() for c in a['candidate_decisions'])


def test_catalog_backed_evaluation_has_four_independent_cash_accounts(days, monkeypatch):
    # Controlled signals/prices isolate catalog timing and cash selection from detectors.
    source = next(iter(days[1].values()))
    day = {'SPY': next(iter(source.values()))}
    monkeypatch.setattr(cs, 'run_session', lambda *_a, **_k: (SimpleNamespace(candidate_log=[row()]), []))
    monkeypatch.setattr(cs, 'model_prices', lambda *_: (np.ones(390), np.full(390, .8)))
    report = cs.evaluate('2026-11-11', day, ['SPY'], snapshots=[catalog()])
    assert report['status'] == 'ok'
    assert report['coverage']['catalog_verified_candidates'] == 1
    assert [a['funded_trades'] for a in report['accounts'].values()] == [1] * 4
    assert all(a['open_positions'] == 0 for a in report['accounts'].values())
    assert report['accounts']['0.05']['net_pnl'] < report['accounts']['0.00']['net_pnl']
    future = catalog()
    future['observed_at'] = '2026-11-11T16:00:00+00:00'
    skipped = cs.evaluate('2026-11-11', day, ['SPY'], snapshots=[future])
    assert skipped['status'] == 'awaiting_catalog'


def test_history_is_causal_hash_linked_and_reconciled(days, tmp_path):
    tickers = list(C.TICKERS)
    first = cs.evaluate('2026-09-17', days[1]['2026-09-17'], tickers, snapshots=[])
    directory = tmp_path / 'shadow'
    directory.mkdir()
    path = directory / '2026-09-17.json'
    path.write_text(json.dumps({'date': first['date'], 'cash_only': first}))
    second = cs.evaluate('2026-09-18', days[1]['2026-09-18'], tickers, snapshots=[], ledger_dir=tmp_path)
    assert second['parent']['digest'] == cs.digest(first)
    assert second['cohort_start'] == first['date']
    assert second == cs.evaluate('2026-09-18', days[1]['2026-09-18'], tickers, snapshots=[], ledger_dir=tmp_path)
    changed = deepcopy(first)
    changed['accounts']['0.00']['settled_cash_end'] += 1
    path.write_text(json.dumps({'date': first['date'], 'cash_only': changed}))
    with pytest.raises(ValueError, match='reconcile'):
        cs.evaluate('2026-09-18', days[1]['2026-09-18'], tickers, snapshots=[], ledger_dir=tmp_path)
    path.write_text(json.dumps({'date': first['date'], 'cash_only': first}))
    with pytest.raises(ValueError, match='missing the previous'):
        cs.evaluate('2026-09-21', days[1]['2026-09-21'], tickers, snapshots=[], ledger_dir=tmp_path)
    (directory / '2026-09-18.json').write_text(json.dumps({'date': second['date'], 'cash_only': second}))
    # A financially consistent edit to an ancestor still invalidates descendants.
    changed = deepcopy(first)
    changed['coverage']['qualified_p1a_candidates'] += 1
    path.write_text(json.dumps({'date': first['date'], 'cash_only': changed}))
    with pytest.raises(ValueError, match='ancestor changed'):
        cs.evaluate('2026-09-21', days[1]['2026-09-21'], tickers, snapshots=[], ledger_dir=tmp_path)


def test_new_cash_report_does_not_change_alerts_or_existing_64_accounts(days, tmp_path, monkeypatch):
    session = date(2026, 9, 18)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'))
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(runner, 'final_day_inputs', lambda *_: (days[1][str(session)], {}))
    monkeypatch.setattr(runner.state, 'git_commit_push', lambda *_: None)
    posts = []
    monkeypatch.setattr(runner.alerts, 'post_discord', lambda _, msg, __: posts.append(msg))
    assert runner.run_eod(cfg, session) == 0
    path = tmp_path / 'ledger' / 'shadow' / f'{session}.json'
    original = json.loads(path.read_text())
    assert original['cash_only']['status'] == 'awaiting_catalog'
    ledger = (tmp_path / 'ledger' / 'trades.csv').read_bytes()
    def fail(*_args, **_kwargs):
        raise ValueError('broken catalog')
    monkeypatch.setattr(cs, 'evaluate', fail)
    assert runner.run_eod(cfg, session) == 0
    failed = json.loads(path.read_text())
    assert failed.pop('cash_only')['status'] == 'error'
    original.pop('cash_only')
    assert failed == original
    assert len(posts) == 2 and posts[0] == posts[1]
    assert ledger == (tmp_path / 'ledger' / 'trades.csv').read_bytes()
