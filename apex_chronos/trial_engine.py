"""Whole-contract, quote-priced paper accounts. Never sends orders or alerts."""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, timedelta

from .cash_catalog import timestamp
from .cash_shadow import fees, next_settlement_day


class PaperAccount:
    def __init__(self, spec, session, arm, scenario, prior=None):
        self.spec, self.session, self.arm, self.scenario = spec, date.fromisoformat(session), arm, scenario
        self.prior = prior or {}
        self.capital = spec['capital']
        self.settled = float(self.prior.get('settled_cash', self.capital))
        pending = self.prior.get('unsettled_proceeds', [])
        self.settled += sum(p['amount'] for p in pending if date.fromisoformat(p['settles_on']) <= self.session)
        self.pending = [dict(p) for p in pending if date.fromisoformat(p['settles_on']) > self.session]
        self.start = self.cash
        self.equity = self.start
        self.peak = float(self.prior.get('equity_peak', self.capital))
        self.max_dd = float(self.prior.get('max_drawdown', 0))
        self.max_dd_dollars = float(self.prior.get('max_drawdown_dollars', 0))
        self.day_peak, self.day_dd = self.start, 0.0
        self.halted = self.prior.get('drawdown_halted', False)
        self.daily_halted = False
        self.pending_entries, self.open, self.used_quotes = {}, {}, {}
        self.decisions, self.fills, self.trades, self.curve = [], [], [], []
        self.fee_total = self.slip_total = 0.0
        self.funded = self.missed = self.candidate_count = 0
        self.last_tick = None
        self.quote_gaps = 0
        self.first_missed = False
        self.used_tickers = set()

    @property
    def cash(self):
        return self.settled + sum(p['amount'] for p in self.pending)

    @staticmethod
    def reserve(quantity):
        # Conservative allowance for split closing fills, even at a zero bid.
        return .06 * quantity + .02 if quantity else 0.0

    def candidate(self, row):
        if row['arm'] != self.arm:
            return
        identity = row['candidate_id']
        if identity in self.pending_entries or any(d['candidate_id'] == identity for d in self.decisions):
            return
        self.candidate_count += 1
        self.decisions.append({'candidate_id': identity, 'reason': 'candidate_logged_before_funding',
                               'observed_at': row['observed_at']})
        if row.get('skip_reason'):
            self.decisions.append({'candidate_id': identity, 'reason': row['skip_reason']})
            return
        signal, observed = timestamp(row['signal_at']), timestamp(row['observed_at'])
        if observed < signal or (observed - signal).total_seconds() > self.spec['signal_max_lag_seconds']:
            self.decisions.append({'candidate_id': identity, 'reason': 'late_or_future_signal'})
            return
        if not self.spec['risk']['approved']:
            self.decisions.append({'candidate_id': identity, 'reason': 'risk_limits_pending'})
            return
        if self.halted or self.daily_halted:
            self.decisions.append({'candidate_id': identity, 'reason': 'risk_halt'})
            return
        if self.scenario['miss_first_entry'] and not self.first_missed:
            self.first_missed = True
            self.missed += 1
            self.decisions.append({'candidate_id': identity, 'reason': 'predeclared_missed_entry_stress'})
            return
        due = observed + timedelta(seconds=self.scenario['entry_delay_seconds'])
        self.pending_entries[identity] = {**row, 'due': due,
                                         'expires': due + timedelta(seconds=self.spec['entry_quote_timeout_seconds'])}

    def availability(self, quote, side):
        key = (quote.side_identity(side), side)
        size = quote.ask_size if side == 'buy' else quote.bid_size
        return max(0, size - self.used_quotes.get(key, 0))

    def consume(self, quote, side, quantity):
        key = (quote.side_identity(side), side)
        if quantity > self.availability(quote, side):
            raise ValueError('paper fill exceeds remaining displayed quote size')
        self.used_quotes[key] = self.used_quotes.get(key, 0) + quantity

    def entry(self, row, quote, now):
        risk = self.spec['risk']
        if self.halted or self.daily_halted:
            return 'risk_halt'
        if len(self.open) >= self.spec['slots']:
            return 'slots_full'
        if self.funded >= self.spec['daily_entry_limit']:
            return 'daily_entry_limit'
        if row['ticker'] in self.used_tickers:
            return 'ticker_already_funded'
        price = round(quote.ask + self.scenario['extra_slippage'], 8)
        reserves = sum(self.reserve(p['remaining']) for p in self.open.values())
        # Existing long options may fall to zero. Their liquidation value is
        # excluded from worst-case equity when reserving the daily loss budget.
        worst_equity = self.cash - reserves
        risk_room = min(risk['max_trade_risk'],
                        risk['max_daily_loss'] + worst_equity - self.start,
                        risk['max_drawdown'] + worst_equity - self.peak)
        budget = max(0, min(self.equity * self.spec['slot_fraction'], self.settled - reserves, risk_room))
        qty = min(2 * math.floor(budget / (price * 100) / 2), 2 * (self.availability(quote, 'buy') // 2))
        while qty >= 2 and qty * price * 100 + fees(qty, price, self.session) + self.reserve(qty) > budget + 1e-9:
            qty -= 2
        if qty < 2:
            return 'insufficient_size_or_cash_or_risk_budget'
        charge = fees(qty, price, self.session)
        self.consume(quote, 'buy', qty)
        self.settled -= qty * price * 100 + charge
        self.fee_total += charge
        slip = qty * (price - quote.ask) * 100
        self.slip_total += slip
        self.funded += 1
        self.used_tickers.add(row['ticker'])
        self.open[row['candidate_id']] = {
            'candidate': row, 'quantity': qty, 'remaining': qty, 'entry_price': price,
            'entry_quote': asdict(quote), 'entry_at': now.isoformat(),
            'deadline': now + timedelta(minutes=self.spec['hold_minutes']),
            'last_manage_minute': int(now.timestamp() // 60), 'peak_roi': 0.0,
            'tp_filled': 0, 'tp_target': qty // 2, 'stop': -self.spec['stop_fraction'],
            'exit_order': None, 'fees': charge, 'slippage': slip, 'proceeds': 0.0,
        }
        self.fills.append({'side': 'buy', 'candidate_id': row['candidate_id'], 'at': now.isoformat(),
                           'quantity': qty, 'price': price, 'fee': charge, 'quote': asdict(quote),
                           'quote_identity': quote.identity})
        if self.settled < -1e-7:
            raise ValueError('paper account overspent settled cash')
        return 'filled'

    def sell(self, identity, quote, now):
        p = self.open[identity]
        order = p['exit_order']
        if order is None:
            return
        qty = min(order['quantity'], self.availability(quote, 'sell'), p['remaining'])
        if qty <= 0:
            return
        price = max(0, round(quote.bid - self.scenario['extra_slippage'], 8))
        charge = fees(qty, price, self.session, sell=True)
        net = qty * price * 100 - charge
        self.consume(quote, 'sell', qty)
        if net >= 0:
            self.pending.append({'amount': net, 'settles_on': str(next_settlement_day(self.session))})
        else:
            self.settled += net
        if self.settled < -1e-7:
            raise ValueError('paper account could not pay reserved closing fees')
        slip = qty * (quote.bid - price) * 100
        self.fee_total += charge
        self.slip_total += slip
        p['remaining'] -= qty
        p['proceeds'] += qty * price * 100
        p['fees'] += charge
        p['slippage'] += slip
        if order['reason'] == 'tp1':
            p['tp_filled'] += qty
        order['quantity'] -= qty
        self.fills.append({'side': 'sell', 'candidate_id': identity, 'at': now.isoformat(),
                           'quantity': qty, 'price': price, 'fee': charge, 'reason': order['reason'],
                           'quote': asdict(quote), 'quote_identity': quote.identity,
                           'settles_on': str(next_settlement_day(self.session))})
        if order['quantity'] == 0:
            p['exit_order'] = None
        if p['remaining'] == 0:
            self.trades.append({'candidate_id': identity, 'ticker': p['candidate']['ticker'],
                                'quantity': p['quantity'], 'entry_at': p['entry_at'], 'exit_at': now.isoformat(),
                                'net_pnl': p['proceeds'] - p['quantity'] * p['entry_price'] * 100 - p['fees'],
                                'fees': p['fees'], 'extra_slippage_paid': p['slippage'],
                                'quote_validated': True})
            del self.open[identity]

    def tick(self, now, quotes):
        if self.last_tick is not None and now < self.last_tick:
            raise ValueError('paper quote replay must be chronological')
        self.last_tick = now
        valid = {symbol: q for symbol, q in quotes.items() if q.rejection(now,
                 max_age_seconds=self.spec['quote_max_age_seconds'],
                 max_spread_fraction=self.spec['max_spread_fraction']) is None}
        # Funding decisions precede this poll's exits, as in the existing bot.
        ordered = sorted(self.pending_entries.items(), key=lambda item: (
            item[1]['signal_at'], -item[1]['score'], self.spec['tickers'].index(item[1]['ticker'])))
        for identity, row in ordered:
            if now < row['due']:
                continue
            if now > row['expires']:
                self.decisions.append({'candidate_id': identity, 'reason': 'quote_or_capacity_timeout'})
                self.missed += 1
                del self.pending_entries[identity]
                continue
            quote = valid.get(row['contract']['symbol'])
            if quote is not None:
                reason = self.entry(row, quote, now)
                if reason == 'filled' or reason in ('risk_halt', 'daily_entry_limit', 'ticker_already_funded'):
                    self.decisions.append({'candidate_id': identity, 'reason': reason})
                    del self.pending_entries[identity]
        minute = int(now.timestamp() // 60)
        for identity, p in list(self.open.items()):
            quote = valid.get(p['candidate']['contract']['symbol'])
            if quote is None:
                self.quote_gaps += 1
                continue
            bid = max(0, quote.bid - self.scenario['extra_slippage'])
            roi = bid / p['entry_price'] - 1
            if p['exit_order'] is None and minute > p['last_manage_minute']:
                p['last_manage_minute'] = minute
                p['peak_roi'] = max(p['peak_roi'], roi)
                if p['tp_filled'] < p['tp_target'] and roi >= self.spec['tp1_fraction'] - 1e-12:
                    p['exit_order'] = {'quantity': p['tp_target'] - p['tp_filled'], 'reason': 'tp1'}
                if p['tp_filled'] >= p['tp_target']:
                    p['stop'] = max(self.spec['tp1_fraction'] * self.spec['floor_fraction'],
                                    p['peak_roi'] * self.spec['trail_fraction'])
                if roi <= p['stop'] + 1e-12:
                    p['exit_order'] = {'quantity': p['remaining'], 'reason': 'stop_or_trail'}
                elif now >= p['deadline']:
                    p['exit_order'] = {'quantity': p['remaining'], 'reason': 'time_stop'}
            if self.halted or self.daily_halted:
                p['exit_order'] = {'quantity': p['remaining'], 'reason': 'risk_halt'}
            self.sell(identity, quote, now)
        # Missing marks use a zero option value. They never manufacture an exit
        # fill, and unresolved positions prevent a readiness pass.
        self.equity = self.cash
        for p in self.open.values():
            q = valid.get(p['candidate']['contract']['symbol'])
            bid = max(0, q.bid - self.scenario['extra_slippage']) if q else 0.0
            executable = min(p['remaining'], self.availability(q, 'sell')) if q else 0
            closing_fee = fees(executable, bid, self.session, sell=True) if executable else 0.0
            closing_fee += self.reserve(p['remaining'] - executable)
            self.equity += executable * bid * 100 - closing_fee
        self.peak, self.day_peak = max(self.peak, self.equity), max(self.day_peak, self.equity)
        self.max_dd_dollars = max(self.max_dd_dollars, self.peak - self.equity)
        self.max_dd = max(self.max_dd, 1 - self.equity / self.peak)
        self.day_dd = max(self.day_dd, 1 - self.equity / self.day_peak)
        if self.spec['risk']['approved']:
            self.daily_halted |= self.start - self.equity >= self.spec['risk']['max_daily_loss'] - 1e-9
            self.halted |= self.peak - self.equity >= self.spec['risk']['max_drawdown'] - 1e-9
        self.curve.append({'at': now.isoformat(), 'equity': self.equity})

    def summary(self, *, private=False):
        net = self.equity - self.start
        if not self.open and not math.isclose(net, sum(t['net_pnl'] for t in self.trades), abs_tol=1e-6):
            raise ValueError('quote paper cash flows failed reconciliation')
        result = {
            'starting_equity': self.start, 'ending_equity': self.equity, 'settled_cash': self.settled,
            'unsettled_proceeds': self.pending, 'net_pnl': net,
            'daily_return': net / self.start if self.start > 0 else None,
            'cumulative_net_pnl': self.equity - self.capital, 'equity_peak': self.peak,
            'max_drawdown': self.max_dd, 'max_drawdown_dollars': self.max_dd_dollars,
            'day_drawdown': self.day_dd, 'fees': self.fee_total, 'extra_slippage_paid': self.slip_total,
            'funded_entries': self.funded, 'closed_quote_validated_trades': len(self.trades),
            'cumulative_closed_trades': self.prior.get('cumulative_closed_trades', 0) + len(self.trades),
            'candidates': self.candidate_count, 'missed_entries': self.missed,
            'open_positions': len(self.open), 'unresolved': bool(self.open), 'quote_gap_polls': self.quote_gaps,
            'daily_loss_halted': self.daily_halted, 'drawdown_halted': self.halted,
        }
        if private:
            result.update(trades=self.trades, fills=self.fills, decisions=self.decisions, equity_curve=self.curve)
        return result
