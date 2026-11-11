"""Frozen prospective trial specification, state validation and review gates."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.stats import bootstrap

from . import config as C
from .calendar_nyse import NY, is_full_session, prev_full_sessions
from .option_quotes import canonical, digest

SOURCE_FILES = ('option_quotes.py', 'trial_engine.py', 'quote_trial.py', 'quote_collector.py',
                'cash_catalog.py', 'cash_shadow.py', 'calendar_nyse.py', 'config.py',
                'features.py', 'signals.py', 'engine.py', 'pricing.py',
                'data.py', 'history.py', 'position.py', 'replay.py')
ARMS = ('candidate', 'original_p1a_control')


def source_hashes():
    import hashlib
    root = Path(__file__).parent
    values = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    values.update({name: hashlib.sha256((root.parent / name).read_bytes()).hexdigest()
                   for name in ('requirements.txt', 'requirements-quotes.txt')})
    return values


def proposed_spec(now=None):
    now = now or datetime.now(timezone.utc)
    first = now.astimezone(NY).date() + timedelta(days=1)
    while not is_full_session(first):
        first += timedelta(days=1)
    scenarios = {f'slip_{s:.2f}': {'extra_slippage': s, 'entry_delay_seconds': 0, 'miss_first_entry': False}
                 for s in (0, .01, .03, .05)}
    scenarios.update({f'delay_{n}s': {'extra_slippage': .01, 'entry_delay_seconds': n, 'miss_first_entry': False}
                      for n in (60, 120)})
    scenarios['miss_first_entry'] = {'extra_slippage': .01, 'entry_delay_seconds': 0, 'miss_first_entry': True}
    return {
        'schema_version': 1, 'trial_id': 'cash_quotes_p1a_v1', 'frozen_at': now.isoformat(),
        'operating_mode': 'observation_only',
        'not_before_session': str(first), 'source_sha256': source_hashes(),
        'capital': 1500.0, 'slot_fraction': .25, 'minimum_contracts': 2, 'even_lots': True,
        'slots': 3, 'daily_entry_limit': 4, 'tickers': list(C.TICKERS),
        'arms': {'candidate': {'p1a_max_rvol': 1.0, 'p1a_max_retrace': 1.0},
                 'original_p1a_control': {'p1a_max_rvol': 1.25, 'p1a_max_retrace': .75}},
        'benchmark_cash_daily_return': 0.0,
        'provider': 'tradier_realtime', 'feed': 'opra', 'size_units_confirmed': False, 'poll_seconds': 5,
        'quote_max_age_seconds': 15, 'max_spread_fraction': .20,
        'min_quote_poll_coverage': .95,
        'signal_max_lag_seconds': 30, 'entry_quote_timeout_seconds': 60,
        'hold_minutes': 50, 'stop_fraction': .26, 'tp1_fraction': .90,
        'floor_fraction': .60, 'trail_fraction': .72, 'management': 'minute_close_quote_decisions',
        'session_minimum_minutes': 90, 'session_maximum_minutes': 120,
        'required_sessions': 60, 'required_closed_trades': 100,
        'review_session_counts': [60, 90, 120],
        'primary_scenario': 'slip_0.01', 'scenarios': scenarios,
        'risk': {'approved': False, 'max_trade_risk': None, 'max_daily_loss': None, 'max_drawdown': None},
        'proposed_risk': {'max_trade_risk': 30.0, 'max_daily_loss': 75.0, 'max_drawdown': 150.0},
        'inference': {'block_sessions': 5, 'confidence_level': 1 - .05 / 6, 'resamples': 9999, 'seed': 731062,
                      'method': 'basic', 'comparisons': ['candidate_vs_cash', 'candidate_vs_original_p1a_control']},
        'automatic_live_promotion': False,
    }


def validate_spec(spec, *, verify_source=True):
    import re
    from .cash_catalog import timestamp
    if spec['schema_version'] != 1 or not re.fullmatch('[a-zA-Z0-9_-]{1,60}', spec['trial_id']):
        raise ValueError('invalid frozen trial identity')
    frozen = timestamp(spec['frozen_at'])
    if date.fromisoformat(spec['not_before_session']) <= frozen.astimezone(NY).date():
        raise ValueError('prospective trial must begin after its freeze date')
    if not is_full_session(date.fromisoformat(spec['not_before_session'])):
        raise ValueError('prospective trial must start on a full trading session')
    if verify_source and spec['source_sha256'] != source_hashes():
        raise ValueError('frozen trial source changed; prepare a new trial before collecting evidence')
    if (set(spec['arms']) != set(ARMS) or spec['capital'] != 1500 or spec['slot_fraction'] != .25
            or spec['minimum_contracts'] != 2 or spec['even_lots'] is not True
            or spec['automatic_live_promotion'] is not False or spec['provider'] not in ('alpaca_opra', 'tradier_realtime')
            or spec['feed'] != 'opra'):
        raise ValueError('unsupported trial account, feed or activation settings')
    risk = spec['risk']
    if spec['operating_mode'] not in ('observation_only', 'paper_evaluation'):
        raise ValueError('invalid quote trial operating mode')
    if type(spec['size_units_confirmed']) is not bool:
        raise ValueError('quote size unit confirmation must be explicit')
    if type(risk['approved']) is not bool:
        raise ValueError('risk approval must be explicit')
    if risk['approved']:
        values = [risk[k] for k in ('max_trade_risk', 'max_daily_loss', 'max_drawdown')]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('approved risk limits require positive dollar amounts')
        if not values[0] <= min(spec['capital'] * spec['slot_fraction'], values[1]) <= values[2] <= spec['capital']:
            raise ValueError('risk limits exceed account constraints')
    if not spec['required_sessions'] >= 60 or not spec['required_closed_trades'] >= 100:
        raise ValueError('review checkpoint cannot be reduced in this trial')
    if spec['primary_scenario'] not in spec['scenarios']:
        raise ValueError('missing primary cost scenario')
    for scenario in spec['scenarios'].values():
        if (scenario['extra_slippage'] < 0 or scenario['entry_delay_seconds'] < 0
                or type(scenario['miss_first_entry']) is not bool):
            raise ValueError('invalid execution scenario')
    canonical(spec)
    return digest(spec)


def load_spec(path):
    spec = json.loads(Path(path).read_text())
    validate_spec(spec)
    return spec


def previous_report(directory, session, spec):
    root = Path(directory) / spec['trial_id']
    previous = None
    for path in sorted(root.glob('????-??-??.json'), reverse=True):
        if path.stem >= session:
            continue
        payload = json.loads(path.read_text())
        if payload.get('date') != path.stem:
            raise ValueError('trial report date/filename mismatch')
        previous = payload
        break
    if previous is None:
        return None
    fingerprint = validate_spec(spec)
    cursor = previous
    while True:
        if cursor.get('trial_digest') != fingerprint:
            raise ValueError('trial settings changed within the same trial ID')
        if cursor.get('synthetic'):
            raise ValueError('synthetic test reports cannot enter a prospective trial')
        for arm in ARMS:
            if set(cursor['accounts'][arm]) != set(spec['scenarios']):
                raise ValueError('trial report has incomplete account coverage')
            for account in cursor['accounts'][arm].values():
                if account['unresolved'] or account['open_positions']:
                    raise ValueError('unresolved paper positions must be reconciled before a new session')
                values = [account[k] for k in ('ending_equity', 'settled_cash', 'equity_peak', 'cumulative_net_pnl')]
                if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                    raise ValueError('trial report contains invalid balances')
                if account['settled_cash'] < 0 or account['equity_peak'] < account['ending_equity']:
                    raise ValueError('invalid settled cash or high-water mark')
                if not math.isclose(account['settled_cash'] + sum(p['amount'] for p in account['unsettled_proceeds']),
                                    account['ending_equity'], abs_tol=1e-6):
                    raise ValueError('trial settlement balances do not reconcile')
                if not math.isclose(account['ending_equity'] - spec['capital'], account['cumulative_net_pnl'], abs_tol=1e-6):
                    raise ValueError('trial cumulative equity does not reconcile')
        parent = cursor['parent']
        if parent is None:
            validate_report(cursor, spec)
            break
        if parent['date'] >= cursor['date']:
            raise ValueError('trial report parent is not strictly earlier')
        older = json.loads((root / f"{parent['date']}.json").read_text())
        if older.get('date') != parent['date'] or digest(older) != parent['digest']:
            raise ValueError('trial ancestor changed; replay dependent sessions')
        validate_report(cursor, spec, older)
        cursor = older
    return previous


def scheduled_sessions(first, last):
    current, sessions = first, []
    while current <= last:
        if is_full_session(current):
            sessions.append(str(current))
        current += timedelta(days=1)
    return sessions


def validate_report(report, spec, previous=None):
    if report.get('trial_digest') != validate_spec(spec) or report.get('synthetic'):
        raise ValueError('invalid report in frozen trial')
    if set(report['accounts']) != set(ARMS):
        raise ValueError('trial report has a different control set')
    if report['date'] < spec['not_before_session'] or not is_full_session(date.fromisoformat(report['date'])):
        raise ValueError('trial report is outside the frozen prospective period')
    for arm in ARMS:
        if set(report['accounts'][arm]) != set(spec['scenarios']):
            raise ValueError('trial report has incomplete scenario coverage')
        for name, account in report['accounts'][arm].items():
            keys = ('starting_equity', 'ending_equity', 'settled_cash', 'net_pnl', 'daily_return',
                    'cumulative_net_pnl', 'equity_peak')
            if any(type(account[k]) not in (int, float) or not math.isfinite(account[k]) for k in keys):
                raise ValueError('trial report has invalid account numbers')
            if account['settled_cash'] < 0 or account['equity_peak'] < account['ending_equity']:
                raise ValueError('trial report has invalid cash or equity peak')
            start = previous['accounts'][arm][name]['ending_equity'] if previous else spec['capital']
            if not math.isclose(account['starting_equity'], start, abs_tol=1e-6):
                raise ValueError('trial account starting balance does not match its parent')
            if not math.isclose(account['ending_equity'] - start, account['net_pnl'], abs_tol=1e-6):
                raise ValueError('trial daily PnL does not reconcile')
            if start <= 0 or not math.isclose(account['daily_return'], account['net_pnl'] / start, abs_tol=1e-10):
                raise ValueError('trial daily return does not reconcile')
            if not math.isclose(account['ending_equity'] - spec['capital'], account['cumulative_net_pnl'], abs_tol=1e-6):
                raise ValueError('trial cumulative PnL does not reconcile')
            if not account['unresolved']:
                pending = account['unsettled_proceeds']
                if any(type(p['amount']) not in (int, float) or not math.isfinite(p['amount']) or p['amount'] < 0
                       or date.fromisoformat(p['settles_on']) <= date.fromisoformat(report['date']) for p in pending):
                    raise ValueError('invalid trial settlement queue')
                if not math.isclose(account['settled_cash'] + sum(p['amount'] for p in pending), account['ending_equity'], abs_tol=1e-6):
                    raise ValueError('trial settled/unsettled balances do not reconcile')
            count = account['closed_quote_validated_trades']
            funded = account['funded_entries']
            if (type(count) is not int or type(funded) is not int or count < 0
                    or not count <= funded <= spec['daily_entry_limit']):
                raise ValueError('invalid quote-validated trade count')
            prior_count = previous['accounts'][arm][name]['cumulative_closed_trades'] if previous else 0
            if account['cumulative_closed_trades'] != prior_count + count:
                raise ValueError('trial trade count does not match its parent')


def confidence_interval(daily, spec):
    settings = spec['inference']
    size = settings['block_sessions']
    used = len(daily) // size * size
    if used < 2 * size:
        return {'lower': None, 'upper': None, 'blocks': used // size, 'sessions_used': used}
    blocks = np.array(daily[:used], dtype=float).reshape(-1, size).mean(axis=1)
    if not np.isfinite(blocks).all():
        raise ValueError('non-finite trial returns')
    if np.all(blocks == blocks[0]):
        low = high = float(blocks[0])
    else:
        result = bootstrap((blocks,), np.mean, confidence_level=settings['confidence_level'],
                           n_resamples=settings['resamples'], method=settings['method'],
                           rng=np.random.default_rng(settings['seed']))
        low, high = map(float, result.confidence_interval)
    return {'lower': low, 'upper': high, 'blocks': len(blocks), 'sessions_used': used,
            'sessions_pending_next_full_block': len(daily) - used,
            'confidence_level': settings['confidence_level'],
            'interpretation': 'Two comparisons at three fixed review looks use Bonferroni-adjusted 95% familywise intervals; 5-session blocks approximate serial dependence.'}


def progress(directory, spec, *, through=None):
    fingerprint = validate_spec(spec)
    root = Path(directory) / spec['trial_id']
    reports = []
    for path in sorted(root.glob('????-??-??.json')):
        if through and path.stem > through:
            continue
        r = json.loads(path.read_text())
        if r.get('date') != path.stem or r.get('trial_digest') != fingerprint or r.get('synthetic'):
            raise ValueError('invalid report in frozen trial')
        reports.append(r)
    for previous, current in zip(reports, reports[1:]):
        if current.get('parent') != {'date': previous['date'], 'digest': digest(previous)}:
            raise ValueError('trial report history is not one intact chronological chain')
    if reports and reports[0].get('parent') is not None:
        raise ValueError('trial report history has a missing origin')
    for index, report in enumerate(reports):
        validate_report(report, spec, reports[index - 1] if index else None)
    eligible = [r for r in reports if r.get('eligible_session')]
    primary = spec['primary_scenario']
    reviewed_n = max((n for n in spec['review_session_counts'] if n <= len(eligible)), default=0)
    reviewed = eligible[:reviewed_n]
    candidate = [r['accounts']['candidate'][primary]['daily_return'] for r in reviewed]
    difference = [r['accounts']['candidate'][primary]['daily_return'] -
                  r['accounts']['original_p1a_control'][primary]['daily_return'] for r in reviewed]
    trades = sum(r['accounts']['candidate'][primary]['closed_quote_validated_trades'] for r in eligible)
    reviewed_trades = sum(r['accounts']['candidate'][primary]['closed_quote_validated_trades'] for r in reviewed)
    c_ci, d_ci = confidence_interval(candidate, spec), confidence_interval(difference, spec)
    expected = scheduled_sessions(date.fromisoformat(spec['not_before_session']),
                                  date.fromisoformat(through or reports[-1]['date'])) if reports else []
    recorded = {r['date'] for r in reports}
    missing = [d for d in expected if d not in recorded]
    latest = reports[-1] if reports else None
    review_snapshot = reviewed[-1] if reviewed else None
    risk_ok = bool(latest) and spec['risk']['approved'] and all(
        not a['unresolved'] and not a['drawdown_halted']
        and a['day_max_loss_dollars'] <= spec['risk']['max_daily_loss'] + 1e-6
        and a['max_drawdown_dollars'] <= spec['risk']['max_drawdown'] + 1e-6
        for r in reports for accounts in r['accounts'].values() for a in accounts.values())
    stress_ok = bool(review_snapshot) and all(
        (review_snapshot['accounts']['candidate'][name]['cumulative_net_pnl'] >= 0 if scenario['miss_first_entry']
         else review_snapshot['accounts']['candidate'][name]['cumulative_net_pnl'] > 0)
        for name, scenario in spec['scenarios'].items())
    gates = {
        'paper_evaluation_activated': spec['operating_mode'] == 'paper_evaluation',
        'risk_limits_approved': spec['risk']['approved'],
        'quote_size_units_confirmed': spec['provider'] == 'alpaca_opra' or spec['size_units_confirmed'],
        'minimum_sessions': reviewed_n >= spec['required_sessions'],
        'minimum_funded_trades': reviewed_trades >= spec['required_closed_trades'],
        'complete_calendar': not missing,
        'all_reports_quote_eligible': bool(reports) and all(r.get('eligible_session') for r in reports),
        'positive_net_expectancy': c_ci['lower'] is not None and c_ci['lower'] > 0,
        'improvement_over_original_p1a_control': d_ci['lower'] is not None and d_ci['lower'] > 0,
        'risk_limits_respected': risk_ok,
        'predeclared_execution_stresses_pass': stress_ok,
    }
    return {'trial_id': spec['trial_id'], 'trial_digest': fingerprint,
            'status': 'review_checkpoint_passed' if all(gates.values()) else 'not_ready',
            'eligible_sessions': len(eligible), 'recorded_sessions': len(reports),
            'review_sessions_used': reviewed_n, 'review_closed_trades_used': reviewed_trades,
            'next_review_session_count': next((n for n in spec['review_session_counts'] if n > len(eligible)), None),
            'required_sessions': spec['required_sessions'], 'closed_quote_validated_trades': trades,
            'required_closed_trades': spec['required_closed_trades'], 'missing_sessions': missing,
            'candidate_vs_cash_interval': c_ci, 'candidate_vs_control_interval': d_ci,
            'gates': gates, 'automatic_live_promotion': False,
            'limitations': ['Quote-sized paper fills are simulations, not broker fill confirmations.',
                            'The 60-session/100-trade target is a review checkpoint; uncertainty can require more data.',
                            'Missing, late, stale, indicative or synthetic evidence never counts as prospective qualified trades.',
                            'All selected sessions and failed executions remain visible; reports are not picked for profitability.']}


def daily_attachment(ledger_dir, session):
    """Public aggregate status only; no quote credentials or decryption needed."""
    path = Path(__file__).resolve().parent.parent / 'trial/cash_quote_trial.json'
    spec = load_spec(path)
    result = progress(Path(ledger_dir) / 'quote_trials', spec, through=session)
    daypath = Path(ledger_dir) / 'quote_trials' / spec['trial_id'] / f'{session}.json'
    observation_path = Path(ledger_dir) / 'quote_observations' / digest(spec)[:16] / f'{session}.json'
    return {'date': session, 'progress': result,
            'daily': json.loads(daypath.read_text()) if daypath.exists() else None,
            'observation_only': json.loads(observation_path.read_text()) if observation_path.exists() else None,
            'valuation_basis': 'Prospective bid/ask-sized paper simulation, separate from the legacy theoretical-price shadows.'}
