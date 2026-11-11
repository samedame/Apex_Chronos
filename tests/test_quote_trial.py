import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from apex_chronos import quote_trial as qt
from apex_chronos.option_quotes import Quote, digest
from apex_chronos.quote_collector import TrialSession, collect
from apex_chronos.trial_engine import PaperAccount

NOW = datetime(2026, 11, 11, 14, 36, tzinfo=timezone.utc)
SYMBOL = 'SPY261111C00700000'


@pytest.fixture
def spec():
    result = qt.proposed_spec(datetime(2026, 11, 10, 18, tzinfo=timezone.utc))
    result['risk'] = {'approved': True, 'max_trade_risk': 300., 'max_daily_loss': 600., 'max_drawdown': 900.}
    return result


def quote(now=NOW, **changes):
    fields = dict(symbol=SYMBOL, bid=.99, ask=1.0, bid_size=20, ask_size=20,
                  market_at=now.isoformat(), observed_at=now.isoformat(),
                  bid_exchange='C', ask_exchange='C', condition='A')
    return Quote(**{**fields, **changes})


def candidate(identity='first', now=NOW, **changes):
    row = {'candidate_id': identity, 'arm': 'candidate', 'ticker': 'SPY', 'score': 9.0,
           'signal_at': now.isoformat(), 'observed_at': now.isoformat(),
           'contract': {'symbol': SYMBOL, 'ticker': 'SPY', 'expiry': '2026-11-11'}}
    return {**row, **changes}


def account(spec, scenario='slip_0.00', prior=None):
    return PaperAccount(spec, '2026-11-11', 'candidate', spec['scenarios'][scenario], prior)


def test_real_quote_funding_whole_contracts_and_t_plus_one(spec):
    a = account(spec)
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    assert a.funded == 1 and a.open['first']['quantity'] == 2
    assert a.settled == pytest.approx(1299.92)
    later = NOW + timedelta(minutes=1)
    a.tick(later, {SYMBOL: quote(later, bid=.70, ask=.71)})
    r = a.summary()
    assert r['ending_equity'] == pytest.approx(1439.83)
    assert r['net_pnl'] == pytest.approx(-60.17)
    assert r['settled_cash'] == pytest.approx(1299.92)
    assert r['unsettled_proceeds'] == [{'amount': pytest.approx(139.91), 'settles_on': '2026-11-12'}]
    next_day = PaperAccount(spec, '2026-11-12', 'candidate', spec['scenarios']['slip_0.00'], r)
    assert next_day.settled == pytest.approx(1439.83) and not next_day.pending


def test_unapproved_limits_cannot_start_funded_trial(spec):
    spec['risk'] = {'approved': False, 'max_trade_risk': None, 'max_daily_loss': None, 'max_drawdown': None}
    a = account(spec)
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    assert a.funded == 0 and a.decisions[-1]['reason'] == 'risk_limits_pending'


def test_full_premium_risk_cap_can_force_zero_trades(spec):
    spec['risk'] = {'approved': True, 'max_trade_risk': 30., 'max_daily_loss': 75., 'max_drawdown': 150.}
    a = account(spec)
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    assert not a.open
    a.tick(NOW + timedelta(seconds=61), {})
    assert a.missed == 1 and a.funded == 0


def test_duplicate_quote_size_cannot_be_reused_for_partial_exits(spec):
    a = account(spec)
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    tp_at = NOW + timedelta(minutes=1)
    tp_quote = quote(tp_at, bid=1.9, ask=1.91, bid_size=1)
    a.tick(tp_at, {SYMBOL: tp_quote})
    assert a.open['first']['remaining'] == 1
    assert a.open['first']['tp_filled'] == 1
    assert a.availability(tp_quote, 'sell') == 0
    repeat = quote(tp_at, bid=1.9, ask=1.91, bid_size=1,
                   observed_at=(tp_at + timedelta(seconds=5)).isoformat())
    a.tick(tp_at + timedelta(seconds=5), {SYMBOL: repeat})
    assert a.open['first']['remaining'] == 1
    next_at = tp_at + timedelta(minutes=1)
    a.tick(next_at, {SYMBOL: quote(next_at, bid=1.2, ask=1.21, bid_size=1)})
    assert not a.open and a.summary()['net_pnl'] == pytest.approx(109.82)
    assert all(type(f['quantity']) is int for f in a.fills)


def test_entry_delay_staleness_and_missed_fills_are_actual_execution_paths(spec):
    delayed = account(spec, 'delay_60s')
    delayed.candidate(candidate())
    delayed.tick(NOW, {SYMBOL: quote()})
    assert not delayed.open
    delayed.tick(NOW + timedelta(seconds=60), {SYMBOL: quote()})
    assert not delayed.open  # Old quote is stale; no retroactive fill.
    fresh_at = NOW + timedelta(seconds=65)
    delayed.tick(fresh_at, {SYMBOL: quote(fresh_at, ask=1.1, bid=1.09)})
    assert delayed.open['first']['entry_price'] == pytest.approx(1.11)
    missed = account(spec, 'miss_first_entry')
    missed.candidate(candidate())
    missed.tick(NOW, {SYMBOL: quote()})
    assert missed.funded == 0 and missed.missed == 1


def test_quote_size_one_crossed_and_indicative_never_fund_two_contracts(spec):
    for q in (quote(ask_size=1), quote(bid=1.01), quote(feed='indicative')):
        a = account(spec)
        a.candidate(candidate())
        a.tick(NOW, {SYMBOL: q})
        assert not a.open and a.funded == 0


def test_original_score_priority_is_preserved_before_slot_selection(spec):
    spec['slots'] = 1
    a = account(spec)
    a.candidate(candidate('lower', score=1))
    qqq = 'QQQ261111C00700000'
    a.candidate(candidate('higher', ticker='QQQ', score=10, contract={'symbol': qqq}))
    a.tick(NOW, {SYMBOL: quote(), qqq: quote(symbol=qqq)})
    assert list(a.open) == ['higher']
    assert a.decisions[0]['reason'] == 'candidate_logged_before_funding'


def test_unsettled_proceeds_cannot_fund_a_replacement_and_daily_loss_reserve_is_enforced(spec):
    a = account(spec)
    a.settled, a.pending = 0., [{'amount': 1500., 'settles_on': '2026-11-12'}]
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    assert not a.open
    a = account(spec)
    spec['risk']['max_daily_loss'] = 250.
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    second_at = NOW + timedelta(seconds=5)
    qqq = 'QQQ261111C00700000'
    a.candidate(candidate('next', second_at, ticker='QQQ', contract={'symbol': qqq}))
    a.tick(second_at, {SYMBOL: quote(second_at), qqq: quote(second_at, symbol=qqq)})
    assert len(a.open) == 1
    assert a.cash - sum(a.reserve(p['remaining']) for p in a.open.values()) >= a.start - 250


def test_missing_exit_quotes_do_not_fabricate_a_flat_result(spec):
    a = account(spec)
    a.candidate(candidate())
    a.tick(NOW, {SYMBOL: quote()})
    a.tick(NOW + timedelta(minutes=60), {})
    r = a.summary()
    assert r['unresolved'] and r['open_positions'] == 1 and r['closed_quote_validated_trades'] == 0
    assert r['ending_equity'] < 1300
    assert len(a.fills) == 1 and a.quote_gaps == 1


def test_frozen_spec_detects_changes_and_has_no_auto_live_switch(spec):
    qt.validate_spec(spec)
    changed = deepcopy(spec)
    changed['source_sha256']['trial_engine.py'] = 'changed'
    with pytest.raises(ValueError, match='source changed'):
        qt.validate_spec(changed)
    changed = deepcopy(spec)
    changed['automatic_live_promotion'] = True
    with pytest.raises(ValueError):
        qt.validate_spec(changed)
    spec['risk']['max_trade_risk'] = 0
    with pytest.raises(ValueError, match='positive dollar'):
        qt.validate_spec(spec)


def test_fixture_and_late_capture_do_not_count_as_prospective_sessions(spec):
    engine = TrialSession(spec, '2026-11-11')
    engine.ingest({'sequence': 0, 'observed_at': NOW.isoformat(), 'capture_mode': 'fixture',
                   'candidates': [], 'quotes': [asdict(quote())], 'covered_minutes': list(range(86)), 'errors': []})
    report = engine.report()
    assert report['synthetic'] and not report['eligible_session']
    assert not report['quality']['full_collection_window']
    assert not any('fills' in a for accounts in report['accounts'].values() for a in accounts.values())


def test_trial_stays_not_ready_without_real_sessions_or_funded_trades(spec, tmp_path):
    result = qt.progress(tmp_path, spec)
    assert result['status'] == 'not_ready' and result['eligible_sessions'] == 0
    assert result['closed_quote_validated_trades'] == 0 and not result['automatic_live_promotion']
    assert result['candidate_vs_cash_interval']['lower'] is None


def test_day_block_intervals_use_predeclared_blocks_and_show_uncertainty(spec):
    r = qt.confidence_interval([.01] * 60, spec)
    assert r['blocks'] == 12 and r['lower'] == pytest.approx(.01)
    noisy = qt.confidence_interval([.03] * 5 + [-.03] * 5 + [.01] * 5 + [-.01] * 5, spec)
    assert noisy['lower'] < 0 < noisy['upper']
    assert qt.confidence_interval([.01] * 4, spec)['lower'] is None


def test_no_access_is_explicit_and_does_not_contact_market_or_send_alerts(spec, tmp_path, monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID', raising=False)
    monkeypatch.delenv('APCA_API_SECRET_KEY', raising=False)
    monkeypatch.delenv('CHRONOS_QUOTE_ARCHIVE_KEY', raising=False)
    monkeypatch.delenv('APEX_GIT_PUSH', raising=False)
    import requests
    monkeypatch.setattr(requests.Session, 'request', lambda *_a, **_k: pytest.fail('Unexpected network call'))
    result = collect(spec, tmp_path / 'state', tmp_path / 'ledger')
    assert 'awaiting_quote_credentials' in result['status']
    assert result['eligible_sessions_added'] == result['quote_validated_trades_added'] == 0
    assert (tmp_path / 'ledger/quote_trials/setup-status.json').is_file()
