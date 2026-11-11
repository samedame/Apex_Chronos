"""Independent P1A-only cash-account shadow. Never sends alerts or orders.

Contracts must appear in a causally observed catalog. Option prices are modeled,
not historical quotes, and missing listing history never falls back to a guess.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np

from . import cash_catalog as catalogs, config as C
from .calendar_nyse import HOLIDAYS, LAST_COVERED, NY, is_full_session, prev_full_sessions, session_open
from .features import compute_features
from .pricing import bs_option_price_and_delta, half_spread_for
from .replay import run_session

MODEL_VERSION = 'cash_listed_p1a_v1'
SLIPPAGE = (0.0, 0.01, 0.03, 0.05)
# DTC is open without settlement services on these bank holidays. Early-close
# trading sessions remain settlement days. Annual coverage is explicit.
SETTLEMENT_BANK_HOLIDAYS = {date(2026, 10, 12), date(2026, 11, 11),
                            date(2027, 10, 11), date(2027, 11, 11)}
FEE_SOURCE = 'https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/'


def next_settlement_day(session):
    if not date(2026, 1, 1) <= session <= LAST_COVERED:
        raise ValueError('cash settlement calendar supports 2026-2027 only')
    current = session + timedelta(days=1)
    while current.weekday() >= 5 or current in HOLIDAYS or current in SETTLEMENT_BANK_HOLIDAYS:
        current += timedelta(days=1)
    if current > LAST_COVERED:
        raise ValueError('cash settlement calendar needs an annual update')
    return current


def fees(quantity, price, session, *, sell=False):
    if quantity < 0 or int(quantity) != quantity or not math.isfinite(price) or price < 0:
        raise ValueError('fees require a nonnegative integer quantity and finite price')
    if not date(2026, 1, 1) <= session <= LAST_COVERED:
        raise ValueError('cash fee model supports 2026-2027 only')
    orf_occ = quantity * 0.04
    raw_cat = quantity * 0.0003
    cat = 0.0 if raw_cat < 0.01 else math.floor(raw_cat * 100 + 0.5) / 100
    principal = quantity * price * 100 if sell else 0.0
    sec_rate = 20.60 if session >= date(2026, 4, 4) else 0.0
    sec = math.ceil(max(0.0, principal * sec_rate / 1_000_000) * 100 - 1e-12) / 100
    paused = date(2026, 10, 1) <= session <= date(2026, 12, 31)
    raw_taf = quantity * 0.00329 if sell and not paused else 0.0
    taf = min(9.79, math.floor(raw_taf * 100 + 0.5) / 100)
    return orf_occ + cat + sec + taf


def years_to_expiry(session, minute, expiry):
    now = session_open(session) + timedelta(minutes=minute + 1)
    end = datetime.combine(expiry, time(16, 0), tzinfo=NY)
    return max((end - now).total_seconds() / (365 * 86400), 1 / (365 * 86400))


def choose_contract(contracts, session, minute, ticker, direction, spot, iv):
    right = 'C' if direction == 1 else 'P'
    eligible = [c for c in contracts if c['ticker'] == ticker and c['right'] == right
                and session <= date.fromisoformat(c['expiry']) <= session + timedelta(days=catalogs.MAX_EXPIRY_DAYS)]
    if not eligible:
        return None
    expiry = min(c['expiry'] for c in eligible)
    eligible = [c for c in eligible if c['expiry'] == expiry]
    strikes = np.array([c['strike'] for c in eligible])
    _, delta = bs_option_price_and_delta(spot, strikes,
                                         years_to_expiry(session, minute, date.fromisoformat(expiry)),
                                         C.RISK_FREE, iv, right == 'C')
    index = min(range(len(eligible)), key=lambda i: (abs(abs(float(delta[i])) - 0.45),
                                                    abs(strikes[i] - spot), eligible[i]['symbol']))
    return {**eligible[index], 'selection_delta_model': float(delta[index])}


def model_prices(contract, session, features):
    expiry = date.fromisoformat(contract['expiry'])
    years = np.array([years_to_expiry(session, m, expiry) for m in range(features.n)])
    mid, _ = bs_option_price_and_delta(features.C, contract['strike'], years, C.RISK_FREE,
                                       features.iv, contract['right'] == 'C')
    hs = half_spread_for(contract['ticker'])
    ask = np.ceil((mid * (1 + hs) + 0.01) * 100 - 1e-9) / 100
    bid = np.floor(np.maximum(mid * (1 - hs) - 0.01, 0) * 100 + 1e-9) / 100
    return ask, bid


class CashAccount:
    def __init__(self, session, capital, allocation, slippage, prior=None):
        self.session, self.capital, self.allocation, self.slippage = session, capital, allocation, slippage
        self.prior = prior or {}
        self.settled = float(self.prior.get('settled_cash_end', capital))
        self.pending = [dict(p) for p in self.prior.get('unsettled_proceeds', [])]
        matured = [p for p in self.pending if date.fromisoformat(p['settles_on']) <= session]
        self.settled += sum(p['amount'] for p in matured)
        self.pending = [p for p in self.pending if date.fromisoformat(p['settles_on']) > session]
        self.start = self.cash
        self.last_equity = self.cash
        self.peak = float(self.prior.get('equity_peak', capital))
        self.max_dd = float(self.prior.get('max_drawdown', 0))
        self.day_peak, self.day_dd = self.start, 0.0
        self.open, self.trades, self.fills, self.decisions, self.curve = [], [], [], [], []
        self.paid_fees = self.paid_slip = self.gross_pnl = 0.0
        self.entries = 0

    @property
    def cash(self):
        return self.settled + sum(p['amount'] for p in self.pending)

    @staticmethod
    def reserve(quantity):
        return quantity * 0.05 + 0.05 if quantity else 0.0

    def enter(self, row, contract, snapshot, ask, bid, minute):
        decision = {**row, 'contract': contract['symbol'], 'catalog_observed_at': snapshot['observed_at'],
                    'catalog_digest': snapshot.get('_digest'), 'slippage': self.slippage}
        if len(self.open) >= C.N_SLOTS:
            self.decisions.append({**decision, 'reason': 'slots_full'})
            return
        if self.entries >= C.PHASE_LIMITS['P1']:
            self.decisions.append({**decision, 'reason': 'daily_entry_limit'})
            return
        price = float(ask[minute]) + self.slippage
        if not math.isfinite(price) or price <= 0 or not math.isfinite(float(bid[minute])):
            raise ValueError('invalid option model mark at cash-shadow entry')
        available = max(0.0, self.settled - sum(self.reserve(p['remaining']) for p in self.open))
        budget = max(0.0, min(self.last_equity * self.allocation, available))
        qty = 2 * math.floor(budget / (price * 100) / 2)
        while qty and qty * price * 100 + fees(qty, price, self.session) + self.reserve(qty) > budget + 1e-9:
            qty -= 2
        if qty < 2:
            self.decisions.append({**decision, 'reason': 'cannot_fund_two_contracts',
                                   'settled_available': available, 'slot_budget': budget,
                                   'model_entry': price, 'two_contract_premium': price * 200,
                                   'two_contract_cost_with_fees_and_reserve': price * 200 + fees(2, price, self.session) + self.reserve(2)})
            return
        charge = fees(qty, price, self.session)
        self.settled -= qty * price * 100 + charge
        self.paid_fees += charge
        self.paid_slip += qty * self.slippage * 100
        self.entries += 1
        self.open.append({'row': row, 'contract': contract, 'snapshot': snapshot,
                          'quantity': qty, 'remaining': qty, 'entry_price': price,
                          'base_entry': float(ask[minute]), 'bid': bid,
                          'fees': charge, 'slip': qty * self.slippage * 100,
                          'base_proceeds': 0.0, 'proceeds': 0.0,
                          'peak_roi': 0.0, 'half_scaled': False, 'stop': -0.26})
        self.fills.append({'kind': 'BUY', 'minute': minute, 'symbol': contract['symbol'],
                           'quantity': qty, 'model_price': price, 'fees': charge,
                           'settled_cash_after': self.settled})
        self.decisions.append({**decision, 'reason': 'selected', 'quantity': qty, 'model_entry': price})

    def sell(self, position, quantity, minute, reason):
        assert quantity > 0 and quantity == int(quantity)
        base_bid = float(position['bid'][minute])
        price = max(0.0, base_bid - self.slippage)
        charge = fees(quantity, price, self.session, sell=True)
        net = quantity * price * 100 - charge
        if net >= 0:
            self.pending.append({'settles_on': str(next_settlement_day(self.session)), 'amount': net})
        else:
            self.settled += net
        if self.settled < -1e-7:
            raise ValueError('cash shadow cannot fund modeled closing fees')
        slip = quantity * (base_bid - price) * 100
        self.paid_fees += charge
        self.paid_slip += slip
        position['remaining'] -= quantity
        position['fees'] += charge
        position['slip'] += slip
        position['base_proceeds'] += quantity * base_bid * 100
        position['proceeds'] += quantity * price * 100
        self.fills.append({'kind': 'SELL', 'minute': minute, 'symbol': position['contract']['symbol'],
                           'quantity': quantity, 'model_price': price, 'fees': charge,
                           'settles_on': str(next_settlement_day(self.session)), 'reason': reason})
        if position['remaining'] == 0:
            principal = position['quantity'] * position['entry_price'] * 100
            gross = position['base_proceeds'] - position['quantity'] * position['base_entry'] * 100
            self.gross_pnl += gross
            self.trades.append({**position['row'], 'contract': position['contract'],
                                'catalog_observed_at': position['snapshot']['observed_at'],
                                'catalog_source': position['snapshot']['source'],
                                'catalog_digest': position['snapshot'].get('_digest'),
                                'm_exit': minute, 'quantity': position['quantity'],
                                'model_entry': position['entry_price'],
                                'price_return': position['proceeds'] / principal - 1,
                                'gross_pnl_before_extra_slippage_and_fees': gross,
                                'fees': position['fees'], 'extra_slippage_paid': position['slip'],
                                'net_pnl': gross - position['fees'] - position['slip'], 'exit_reason': reason})
            self.open.remove(position)

    def mark_and_manage(self, minute, features):
        # Entry selection occurs first, matching the original slot timing.
        for p in list(self.open):
            if minute <= p['row']['m_entry']:
                continue
            f = features[p['row']['ticker']]
            if (not math.isfinite(float(f.C[minute])) or f.C[minute] <= 0 or f.V[minute] <= 0
                    or not math.isfinite(float(p['bid'][minute]))):
                raise ValueError('missing/zero-volume bar while a cash-shadow position is open')
            bid = max(0.0, float(p['bid'][minute]) - self.slippage)
            roi = bid / p['entry_price'] - 1
            p['peak_roi'] = max(p['peak_roi'], roi)
            if not p['half_scaled'] and roi >= 0.90 - 1e-12:
                p['half_scaled'] = True
                p['stop'] = 0.90 * C.AFTER_TP1_FLOOR_FRAC
                self.sell(p, p['quantity'] // 2, minute, 'tp1_close_mark')
            if p['half_scaled']:
                p['stop'] = max(p['stop'], p['peak_roi'] * C.TRAIL_PCT_OF_PEAK)
            if roi <= p['stop'] + 1e-12:
                self.sell(p, p['remaining'], minute, 'profit_trail' if p['half_scaled'] else 'stop_loss')
            elif minute >= min(p['row']['m_entry'] + 50, 354):
                self.sell(p, p['remaining'], minute, 'time_stop')
        equity = self.cash
        for p in self.open:
            bid = max(0.0, float(p['bid'][minute]) - self.slippage)
            equity += p['remaining'] * bid * 100 - fees(p['remaining'], bid, self.session, sell=True)
        self.last_equity = equity
        self.peak, self.day_peak = max(self.peak, equity), max(self.day_peak, equity)
        self.max_dd = max(self.max_dd, 1 - equity / self.peak)
        if self.day_peak > 0:
            self.day_dd = max(self.day_dd, 1 - equity / self.day_peak)
        self.curve.append(equity)

    def summary(self):
        if self.open:
            raise ValueError('cash-only replay ended with open positions')
        net = self.cash - self.start
        if not math.isclose(net, self.gross_pnl - self.paid_fees - self.paid_slip, abs_tol=1e-6):
            raise ValueError('cash-only account cash flows do not reconcile')
        return {
            'starting_equity': self.start, 'ending_equity': self.cash, 'settled_cash_end': self.settled,
            'unsettled_proceeds': self.pending, 'net_pnl': net, 'return': net / self.start if self.start > 0 else None,
            'cumulative_net_pnl': self.cash - self.capital,
            'gross_pnl_before_extra_slippage_and_fees': self.gross_pnl, 'fees': self.paid_fees,
            'extra_slippage_paid': self.paid_slip,
            'cumulative_gross_pnl': self.prior.get('cumulative_gross_pnl', 0) + self.gross_pnl,
            'cumulative_fees': self.prior.get('cumulative_fees', 0) + self.paid_fees,
            'cumulative_slippage': self.prior.get('cumulative_slippage', 0) + self.paid_slip,
            'equity_peak': self.peak, 'max_drawdown': self.max_dd, 'day_max_drawdown': self.day_dd,
            'funded_trades': self.entries,
            'cumulative_funded_trades': self.prior.get('cumulative_funded_trades', 0) + self.entries,
            'open_positions': 0, 'trades': self.trades, 'fills': self.fills,
            'candidate_decisions': self.decisions, 'equity_curve': self.curve,
        }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def settings_for(capital, allocation, tickers):
    if not math.isfinite(capital) or capital <= 0 or not math.isfinite(allocation) or not 0 < allocation <= 0.25:
        raise ValueError('cash shadow needs positive capital and allocation at most 25% per slot')
    source = Path(__file__).parent
    files = ('cash_shadow.py', 'cash_catalog.py', 'calendar_nyse.py', 'engine.py',
             'features.py', 'signals.py', 'config.py', 'pricing.py', 'replay.py')
    return {'model_version': MODEL_VERSION, 'starting_capital': capital, 'slot_fraction': allocation,
            'tickers': list(tickers), 'slippage': list(SLIPPAGE), 'catalog_max_age_hours': 24,
            'max_expiry_calendar_days': 7, 'target_delta_model': 0.45, 'minimum_contracts': 2,
            'even_lots': True, 'management': 'minute_close_marks', 'signals': 'P1A_RVOL1_retrace1',
            'fee_schedule_as_of': '2026-11-11',
            'source_sha256': {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}}


def load_previous(ledger_dir, session, settings):
    if ledger_dir is None:
        return None, 'first_cash_report'
    directory = Path(ledger_dir) / 'shadow'
    previous = None
    for path in sorted(directory.glob('????-??-??.json'), reverse=True):
        if path.stem >= str(session):
            continue
        payload = json.loads(path.read_text())
        if 'cash_only' in payload:
            previous = payload['cash_only']
            if payload.get('date') != path.stem or previous.get('date') != path.stem:
                raise ValueError('cash report date does not match its filename')
            break
    if previous is None:
        return None, 'first_cash_report'
    if previous.get('status') not in ('ok', 'awaiting_catalog', 'partial_catalog'):
        raise ValueError('previous cash report failed; replay it before continuing')
    if previous.get('settings') != settings:
        return None, 'settings_changed'
    if previous['date'] != str(prev_full_sessions(session, 1)[0]):
        raise ValueError('cash report is missing the previous full trading session')
    cursor = previous
    while True:
        if (cursor.get('status') not in ('ok', 'awaiting_catalog', 'partial_catalog')
                or cursor.get('cohort_start') != previous['cohort_start']
                or set(cursor['accounts']) != {f'{s:.2f}' for s in SLIPPAGE}):
            raise ValueError('cash report has invalid status, cohort or scenario coverage')
        for state in cursor['accounts'].values():
            values = [state[k] for k in ('ending_equity', 'settled_cash_end', 'equity_peak', 'max_drawdown',
                                         'cumulative_gross_pnl', 'cumulative_fees', 'cumulative_slippage')]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                raise ValueError('cash report contains invalid account numbers')
            if (state['open_positions'] or state['settled_cash_end'] < 0 or state['equity_peak'] < state['ending_equity']
                    or not 0 <= state['max_drawdown'] <= 1 or state['cumulative_fees'] < 0
                    or state['cumulative_slippage'] < 0):
                raise ValueError('cash report contains invalid open/cash/high-water state')
            pending = state['unsettled_proceeds']
            if any(not math.isfinite(p['amount']) or p['amount'] < 0 or
                   date.fromisoformat(p['settles_on']) <= date.fromisoformat(cursor['date']) for p in pending):
                raise ValueError('cash report contains an invalid unsettled-proceeds queue')
            if not math.isclose(state['settled_cash_end'] + sum(p['amount'] for p in pending), state['ending_equity'], abs_tol=1e-6):
                raise ValueError('cash settlement balances do not reconcile')
            expected = settings['starting_capital'] + state['cumulative_gross_pnl'] - state['cumulative_fees'] - state['cumulative_slippage']
            if not math.isclose(expected, state['ending_equity'], abs_tol=1e-6):
                raise ValueError('cash cumulative PnL does not reconcile')
        parent = cursor['parent']
        if parent is None:
            if cursor['date'] != previous['cohort_start']:
                raise ValueError('cash cohort origin does not match its parent chain')
            break
        if parent['date'] != str(prev_full_sessions(date.fromisoformat(cursor['date']), 1)[0]):
            raise ValueError('cash parent is not the preceding full session')
        envelope = json.loads((directory / f"{parent['date']}.json").read_text())
        older = envelope['cash_only']
        if (envelope.get('date') != parent['date'] or older.get('settings') != settings
                or older.get('date') != parent['date'] or digest(older) != parent['digest']):
            raise ValueError('cash ancestor changed; replay dependent reports')
        cursor = older
    return previous, None


def evaluate(session_date, day, tickers, *, capital=1500.0, slot_fraction=0.25,
             ledger_dir=None, catalog_dir=None, snapshots=None):
    session = date.fromisoformat(session_date)
    if not is_full_session(session):
        raise ValueError('cash shadow only evaluates full trading sessions')
    next_settlement_day(session)  # coverage is checked even when there are no trades
    settings = settings_for(capital, slot_fraction, tickers)
    previous, reset = load_previous(ledger_dir, session, settings)
    snapshots = catalogs.load_snapshots(catalog_dir, session) if snapshots is None else snapshots
    for snapshot in snapshots:
        catalogs.validate_snapshot(snapshot)
    snapshots = sorted(snapshots, key=lambda s: catalogs.timestamp(s['observed_at']))
    fs = {t: compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix) for t, td in day.items()}
    if not fs or min(f.n for f in fs.values()) < 86:
        raise ValueError('cash shadow needs the complete P1A signal and 50-minute management window')
    if len({f.n for f in fs.values()}) != 1:
        raise ValueError('cash shadow needs aligned ticker bar grids')
    # No-slot replay gathers first-trigger P1A candidates before cash capacity or
    # affordability selection. Spread candidates never become cash positions.
    candidate_engine, _ = run_session(session_date, day, tickers, engine_kwargs={
        'n_slots': 0, 'p1a_max_rvol': 1.0, 'p1a_max_retrace': 1.0})
    candidates = [c for c in candidate_engine.candidate_log if c['module'] == C.MODULES['P1A'].name]
    candidates.sort(key=lambda c: (c['m_entry'], -c['score'], tickers.index(c['ticker'])))
    accounts = {f'{s:.2f}': CashAccount(session, capital, slot_fraction, s,
                                     previous['accounts'][f'{s:.2f}'] if previous else None) for s in SLIPPAGE}
    by_minute, coverage = {}, Counter()
    for row in candidates:
        by_minute.setdefault(row['m_entry'], []).append(row)
    for minute in range(max(f.n for f in fs.values())):
        for row in by_minute.get(minute, []):
            f = fs[row['ticker']]
            prefix = slice(0, minute + 1)
            if (not np.isfinite(f.C[prefix]).all() or not np.isfinite(f.iv[prefix]).all()
                    or np.any(f.C[prefix] <= 0) or np.any(f.iv[prefix] <= 0) or np.any(f.V[prefix] <= 0)):
                coverage['invalid_signal_data'] += 1
                for account in accounts.values():
                    account.decisions.append({**row, 'reason': 'invalid_signal_data'})
                continue
            now = (session_open(session) + timedelta(minutes=minute + 1)).astimezone(timezone.utc)
            contracts, snapshot, error = catalogs.contracts_at(snapshots, row['ticker'], now)
            contract = choose_contract(contracts, session, minute, row['ticker'], row['direction'],
                                       float(f.C[minute]), float(f.iv[minute])) if contracts else None
            if contract is None:
                reason = error or 'no_listed_contract_in_expiry_range'
                coverage[reason] += 1
                for account in accounts.values():
                    account.decisions.append({**row, 'reason': reason})
                continue
            coverage['catalog_verified_candidates'] += 1
            ask, bid = model_prices(contract, session, f)
            for account in accounts.values():
                account.enter(row, contract, snapshot, ask, bid, minute)
        for account in accounts.values():
            account.mark_and_manage(minute, fs)
    missing = coverage['missing_causal_catalog'] + coverage['ticker_absent_from_catalog']
    window_end = (session_open(session) + timedelta(minutes=36)).astimezone(timezone.utc)
    catalog_in_window = any(timedelta(0) <= window_end - catalogs.timestamp(s['observed_at'])
                            <= catalogs.MAX_CATALOG_AGE for s in snapshots)
    status = ('awaiting_catalog' if (missing or not catalog_in_window) and not coverage['catalog_verified_candidates']
              else 'partial_catalog' if missing else 'ok')
    return {
        'schema_version': 1, 'date': session_date, 'status': status,
        'settings': settings, 'cohort_start': previous['cohort_start'] if previous else session_date,
        'reset_reason': reset,
        'parent': {'date': previous['date'], 'digest': digest(previous)} if previous else None,
        'coverage': {'qualified_p1a_candidates': len(candidates), **dict(coverage)},
        'accounts': {key: account.summary() for key, account in accounts.items()},
        'valuation_basis': 'Listed contract membership with Black-Scholes model prices; not executable option quotes or fills.',
        'catalog_observation_count': len(snapshots), 'fee_source': FEE_SOURCE,
        'full_market_session': min(f.n for f in fs.values()) == C.SESSION_BARS,
        'limitations': ['Initial account equity is assumed to be fully settled modeled cash, not a verified broker balance.',
                        'Catalog membership does not establish liquidity, broker approval or execution price.',
                        'Missing, future or older-than-24-hour catalog observations are never replaced with synthetic listings.',
                        'Option pricing uses theoretical IV/delta and actual listed expiries/strikes; historical fills remain unverified.',
                        'T+1 settlement excludes market closures and Columbus/Veterans bank holidays; actual broker release is not verified.',
                        'P1A-only signals use minute-close management, not the old intrabar exit simulation.'],
    }
