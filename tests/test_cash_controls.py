from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet
import pytest

from apex_chronos import alerts, cash_catalog, runner, state
from apex_chronos.cash_controls import CashBook, CashControlError, cents, quantity_for
from apex_chronos.engine import Event
from apex_chronos.calendar_nyse import NY, session_open

NOW = datetime(2026, 11, 10, 15, 1, tzinfo=timezone.utc)
SYMBOL = 'SPY261110C00700000'


def entry(price=1., ticker='SPY', minute=30):
    return Event('ENTRY', ticker, 'Phase1A_GapTrap_Single0DTE', minute,
                 {'structure': 'single', 'direction': 1, 'K1': 700., 'cost_basis': price,
                  'slot': 0, 'phase': 'P1', 'legs': 'CALL 700', 'S_entry': 700., 'iv_entry': .2,
                  'rvol_entry': 1., 'score': 9.,
                  'plan': {'stop_roi': -.26, 'tp1_roi': .9, 'after_tp1_floor': .54, 'm_end': 80, 'max_hold': 50}})


def identity(e=None):
    return '2026-11-10|' + (e or entry()).key


@pytest.fixture
def setup(tmp_path):
    now = NOW
    key = Fernet.generate_key().decode()
    book = CashBook(tmp_path / 'cash-controls.enc', key)
    snapshot = {'confirmed': True, 'account_type': 'cash', 'flat': True, 'options_level2_approved': True,
                'as_of': (now - timedelta(minutes=1)).isoformat(), 'settled_cash': 1500.,
                'risk': {'approved': True, 'max_trade_risk': 375., 'max_daily_loss': 750., 'max_drawdown': 1000.}}
    book.initialize(snapshot, now)
    catalogs = tmp_path / 'cash_options'
    cash_catalog.save_snapshot(catalogs, {'schema_version': 1, 'observed_at': (now - timedelta(minutes=2)).isoformat(),
         'source': 'synthetic test catalog', 'contracts': [{'symbol': SYMBOL, 'ticker': 'SPY', 'expiry': '2026-11-10',
         'right': 'C', 'strike': 700., 'multiplier': 100, 'standard': True}]})
    return book, catalogs, key


def reserve(setup, event=None, when=NOW):
    book, catalogs, _ = setup
    e = event or entry()
    return book.reserve(identity(e), e, when, catalog_dir=catalogs)


def fill(book, action, event_id, quantity=2, price=1., fees=.08, at=None, **changes):
    record = {'confirmed': True, 'action': action, 'event_id': event_id, 'reservation_id': identity(),
              'symbol': SYMBOL, 'quantity': quantity, 'price': price, 'fees': fees,
              'at': (at or NOW + timedelta(seconds=5)).isoformat(), **changes}
    book.reconcile(record, at or NOW + timedelta(seconds=10))
    return record


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, True, None])
def test_invalid_money_cannot_inflate_cash(bad):
    with pytest.raises(CashControlError):
        cents(bad)


def test_sizing_includes_fees_and_full_loss_reserve():
    assert quantity_for(2., 375, NOW.date()) == 0
    assert quantity_for(1., 375, NOW.date()) == 2
    assert quantity_for(1., 200, NOW.date()) == 0
    assert quantity_for(1., 400.5, NOW.date()) == 4


def test_reservation_is_encrypted_persistent_and_idempotent(setup):
    book, _, key = setup
    r = reserve(setup)
    assert r['quantity'] == 2
    assert book.view()['settled_cents'] == 150000  # reservation is not a broker fill
    assert b'SPY' not in book.path.read_bytes() and b'150000' not in book.path.read_bytes()
    assert CashBook(book.path, key).view()['reservations'][identity()] == r
    assert reserve(setup) == r
    assert len(book.view()['reservations']) == 1


def test_missing_wrong_key_and_corruption_fail_closed(setup):
    book, catalogs, _ = setup
    with pytest.raises(CashControlError):
        CashBook(book.path, Fernet.generate_key().decode()).reserve(identity(), entry(), NOW, catalog_dir=catalogs)
    book.path.write_bytes(b'corrupt')
    with pytest.raises(CashControlError):
        reserve(setup)


def test_unconfirmed_order_blocks_other_entries_including_concurrent_calls(setup):
    def one(minute):
        try:
            reserve(setup, entry(minute=minute))
            return True
        except CashControlError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(one, (30, 31))) == 1


def test_pending_risk_missing_catalog_and_spreads_never_reserve(setup):
    book, catalogs, _ = setup
    e = entry()
    e.data['structure'] = 'debit_spread'
    with pytest.raises(CashControlError, match='single-leg'):
        reserve(setup, e)
    with pytest.raises(CashControlError, match='catalog'):
        book.reserve(identity(), entry(), NOW, catalog_dir=catalogs / 'missing')
    with book._locked():
        b = book._read()
        b['risk'] = {'approved': False}
        book._write(b)
    with pytest.raises(CashControlError, match='unapproved'):
        reserve(setup)
    assert not book.view()['reservations']


def test_partial_fill_and_confirmed_cancellation_release_only_unused_cash(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'buy-one', quantity=1, fees=.04)
    b = book.view()
    assert b['settled_cents'] == 139996 and b['reservations'][identity()]['held'] == 1
    with pytest.raises(CashControlError, match='unconfirmed'):
        reserve(setup, entry(minute=31))
    fill(book, 'cancel_buy', 'cancel-one')
    r = book.view()['reservations'][identity()]
    assert r['held'] == 1 and r['unfilled_reserve_cents'] == 0


def test_sales_use_actual_fills_and_holiday_settlement_not_model_exits(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'bought')
    assert book.view()['settled_cents'] == 129992
    book.request_exit(identity(), 'exit', 'EXIT')
    assert book.view()['settled_cents'] == 129992  # alert does not release a penny
    sold_at = NOW + timedelta(minutes=1)
    fill(book, 'sell', 'sold', price=1.1, fees=.09, at=sold_at, request_id='exit')
    b = book.view()
    assert b['settled_cents'] == 129992
    assert b['unsettled'] == [{'amount_cents': 21991, 'settles_on': '2026-11-12'}]  # Nov 11 bank holiday
    assert b['reservations'][identity()]['held'] == 0


def test_sell_cannot_exceed_confirmed_holding_or_duplicate_pending_exit(setup):
    book, _, _ = setup
    reserve(setup)
    with pytest.raises(CashControlError, match='no confirmed holding'):
        book.request_exit(identity(), 'tp1', 'TP1')
    fill(book, 'buy', 'buy')
    assert book.request_exit(identity(), 'tp1', 'TP1') == 1
    assert book.request_exit(identity(), 'tp1', 'TP1') == 1
    with pytest.raises(CashControlError, match='unconfirmed'):
        book.request_exit(identity(), 'exit', 'EXIT')
    fill(book, 'sell', 'tp1-fill', quantity=1, fees=.05, request_id='tp1')
    assert book.request_exit(identity(), 'exit', 'EXIT') == 1


def test_repeated_fill_is_idempotent_and_changed_duplicate_blocks_book(setup):
    book, _, _ = setup
    reserve(setup)
    record = fill(book, 'buy', 'buy')
    before = book.view()
    book.reconcile(record, NOW + timedelta(seconds=10))
    assert book.view() == before
    with pytest.raises(CashControlError, match='blocked'):
        book.reconcile({**record, 'quantity': 1}, NOW + timedelta(seconds=10))
    assert book.view()['blocked']
    assert book.view()['settled_cents'] == before['settled_cents']


def test_actual_over_limit_fill_is_recorded_and_blocks_future_entries(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'bad-price', price=1.01)
    assert book.view()['settled_cents'] == 129792
    assert book.view()['blocked']
    with pytest.raises(CashControlError, match='reconciliation'):
        reserve(setup, entry(minute=31))


def test_reconcile_next_session_matures_only_due_funds_and_never_resets_capital(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'bought')
    book.request_exit(identity(), 'exit', 'EXIT')
    fill(book, 'sell', 'sold', price=.7, fees=.09, request_id='exit')
    next_day = datetime(2026, 11, 12, 14, 20, tzinfo=timezone.utc)
    assert book.view()['settled_cents'] == 129992
    book.reconcile({'confirmed': True, 'event_id': 'reconciled12', 'action': 'confirm', 'at': next_day.isoformat(),
                    'settled_cash': 1439.83, 'unsettled_cash': 0, 'positions': {}}, next_day)
    b = book.view()
    assert b['settled_cents'] == 143983 and not b['unsettled']
    assert b['session_start_cents'] == 143983


def test_account_mismatch_future_and_fractional_fill_block_new_entries(setup):
    book, _, _ = setup
    reserve(setup)
    with pytest.raises(CashControlError):
        fill(book, 'buy', 'fractional', quantity=1.5)
    assert book.view()['blocked']


def test_insufficient_settled_funds_does_not_spend_unsettled_cash(setup):
    book, _, _ = setup
    with book._locked():
        b = book._read()
        b['settled_cents'] = 20000
        b['unsettled'] = [{'amount_cents': 130000, 'settles_on': '2026-11-12'}]
        book._write(b)
    with pytest.raises(CashControlError, match='two contracts'):
        reserve(setup)


def test_cash_mode_missing_book_never_falls_back_to_legacy_entry(tmp_path, monkeypatch):
    messages = []
    monkeypatch.delenv('CHRONOS_CASH_BOOK_KEY', raising=False)
    monkeypatch.setattr(alerts, 'post_discord', lambda _, text, **kw: messages.append(text) or True)
    cfg = runner.Settings(state_dir=str(tmp_path), cash_controls_enabled=True)
    s = runner.LiveSession(cfg, NOW.astimezone(NY).date())
    assert s.dispatch([entry()], NOW) == 0
    assert len(messages) == 1 and 'Cash action blocked' in messages[0]
    assert not s.posted_entries


def test_cash_settings_stay_disabled_by_default_and_reject_malformed_enable(monkeypatch):
    monkeypatch.delenv('CASH_CONTROLS_ENABLED', raising=False)
    assert runner.Settings.from_env().cash_controls_enabled is False
    monkeypatch.setenv('CASH_CONTROLS_ENABLED', 'tru')
    with pytest.raises(ValueError):
        runner.Settings.from_env()


@pytest.mark.parametrize('enabled', [False, True])
def test_runner_data_guard_activation_matches_cash_flag(tmp_path, enabled):
    cfg = runner.Settings(state_dir=str(tmp_path), cash_controls_enabled=enabled)
    sess = runner.LiveSession(cfg, date(2026, 11, 10))
    assert sess.engine.protect_degraded is enabled


def test_guarded_dispatch_uses_fixed_quantity_and_reserves_before_delivery(setup, monkeypatch):
    book, _, key = setup
    monkeypatch.setenv('CHRONOS_CASH_BOOK_KEY', key)
    messages = []
    published = []
    monkeypatch.setenv('APEX_GIT_PUSH', '1')
    monkeypatch.setattr(state, 'git_commit_push', lambda paths, _: published.append(paths) or True)
    def record(_, text, **kw):
        assert identity() in book.view()['reservations']
        assert published and published[-1] == [str(book.path)]
        messages.append(text)
        return True
    monkeypatch.setattr(alerts, 'post_discord', record)
    cfg = runner.Settings(state_dir=str(book.path.parent), cash_controls_enabled=True, capital=1500)
    sess = runner.LiveSession(cfg, NOW.astimezone(NY).date())
    assert sess.dispatch([entry()], NOW) == 1
    assert 'BUY LIMIT 2 whole contracts' in messages[0] and '-> 3 contract(s)' not in messages[0]
    assert book.view()['settled_cents'] == 150000
    assert sess.dispatch([entry()], NOW) == 0
    fill(book, 'buy', 'confirmed-buy')
    tp = Event('TP1', 'SPY', entry().module, 31, {'m_entry': 30})
    assert sess.dispatch([tp], NOW + timedelta(minutes=1)) == 1
    assert 'sell up to 1 confirmed whole' in messages[-1]
    assert book.view()['settled_cents'] == 129992 and not book.view()['unsettled']


def test_uncertain_post_retains_reservation_and_exit_requires_confirmed_holding(setup, monkeypatch):
    book, _, key = setup
    monkeypatch.setenv('CHRONOS_CASH_BOOK_KEY', key)
    monkeypatch.setattr(alerts, 'post_discord', lambda *args, **kwargs: False)
    sess = runner.LiveSession(runner.Settings(state_dir=str(book.path.parent), cash_controls_enabled=True), date(2026, 11, 10))
    assert sess.dispatch([entry()], NOW) == 0
    assert book.view()['reservations'][identity()]['unfilled'] == 2
    assert book.view()['settled_cents'] == 150000
    with pytest.raises(CashControlError, match='no confirmed holding'):
        book.request_exit(identity(), 'exit', 'EXIT')


def test_initialization_does_not_assume_the_account_is_fully_settled(tmp_path):
    book = CashBook(tmp_path / 'book.enc', Fernet.generate_key().decode())
    with pytest.raises(CashControlError):
        book.initialize({'confirmed': False, 'as_of': NOW.isoformat()}, NOW)
    assert not book.path.exists()


def test_future_confirmation_and_account_mismatch_cannot_reset_money(setup):
    book, _, _ = setup
    with pytest.raises(CashControlError):
        book.reconcile({'confirmed': True, 'action': 'confirm', 'event_id': 'future',
                        'at': (NOW + timedelta(days=1)).isoformat()}, NOW)
    assert book.view()['blocked'] and book.view()['settled_cents'] == 150000
    with pytest.raises(CashControlError):
        book.reconcile({'confirmed': True, 'action': 'confirm', 'event_id': 'mismatch', 'at': NOW.isoformat(),
                        'settled_cash': 1501., 'unsettled_cash': 0, 'positions': {}}, NOW)
    assert book.view()['settled_cents'] == 150000


def test_manual_exit_fill_can_be_recorded_without_inventing_an_exit_alert(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'bought')
    fill(book, 'sell', 'manual-close', request_id='broker-manual-exit', manual_exit=True)
    assert book.view()['reservations'][identity()]['held'] == 0
    assert book.view()['unsettled']


def test_price_improvement_is_booked_exactly_without_rounding_a_credit_up(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'improved', price=.995)
    assert book.view()['settled_cents'] == 130092


def test_risk_budget_reserves_full_premium_of_open_positions(setup):
    book, _, _ = setup
    reserve(setup)
    fill(book, 'buy', 'bought')
    with book._locked():
        b = book._read()
        b['risk']['max_daily_loss'] = 375.
        book._write(b)
    with pytest.raises(CashControlError, match='two contracts'):
        reserve(setup, entry(minute=31))


def test_no_new_entries_after_midnight_without_fresh_account_confirmation(setup):
    with pytest.raises(CashControlError, match='reconciliation'):
        reserve(setup, when=NOW + timedelta(days=1))


def test_failed_durable_publish_cannot_send_a_cash_entry_or_free_reservation(setup, monkeypatch):
    book, _, key = setup
    monkeypatch.setenv('CHRONOS_CASH_BOOK_KEY', key)
    monkeypatch.setenv('APEX_GIT_PUSH', '1')
    monkeypatch.setattr(state, 'git_commit_push', lambda *args: False)
    texts = []
    monkeypatch.setattr(alerts, 'post_discord', lambda _, text, **kw: texts.append(text) or True)
    cfg = runner.Settings(state_dir=str(book.path.parent), cash_controls_enabled=True)
    sess = runner.LiveSession(cfg, date(2026, 11, 10))
    assert sess.dispatch([entry()], NOW) == 0
    assert all('Cash action blocked' in t for t in texts)
    assert book.view()['reservations'][identity()]['unfilled'] == 2


def test_claimed_delivery_survives_restart_without_duplicate_order_intent(setup):
    book, _, key = setup
    reserve(setup)
    assert book.claim_delivery(identity(), identity())
    restarted = CashBook(book.path, key)
    assert not restarted.claim_delivery(identity(), identity())


def test_confirmation_cannot_round_a_cash_credit_up(setup):
    book, _, _ = setup
    with pytest.raises(CashControlError):
        book.reconcile({'confirmed': True, 'action': 'confirm', 'event_id': 'fractional-cent', 'at': NOW.isoformat(),
                        'settled_cash': 1499.999, 'unsettled_cash': 0, 'positions': {}}, NOW)
    assert book.view()['settled_cents'] == 150000 and book.view()['blocked']


def test_half_exit_uses_actual_purchased_count_after_partial_cancellation(setup):
    book, _, _ = setup
    r = reserve(setup, entry(price=.5))
    assert r['quantity'] == 6
    fill(book, 'buy', 'partial-two', quantity=2, price=.5)
    with pytest.raises(CashControlError, match='partially filled'):
        book.request_exit(identity(), 'tp1', 'TP1')
    fill(book, 'cancel_buy', 'rest-canceled')
    assert book.request_exit(identity(), 'tp1', 'TP1') == 1


def test_cash_entry_is_blocked_after_the_close(setup):
    with pytest.raises(CashControlError, match='regular full trading session'):
        reserve(setup, when=datetime(2026, 11, 10, 22, tzinfo=timezone.utc))
