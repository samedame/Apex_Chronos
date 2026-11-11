"""Offline execution-cost accounts for the end-of-day shadow policies.

No function here posts alerts, fetches market data, or changes signal decisions.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path

import numpy as np

from . import config as C
from .calendar_nyse import prev_full_sessions
from .features import compute_features
from .pricing import Structure, bs_option_price_and_delta, minutes_to_T

MODEL_VERSION = 'robinhood_shadow_accounts_v2'
FEE_AS_OF = '2026-11-10'
FEE_SOURCE = 'https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/'
SLIPPAGE = (0.0, 0.01, 0.03, 0.05)
ONE_CONTRACT_SIZING = ('whole_contracts_keep_runner', 'whole_contracts_sell_odd_at_tp1')
SIZING = ('fractional_equivalent', 'whole_even_contracts', *ONE_CONTRACT_SIZING)
SIZING_RULES = {
    'fractional_equivalent': 'Theoretical exact allocation and half exits; fractional contracts are not executable.',
    'whole_even_contracts': 'Largest affordable even quantity, minimum two; sell exactly half at TP1.',
    'whole_contracts_keep_runner': 'Largest affordable integer quantity, minimum one; sell floor(quantity/2) at TP1 and keep the remainder on the paper runner exit.',
    'whole_contracts_sell_odd_at_tp1': 'Largest affordable integer quantity, minimum one; sell ceil(quantity/2) at TP1 and keep the remainder on the paper runner exit.',
}
POLICIES = ('current', 'skip_p1c_p3', 'p1a_rvol1_retrace1', 'combined_p1a_skip_p1c_p3')

def trade_key(ticker, module, entry):
    return ticker, module, int(entry)

def cent_ceiling(value):
    return math.ceil(max(0.0, value) * 100.0 - 1e-12) / 100.0

def regulatory_fees(quantity, legs, sell_principal_per_contract, session):
    """Estimated non-professional US stock/ETF-option fees on a modeled fill."""
    if session < date(2026, 1, 1):
        raise ValueError('the Robinhood fee model supports dates from 2026-01-01')
    if quantity <= 0:
        return 0.0
    orf_occ = 0.04 * quantity * legs
    sec_rate = 20.60 if session >= date(2026, 4, 4) else 0.0
    sec = cent_ceiling(quantity * sell_principal_per_contract * sec_rate / 1_000_000.0)
    raw_cat = 0.0003 * quantity * legs
    cat = 0.0 if raw_cat < 0.01 else math.floor(raw_cat * 100 + 0.5) / 100
    raw_taf = quantity * 0.00329 if sell_principal_per_contract > 0 else 0.0
    if date(2026, 10, 1) <= session <= date(2026, 12, 31):
        raw_taf = 0.0
    taf = 0.0 if raw_taf < 0.01 else min(9.79, math.floor(raw_taf * 100 + 0.5) / 100)
    return orf_occ + sec + cat + taf

class Account:
    def __init__(self, policy, sizing, slippage, session, capital=1500.0, slot_fraction=0.25, prior=None):
        if sizing not in SIZING:
            raise ValueError(f'unknown shadow sizing mode: {sizing}')
        self.policy = policy
        self.sizing = sizing
        self.slippage = slippage
        self.session = session
        self.capital = capital
        self.slot_fraction = slot_fraction
        self.prior = prior or {}
        self.cash = float(self.prior.get('ending_equity', capital))
        self.starting_equity = self.cash
        self.last_equity = self.cash
        self.peak = float(self.prior.get('equity_peak', capital))
        self.max_drawdown = float(self.prior.get('max_minute_close_drawdown', 0.0))
        self.max_drawdown_dollars = float(self.prior.get('max_drawdown_dollars', 0.0))
        self.day_peak = self.cash
        self.day_drawdown = 0.0
        self.minimum_equity = self.cash
        self.open = {}
        self.fees = 0.0
        self.slippage_paid = 0.0
        self.gross_pnl = 0.0
        self.paper_entries = 0
        self.executed = 0
        self.skipped = 0
        self.one_contract_affordable = 0
        self.two_contracts_affordable = 0
        self.single_contract_entries = 0
        self.odd_contract_entries = 0
        self.closed = []

    def opening_cost(self, qty, meta):
        fees = regulatory_fees(qty, meta['legs'], meta['entry_sell'], self.session)
        slip = qty * meta['legs'] * self.slippage * 100
        return qty * meta['basis'] * 100 + fees + slip

    def quantity(self, budget, meta):
        if self.sizing != 'fractional_equivalent':
            step = 2 if self.sizing == 'whole_even_contracts' else 1
            qty = step * math.floor(budget / (meta['basis'] * 100) / step)
            while qty > 0 and self.opening_cost(qty, meta) > budget + 1e-9:
                qty -= step
            return qty
        low, high = 0.0, budget / (meta['basis'] * 100)
        for _ in range(35):
            mid = (low + high) / 2
            if self.opening_cost(mid, meta) <= budget:
                low = mid
            else:
                high = mid
        return low

    def charge(self, qty, meta, sell_principal):
        fees = regulatory_fees(qty, meta['legs'], sell_principal, self.session)
        slip = qty * meta['legs'] * self.slippage * 100
        self.fees += fees
        self.slippage_paid += slip
        return fees, slip

    def process(self, events, metadata, minute):
        for event in events:
            if event.kind == 'ENTRY':
                key = trade_key(event.ticker, event.module, event.minute)
                meta = metadata[key]
                self.paper_entries += 1
                budget = max(0.0, min(self.last_equity * self.slot_fraction, self.cash))
                self.one_contract_affordable += self.opening_cost(1, meta) <= budget
                self.two_contracts_affordable += self.opening_cost(2, meta) <= budget
                qty = self.quantity(budget, meta)
                if qty <= 1e-12:
                    self.skipped += 1
                    continue
                fees, slip = self.charge(qty, meta, meta['entry_sell'])
                self.cash -= qty * meta['basis'] * 100 + fees + slip
                if self.cash < -1e-7:
                    raise ValueError('shadow account spent more than its available cash')
                self.open[key] = {'quantity': qty, 'remaining': qty, 'tp_gross_paid': 0.0,
                                  'fees': fees, 'slippage': slip, 'tp_seen': False,
                                  'tp_contracts': 0, 'last_sale_minute': None}
                self.executed += 1
                if self.sizing != 'fractional_equivalent':
                    self.single_contract_entries += qty == 1
                    self.odd_contract_entries += qty % 2 == 1
            elif event.kind == 'TP1':
                key = trade_key(event.ticker, event.module, event.data['m_entry'])
                position = self.open.get(key)
                if position is None:
                    continue
                meta = metadata[key]
                position['tp_seen'] = True
                if self.sizing == 'whole_contracts_keep_runner':
                    qty = position['quantity'] // 2
                elif self.sizing == 'whole_contracts_sell_odd_at_tp1':
                    qty = math.ceil(position['quantity'] / 2)
                else:
                    qty = position['quantity'] / 2
                position['tp_contracts'] = qty
                if qty == 0:
                    continue
                gross = qty * meta['basis'] * 100 * (1 + meta['tp'])
                sale = gross / qty if meta['legs'] == 1 else float(meta['exit_sell'][minute])
                fees, slip = self.charge(qty, meta, sale)
                self.cash += gross - fees - slip
                position['remaining'] -= qty
                position['tp_gross_paid'] += gross
                position['fees'] += fees
                position['slippage'] += slip
                position['last_sale_minute'] = minute
            elif event.kind == 'EXIT':
                trade = event.data['trade']
                key = trade_key(event.ticker, event.module, trade['m_entry'])
                position = self.open.pop(key, None)
                if position is None:
                    continue
                meta = metadata[key]
                qty = position['remaining']
                if self.sizing in ONE_CONTRACT_SIZING:
                    # Paper ROI blends equal halves. Apply the recovered runner
                    # return only to the actual integer quantity still held.
                    runner_roi = 2 * trade['roi'] - meta['tp'] if position['tp_seen'] else trade['roi']
                    gross = qty * meta['basis'] * 100 * (1 + runner_roi)
                else:
                    gross = position['quantity'] * meta['basis'] * 100 * (1 + trade['roi']) - position['tp_gross_paid']
                if gross < -1e-7:
                    raise ValueError('negative modeled exit proceeds')
                fees = slip = 0.0
                if qty > 0:
                    sale = max(0.0, gross / qty) if meta['legs'] == 1 else float(meta['exit_sell'][minute])
                    fees, slip = self.charge(qty, meta, sale)
                    self.cash += gross - fees - slip
                    position['last_sale_minute'] = minute
                principal = position['quantity'] * meta['basis'] * 100
                if self.sizing in ONE_CONTRACT_SIZING:
                    gross_profit = position['tp_gross_paid'] + gross - principal
                    trade = {
                        **trade, 'paper_m_exit': trade['m_exit'], 'paper_model_roi': trade['roi'],
                        'paper_exit_reason': trade['exit_reason'],
                        'm_exit': position['last_sale_minute'],
                        'hold_mins': position['last_sale_minute'] - trade['m_entry'],
                        'roi': gross_profit / principal,
                        'exit_reason': 'all_at_tp1' if qty == 0 else trade['exit_reason'],
                        'tp1_contracts': position['tp_contracts'], 'runner_contracts': qty,
                    }
                else:
                    gross_profit = principal * trade['roi']
                self.gross_pnl += gross_profit
                total_fees = position['fees'] + fees
                total_slip = position['slippage'] + slip
                self.closed.append({
                    'policy': self.policy, 'sizing': self.sizing, 'extra_slippage_per_share_leg_side': self.slippage,
                    **trade, 'contracts': position['quantity'], 'gross_dollar_pnl': gross_profit,
                    'robinhood_fee_estimate': total_fees, 'additional_slippage_dollars': total_slip,
                    'net_dollar_pnl': gross_profit - total_fees - total_slip,
                })

        equity = self.cash
        for key, position in self.open.items():
            meta = metadata[key]
            qty = position['remaining']
            if qty == 0:
                continue
            sale = float(meta['unit_value'][minute] * 100) if meta['legs'] == 1 else float(meta['exit_sell'][minute])
            exit_fee = regulatory_fees(qty, meta['legs'], sale, self.session)
            exit_slip = qty * meta['legs'] * self.slippage * 100
            equity += qty * float(meta['unit_value'][minute]) * 100 - exit_fee - exit_slip
        self.last_equity = equity
        self.peak = max(self.peak, equity)
        self.max_drawdown = max(self.max_drawdown, 1 - equity / self.peak)
        self.max_drawdown_dollars = max(self.max_drawdown_dollars, self.peak - equity)
        self.day_peak = max(self.day_peak, equity)
        if self.day_peak > 0:
            self.day_drawdown = max(self.day_drawdown, 1 - equity / self.day_peak)
        self.minimum_equity = min(self.minimum_equity, equity)

    def summary(self):
        if self.open:
            raise ValueError('cost replay ended with open positions')
        net_pnl = self.cash - self.starting_equity
        if not math.isclose(net_pnl, self.gross_pnl - self.fees - self.slippage_paid, abs_tol=1e-6):
            raise ValueError('shadow cash flows do not reconcile')
        return {
            'policy': self.policy, 'sizing': self.sizing,
            'extra_slippage_per_share_leg_side': self.slippage,
            'starting_equity': self.starting_equity, 'ending_equity': self.cash,
            'net_pnl': net_pnl,
            'return': self.cash / self.starting_equity - 1 if self.starting_equity > 0 else None,
            'cumulative_net_pnl': self.cash - self.capital,
            'cumulative_return': self.cash / self.capital - 1,
            'gross_pnl': self.gross_pnl, 'robinhood_fees_estimated': self.fees,
            'additional_slippage_dollars': self.slippage_paid,
            'cumulative_gross_pnl': self.prior.get('cumulative_gross_pnl', 0.0) + self.gross_pnl,
            'cumulative_fees': self.prior.get('cumulative_fees', 0.0) + self.fees,
            'cumulative_slippage': self.prior.get('cumulative_slippage', 0.0) + self.slippage_paid,
            'equity_peak': self.peak,
            'max_minute_close_drawdown': self.max_drawdown,
            'max_drawdown_dollars': self.max_drawdown_dollars,
            'day_max_minute_close_drawdown': self.day_drawdown,
            'day_minimum_equity': self.minimum_equity,
            'paper_trades': self.paper_entries, 'account_trades': self.executed,
            'skipped_for_funding': self.skipped,
            'single_contract_entries': self.single_contract_entries,
            'odd_contract_entries': self.odd_contract_entries,
            'cumulative_paper_trades': self.prior.get('cumulative_paper_trades', 0) + self.paper_entries,
            'cumulative_account_trades': self.prior.get('cumulative_account_trades', 0) + self.executed,
            'cumulative_skipped_for_funding': self.prior.get('cumulative_skipped_for_funding', 0) + self.skipped,
            'one_contract_affordable': int(self.one_contract_affordable),
            'two_contracts_affordable': int(self.two_contracts_affordable),
            'positive_days': int(self.prior.get('positive_days', 0)) + int(net_pnl > 0),
            'open_positions': 0,
            'account_status': 'insolvent' if self.cash <= 0 else 'funded',
            'trades': self.closed,
        }

def metadata_for(event, features):
    d = event.data
    spec = C.MODULES[d['module_key']]
    f = features[event.ticker]
    st = Structure(event.ticker, d['structure'], d['direction'], d['S_entry'], d['iv_entry'],
                   minutes_to_T(event.minute), spec.target_delta)
    minutes = np.arange(f.n)
    values = st.value(f.C, minutes_to_T(minutes), f.iv)
    if st.structure == 'single':
        entry_sale = 0.0
        exit_sale = values * 100
        legs = 1
    else:
        legs = 2
        if st.structure == 'debit_spread':
            entry_strike, exit_strike = st.K2, st.K1
            entry_skew, exit_skew, is_call = 1.01, 1.0, st.is_call
        else:
            entry_strike, exit_strike = st.K1, st.K2
            entry_skew, exit_skew, is_call = 1.0, 1.02, not st.is_call
        price, _ = bs_option_price_and_delta(f.C[event.minute], entry_strike, minutes_to_T(event.minute),
                                             C.RISK_FREE, f.iv[event.minute] * entry_skew, is_call)
        entry_sale = max(0.0, float(price) * 100 * (1 - st.hs))
        prices, _ = bs_option_price_and_delta(f.C, exit_strike, minutes_to_T(minutes),
                                              C.RISK_FREE, f.iv * exit_skew, is_call)
        exit_sale = np.maximum(prices * 100 * (1 - st.hs), 0.0)
    return {'basis': d['cost_basis'], 'tp': spec.tp1_pct, 'legs': legs,
            'unit_value': values, 'entry_sell': entry_sale, 'exit_sell': exit_sale}


def scenario_key(sizing, slippage):
    return f'{sizing}|{slippage:.2f}'


def contract_sizing_comparison(results):
    """Compact daily/cumulative account comparison, without copying trade arrays."""
    fields = (
        'net_pnl', 'cumulative_net_pnl', 'ending_equity',
        'day_max_minute_close_drawdown', 'max_minute_close_drawdown',
        'robinhood_fees_estimated', 'cumulative_fees',
        'additional_slippage_dollars', 'cumulative_slippage',
        'paper_trades', 'account_trades', 'skipped_for_funding', 'single_contract_entries',
        'cumulative_paper_trades', 'cumulative_account_trades', 'cumulative_skipped_for_funding',
    )
    differences = ('net_pnl', 'cumulative_net_pnl', 'day_max_minute_close_drawdown',
                   'max_minute_close_drawdown', 'account_trades', 'skipped_for_funding')
    comparison = {}
    for policy, scenarios in results.items():
        comparison[policy] = {}
        for slip in SLIPPAGE:
            accounts = {
                sizing: {field: scenarios[scenario_key(sizing, slip)][field] for field in fields}
                for sizing in ('whole_even_contracts', *ONE_CONTRACT_SIZING)
            }
            baseline = accounts['whole_even_contracts']
            comparison[policy][f'{slip:.2f}'] = {
                'baseline_sizing': 'whole_even_contracts', 'accounts': accounts,
                'differences_vs_two_contracts': {
                    sizing: {field: accounts[sizing][field] - baseline[field] for field in differences}
                    for sizing in ONE_CONTRACT_SIZING
                },
            }
    return comparison


def digest(report):
    encoded = json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def settings_for(capital, slot_fraction, tickers):
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError('shadow starting capital must be positive and finite')
    if not math.isfinite(slot_fraction) or not 0 < slot_fraction <= 1:
        raise ValueError('shadow slot fraction must be in (0, 1]')
    source_dir = Path(__file__).parent
    sources = ('engine.py', 'config.py', 'position.py', 'pricing.py', 'features.py',
               'signals.py', 'shadow.py', 'shadow_costs.py')
    return {
        'model_version': MODEL_VERSION, 'starting_capital': float(capital),
        'slot_fraction': float(slot_fraction), 'tickers': list(tickers),
        'sizing_modes': list(SIZING), 'additional_slippage_per_share_leg_side': list(SLIPPAGE),
        'fee_schedule_as_of': FEE_AS_OF,
        'source_sha256': {name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest() for name in sources},
    }


def validate_states(report, settings):
    expected = {scenario_key(sizing, slip) for sizing in SIZING for slip in SLIPPAGE}
    policies = report['policies']
    if set(policies) != set(POLICIES):
        raise ValueError('prior cost report has a different policy set')
    fields = ('ending_equity', 'equity_peak', 'max_minute_close_drawdown', 'max_drawdown_dollars',
              'cumulative_gross_pnl', 'cumulative_fees', 'cumulative_slippage', 'cumulative_net_pnl')
    for policy in POLICIES:
        if set(policies[policy]) != expected:
            raise ValueError('prior cost report has missing or extra scenarios')
        for key, state in policies[policy].items():
            if key != scenario_key(state['sizing'], state['extra_slippage_per_share_leg_side']):
                raise ValueError('prior scenario identity does not match its state')
            if any(type(state[field]) not in (int, float) or not math.isfinite(state[field]) for field in fields):
                raise ValueError('prior account state contains non-finite or invalid numbers')
            if state['open_positions'] != 0 or state['equity_peak'] < max(settings['starting_capital'], state['ending_equity']):
                raise ValueError('prior account is not flat or has an invalid equity peak')
            if any(state[field] < 0 for field in (
                    'cumulative_fees', 'cumulative_slippage', 'max_minute_close_drawdown', 'max_drawdown_dollars')):
                raise ValueError('prior costs or drawdowns are negative')
            cumulative = state['cumulative_gross_pnl'] - state['cumulative_fees'] - state['cumulative_slippage']
            net = state['ending_equity'] - settings['starting_capital']
            if not math.isclose(cumulative, net, abs_tol=1e-5) or not math.isclose(state['cumulative_net_pnl'], net, abs_tol=1e-5):
                raise ValueError('prior cumulative cash flows do not reconcile')


def load_previous(ledger_dir, session, settings):
    """Load a strictly earlier, compatible, intact chain of flat account states."""
    if ledger_dir is None:
        return None, 'first_cost_report'
    directory = Path(ledger_dir) / 'shadow'
    paths = sorted(directory.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json'), reverse=True)
    previous = None
    for path in paths:
        if path.stem >= session.isoformat():
            continue
        payload = json.loads(path.read_text())
        costs = payload.get('execution_costs')
        if costs is not None:
            previous = costs
            if costs.get('date') != path.stem or payload.get('date') != path.stem:
                raise ValueError('prior cost report date does not match its filename')
            break
    if previous is None:
        return None, 'first_cost_report'
    if previous.get('status') != 'ok':
        raise ValueError('previous cost report failed; replay that session before continuing')
    if previous['settings'] != settings:
        return None, 'settings_changed'
    expected_date = prev_full_sessions(session, 1)[0].isoformat()
    if previous['date'] != expected_date:
        raise ValueError(f'missing cost report for previous full session {expected_date}')
    cursor = previous
    while True:
        validate_states(cursor, settings)
        parent = cursor['parent']
        if parent is None:
            if cursor['date'] != previous['cohort_start']:
                raise ValueError('cost report chain does not start at its stated cohort date')
            break
        parent_date = date.fromisoformat(parent['date'])
        if parent_date.isoformat() != prev_full_sessions(date.fromisoformat(cursor['date']), 1)[0].isoformat():
            raise ValueError('cost report parent is not the preceding full session')
        payload = json.loads((directory / f'{parent_date.isoformat()}.json').read_text())
        older = payload['execution_costs']
        if (payload.get('date') != parent['date'] or older.get('date') != parent['date']
                or older.get('status') != 'ok' or older['settings'] != settings
                or older['cohort_start'] != previous['cohort_start'] or digest(older) != parent['digest']):
            raise ValueError('prior cost report changed; replay its dependent sessions')
        cursor = older
    return previous, None


def evaluate(session_date, day, tickers, event_streams, *, capital=1500.0,
             slot_fraction=0.25, ledger_dir=None):
    """Price the four unchanged paper event streams in independent account overlays."""
    session = date.fromisoformat(session_date)
    if session < date(2026, 1, 1):
        raise ValueError('the Robinhood fee model supports dates from 2026-01-01')
    settings = settings_for(capital, slot_fraction, tickers)
    previous, reset_reason = load_previous(ledger_dir, session, settings)
    features = {
        ticker: compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
        for ticker, td in day.items()
    }
    if not features or set(event_streams) != set(POLICIES):
        raise ValueError('cost replay needs session data and all four policy event streams')
    if any(not np.isfinite(f.C).all() or not np.isfinite(f.iv).all() for f in features.values()):
        raise ValueError('cost replay inputs contain non-finite prices or IV')
    results = {}
    for policy in POLICIES:
        states = previous['policies'][policy] if previous else {}
        accounts = {
            scenario_key(sizing, slip): Account(policy, sizing, slip, session, capital, slot_fraction,
                                                states.get(scenario_key(sizing, slip)))
            for sizing in SIZING for slip in SLIPPAGE
        }
        events_by_minute = {}
        for event in event_streams[policy]:
            events_by_minute.setdefault(event.minute, []).append(event)
        metadata = {}
        n_bars = max(f.n for f in features.values())
        if any(minute < 0 or minute >= n_bars for minute in events_by_minute):
            raise ValueError('cost event falls outside the replayed session')
        for minute in range(n_bars):
            events = events_by_minute.get(minute, [])
            for event in events:
                if event.kind == 'ENTRY':
                    metadata[trade_key(event.ticker, event.module, event.minute)] = metadata_for(event, features)
            for account in accounts.values():
                account.process(events, metadata, minute)
        results[policy] = {key: account.summary() for key, account in accounts.items()}
    return {
        'status': 'ok', 'schema_version': 2, 'date': session_date,
        'settings': settings, 'cohort_start': previous['cohort_start'] if previous else session_date,
        'reset_reason': reset_reason,
        'parent': {'date': previous['date'], 'digest': digest(previous)} if previous else None,
        'policies': results, 'universe_used': sorted(day),
        'sizing_rules': SIZING_RULES,
        'contract_sizing_comparison': contract_sizing_comparison(results),
        'fee_source': FEE_SOURCE,
        'fee_model': {
            'commission': 0.0, 'orf_occ_per_contract_leg_side': 0.04,
            'sec_per_million_sold': 20.60 if session >= date(2026, 4, 4) else 0.0,
            'cat_per_contract_leg_side': 0.0003,
            'taf_per_contract_sold': 0.0 if date(2026, 10, 1) <= session <= date(2026, 12, 31) else 0.00329,
        },
        'limitations': [
            'Model prices and estimated fees/slippage, without real option quotes, fills or verified listings.',
            'Fractional-equivalent sizing is theoretical; even whole lots require at least two contracts for half exits.',
            'One-contract modes allow any affordable integer quantity; odd lots require the stated TP1 rounding rule.',
            'Early account exits keep their original paper scheduler slots and do not create replacement trades.',
            'Unfunded account orders retain their paper scheduler slots and do not create replacement alerts.',
            'Drawdowns use minute-close modeled net liquidation including estimated remaining closing costs.',
            'All additional slippage scenarios are beyond the existing model spread assumption.',
        ],
    }
