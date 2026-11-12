"""Opt-in cash alert gate. Reserves cash; only confirmed fills move money.

No broker/order API is implemented. All private account state is encrypted.
Missing state, stale reconciliation or unapproved limits block new entries.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import fcntl
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import cash_catalog, cash_shadow
from .calendar_nyse import NY, is_full_session, session_open


class CashControlError(ValueError):
    pass


def cents(value):
    try:
        amount = Decimal(str(value))
        if isinstance(value, bool) or not amount.is_finite() or amount < 0:
            raise ValueError
        return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError):
        raise CashControlError('money must be finite and nonnegative') from None


def stamp(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise CashControlError('confirmation requires an aware timestamp') from None


def cash_cents(value):
    result = cents(value)
    if Decimal(str(value)) * 100 != result:
        raise CashControlError('cash balances, fees and premiums require exact cents')
    return result


def integer(value):
    if type(value) is not int or value < 1:
        raise CashControlError('quantity must be a positive whole contract count')
    return value


def fee_reserve(quantity):
    # One-contract split exits each incur rounding; reserve conservatively.
    return 8 * quantity


def opening_cost(quantity, price_cents, session):
    return quantity * price_cents * 100 + cents(cash_shadow.fees(quantity, price_cents / 100, session))


def quantity_for(limit_price, budget, session):
    price = cents(limit_price)
    if not price:
        raise CashControlError('entry limit must be positive')
    available = cash_cents(budget)
    qty = 2 * (available // (price * 100) // 2)
    while qty >= 2 and opening_cost(qty, price, session) + fee_reserve(qty) > available:
        qty -= 2
    return qty


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class CashBook:
    def __init__(self, path, key=None):
        from cryptography.fernet import Fernet
        try:
            self.crypt = Fernet((key or os.environ.get('CHRONOS_CASH_BOOK_KEY', '')).encode())
        except (ValueError, TypeError):
            raise CashControlError('private cash-book encryption key is not configured') from None
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with Path(str(self.path) + '.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        try:
            book = json.loads(self.crypt.decrypt(self.path.read_bytes()))
            if book['schema_version'] != 1 or book['account_type'] != 'cash':
                raise ValueError
            return book
        except Exception:
            raise CashControlError('cash book missing or invalid; reconciliation required') from None

    def _write(self, book):
        encoded = self.crypt.encrypt(json.dumps(book, sort_keys=True, allow_nan=False).encode())
        with NamedTemporaryFile(dir=self.path.parent, delete=False) as temp:
            name = Path(temp.name)
            try:
                temp.write(encoded)
                temp.flush()
                os.fsync(temp.fileno())
                temp.close()
                os.replace(name, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                name.unlink(missing_ok=True)

    def view(self):
        with self._locked():
            return self._read()

    @staticmethod
    def _mature(book, session):
        due = [p for p in book['unsettled'] if p['settles_on'] <= session]
        book['settled_cents'] += sum(p['amount_cents'] for p in due)
        book['unsettled'] = [p for p in book['unsettled'] if p['settles_on'] > session]

    @staticmethod
    def _cash(book):
        return book['settled_cents'] + sum(p['amount_cents'] for p in book['unsettled'])

    @staticmethod
    def _reserved(book):
        return sum(r['unfilled_reserve_cents'] + fee_reserve(r['held']) for r in book['reservations'].values())

    def initialize(self, snapshot, now):
        """Explicit initial operator/broker reconciliation; never inferred from capital."""
        with self._locked():
            if self.path.exists():
                raise CashControlError('refusing to replace an existing cash book')
            observed = stamp(snapshot['as_of'])
            session = now.astimezone(NY).date().isoformat()
            if (snapshot.get('confirmed') is not True or snapshot.get('account_type') != 'cash'
                    or snapshot.get('flat') is not True or snapshot.get('options_level2_approved') is not True
                    or observed > now or observed.astimezone(NY).date().isoformat() != session):
                raise CashControlError('a current confirmed flat cash account and options permission are required')
            settled = cash_cents(snapshot['settled_cash'])
            unsettled = []
            for item in snapshot.get('unsettled_proceeds', []):
                # Initial pending proceeds need the original sale date, so the
                # calendar, not a supplied maturity date, establishes settlement.
                trade = stamp(item['sold_at'])
                if trade > observed:
                    raise CashControlError('future pending proceeds')
                unsettled.append({'amount_cents': cash_cents(item['amount']),
                                  'settles_on': str(cash_shadow.next_settlement_day(trade.astimezone(NY).date()))})
            book = {'schema_version': 1, 'account_type': 'cash', 'settled_cents': settled,
                    'unsettled': unsettled, 'reservations': {}, 'receipts': {},
                    'confirmed_session': session, 'confirmed_at': observed.isoformat(),
                    'session': session, 'blocked': None, 'risk': deepcopy(snapshot.get('risk', {'approved': False})),
                    'equity_peak_cents': settled + sum(p['amount_cents'] for p in unsettled)}
            if self._cash(book) > 150000:
                raise CashControlError('initial cash allocation exceeds the specified $1500 account')
            self._mature(book, session)
            if book['risk'].get('approved') is True:
                self._limits(book)
            book['session_start_cents'] = self._cash(book)
            self._write(book)

    @staticmethod
    def _limits(book):
        risk = book['risk']
        if risk.get('approved') is not True:
            raise CashControlError('risk limits remain unapproved')
        try:
            values = [cash_cents(risk[k]) for k in ('max_trade_risk', 'max_daily_loss', 'max_drawdown')]
        except KeyError:
            raise CashControlError('approved risk limits are incomplete') from None
        if not 0 < values[0] <= values[1] <= values[2] <= 150000:
            raise CashControlError('risk limits must be positive, ordered and within the account')
        return values

    def reserve(self, identity, event, now, *, catalog_dir, slot_fraction=.25):
        if not 0 < slot_fraction <= .25:
            raise CashControlError('cash slot fraction must be positive and at most 25%')
        session = now.astimezone(NY).date()
        if (not is_full_session(session) or now < session_open(session)
                or now >= session_open(session).replace(hour=16, minute=0)):
            raise CashControlError('cash entries require the regular full trading session')
        with self._locked():
            book = self._read()
            self._mature(book, str(session))
            if book['blocked'] or book['confirmed_session'] != str(session) or stamp(book['confirmed_at']) > now:
                raise CashControlError('current account reconciliation required')
            max_trade, daily, drawdown = self._limits(book)
            if identity in book['reservations']:
                return deepcopy(book['reservations'][identity])
            if any(r['unfilled'] or r['exit_requests'] for r in book['reservations'].values()):
                raise CashControlError('unconfirmed order activity; reconcile fills or cancellation first')
            if any(r['held'] and stamp(r['created_at']).astimezone(NY).date() != session
                   for r in book['reservations'].values()):
                raise CashControlError('unresolved overnight position requires manual management')
            if sum(bool(r['held']) for r in book['reservations'].values()) >= 3:
                raise CashControlError('cash position slots full')
            d = event.data
            if d.get('structure') != 'single':
                raise CashControlError('cash account permits listed single-leg options only')
            symbol = cash_catalog.option_symbol(event.ticker, session, 'C' if d['direction'] == 1 else 'P', d['K1'])
            snapshots = cash_catalog.load_snapshots(catalog_dir, session)
            contracts, _, error = cash_catalog.contracts_at(snapshots, event.ticker, now)
            if error or not any(c['symbol'] == symbol for c in contracts):
                raise CashControlError('exact same-day standard contract is not in a causal listing catalog')
            price = cents(d['cost_basis'])
            if book['session'] != str(session):
                raise CashControlError('confirm account before the new session')
            reserved = self._reserved(book)
            worst = self._cash(book) - reserved
            budget = max(0, min(book['settled_cents'] - reserved,
                               int(self._cash(book) * slot_fraction), max_trade,
                               daily + worst - book['session_start_cents'],
                               drawdown + worst - book['equity_peak_cents']))
            qty = quantity_for(price / 100, budget / 100, session)
            if qty < 2:
                raise CashControlError('two contracts do not fit settled cash, fees and approved risk')
            reservation = {'identity': identity, 'symbol': symbol, 'created_at': now.isoformat(),
                           'quantity': qty, 'limit_price_cents': price,
                           'unfilled': qty, 'held': 0, 'bought': 0, 'sold': 0, 'exit_requests': {},
                           'buy_cost_cents': 0,
                           'maximum_risk_cents': opening_cost(qty, price, session) + fee_reserve(qty),
                           'unfilled_reserve_cents': opening_cost(qty, price, session) + fee_reserve(qty)}
            book['reservations'][identity] = reservation
            self._write(book)  # Persist BEFORE posting; failed/uncertain delivery never releases cash.
            return deepcopy(reservation)

    def request_exit(self, identity, request_id, kind):
        with self._locked():
            book = self._read()
            r = book['reservations'].get(identity)
            if not r or not r['held']:
                raise CashControlError('no confirmed holding; reconcile actual fills before selling')
            if r['unfilled']:
                raise CashControlError('buy order is partially filled; cancel/reconcile its remainder before selling')
            if request_id in r['exit_requests']:
                return r['exit_requests'][request_id]
            if r['exit_requests']:
                raise CashControlError('an exit is unconfirmed; cancel/reconcile it before another sale')
            qty = r['held'] if kind == 'EXIT' else max(0, min(r['held'], r['bought'] // 2 - r['sold']))
            if not qty:
                raise CashControlError('the confirmed TP1 quantity was already sold')
            r['exit_requests'][request_id] = qty
            self._write(book)
            return qty

    def claim_delivery(self, identity, request_id):
        """At-most-once intent attempt; uncertain delivery retains its reservation."""
        with self._locked():
            book = self._read()
            attempts = book['reservations'][identity].setdefault('alert_attempts', {})
            if request_id in attempts:
                return False
            attempts[request_id] = True
            self._write(book)
            return True

    def reconcile(self, record, now):
        """Apply one explicit confirmed fill/cancellation/reconciliation atomically."""
        with self._locked():
            book = self._read()
            original = deepcopy(book)
            try:
                if not isinstance(record, dict):
                    raise CashControlError('confirmation must be an object')
                if record.get('confirmed') is not True:
                    raise CashControlError('record is not confirmed')
                at = stamp(record['at'])
                if at > now:
                    raise CashControlError('future confirmation')
                identity = record['event_id']
                if not isinstance(identity, str) or not identity or len(identity) > 200:
                    raise CashControlError('invalid confirmation identity')
                fingerprint = _digest(record)
                if identity in book['receipts']:
                    if book['receipts'][identity] != fingerprint:
                        raise CashControlError('confirmation identity reused with different contents')
                    return
                session = str(now.astimezone(NY).date())
                self._mature(book, session)
                action = record['action']
                if action == 'confirm':
                    if at.astimezone(NY).date().isoformat() != session:
                        raise CashControlError('account confirmation is not for today')
                    positions = {key: r['held'] for key, r in book['reservations'].items() if r['held']}
                    if (cash_cents(record['settled_cash']) != book['settled_cents'] or record['positions'] != positions
                            or cash_cents(record['unsettled_cash']) != sum(p['amount_cents'] for p in book['unsettled'])):
                        raise CashControlError('broker balances or positions do not match the cash book')
                    if book['session'] != session:
                        book['session'], book['session_start_cents'] = session, self._cash(book)
                    book['confirmed_session'], book['confirmed_at'], book['blocked'] = session, at.isoformat(), None
                else:
                    r = book['reservations'][record['reservation_id']]
                    if at < stamp(r['created_at']):
                        raise CashControlError('fill or cancellation predates its reservation')
                    if action == 'cancel_buy':
                        r['unfilled'], r['unfilled_reserve_cents'] = 0, 0
                    elif action == 'cancel_sell':
                        r['exit_requests'].pop(record['request_id'])
                    elif action in ('buy', 'sell'):
                        qty, price, fee = integer(record['quantity']), cents(record['price']), cash_cents(record['fees'])
                        premium = cash_cents(Decimal(str(record['price'])) * qty * 100)
                        if record['symbol'] != r['symbol']:
                            raise CashControlError('fill symbol does not match reservation')
                        if action == 'buy':
                            if qty > r['unfilled']:
                                raise CashControlError('buy fill exceeds remaining order quantity')
                            book['settled_cents'] -= premium + fee
                            r['buy_cost_cents'] += premium + fee
                            r['held'] += qty
                            r['bought'] += qty
                            r['unfilled'] -= qty
                            trade_day = at.astimezone(NY).date()
                            r['unfilled_reserve_cents'] = (opening_cost(r['unfilled'], r['limit_price_cents'], trade_day)
                                                           + fee_reserve(r['unfilled'])) if r['unfilled'] else 0
                            if (price > r['limit_price_cents'] or book['settled_cents'] < self._reserved(book)
                                    or r['buy_cost_cents'] + r['unfilled_reserve_cents'] + fee_reserve(r['held'])
                                    > r['maximum_risk_cents']):
                                book['blocked'] = 'actual fill exceeded approved cash or price limits; review required'
                        else:
                            request_id = record['request_id']
                            if request_id not in r['exit_requests'] and record.get('manual_exit') is not True:
                                raise CashControlError('unmatched sale requires explicit manual-exit confirmation')
                            if qty > min(r['held'], r['exit_requests'].get(request_id, r['held'])):
                                raise CashControlError('sale exceeds confirmed holding or pending exit quantity')
                            r['held'] -= qty
                            r['sold'] += qty
                            if request_id in r['exit_requests']:
                                r['exit_requests'][request_id] -= qty
                                if not r['exit_requests'][request_id]:
                                    del r['exit_requests'][request_id]
                            net = premium - fee
                            if net < 0:
                                book['settled_cents'] += net
                            else:
                                book['unsettled'].append({'amount_cents': net,
                                    'settles_on': str(cash_shadow.next_settlement_day(at.astimezone(NY).date()))})
                            self._mature(book, session)
                    else:
                        raise CashControlError('unsupported confirmation action')
                if not any(r['held'] for r in book['reservations'].values()):
                    book['equity_peak_cents'] = max(book['equity_peak_cents'], self._cash(book))
                if book['settled_cents'] < 0:
                    book['blocked'] = 'negative actual settled cash; review required'
                book['receipts'][identity] = fingerprint
            except (CashControlError, KeyError, TypeError, ValueError):
                original['blocked'] = 'invalid reconciliation record; correct and reconcile before new entries'
                self._write(original)
                raise CashControlError('confirmation rejected; cash book blocked for reconciliation') from None
            self._write(book)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('initialize', 'reconcile'))
    parser.add_argument('--book', required=True)
    parser.add_argument('--input', required=True, help='Private confirmed snapshot/record JSON; never commit plaintext')
    args = parser.parse_args()
    book = CashBook(args.book)
    record = json.loads(Path(args.input).read_text())
    now = datetime.now(timezone.utc)
    if args.command == 'initialize':
        book.initialize(record, now)
    else:
        book.reconcile(record, now)
    print('Private cash record saved. No order or alert was sent.')


if __name__ == '__main__':
    main()
