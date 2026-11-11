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
                'features.py', 'signals.py', 'engine.py', 'pricing.py')
ARMS = ('candidate', 'original_p1a_control')


def source_hashes():
    import hashlib
    root = Path(__file__).parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


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
        'primary_scenario': 'slip_0.01', 'scenarios': scenarios,
        'risk': {'approved': False, 'max_trade_risk': None, 'max_daily_loss': None, 'max_drawdown': None},
        'proposed_risk': {'max_trade_risk': 30.0, 'max_daily_loss': 75.0, 'max_drawdown': 150.0},
        'inference': {'block_sessions': 5, 'confidence_level': .975, 'resamples': 9999, 'seed': 731062,
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
            break
        if parent['date'] >= cursor['date']:
            raise ValueError('trial report parent is not strictly earlier')
        older = json.loads((root / f"{parent['date']}.json").read_text())
        if older.get('date') != parent['date'] or digest(older) != parent['digest']:
            raise ValueError('trial ancestor changed; replay dependent sessions')
        cursor = older
    return previous


def scheduled_sessions(first, last):
    current, sessions = first, []
    while current <= last:
        if is_full_session(current):
            sessions.append(str(current))
        current += timedelta(days=1)
    return sessions


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
            'interpretation': 'Two predeclared comparisons use Bonferroni-adjusted 95% familywise intervals; 5-session blocks approximate serial dependence.'}


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
    eligible = [r for r in reports if r.get('eligible_session')]
    primary = spec['primary_scenario']
    candidate = [r['accounts']['candidate'][primary]['daily_return'] for r in eligible]
    difference = [r['accounts']['candidate'][primary]['daily_return'] -
                  r['accounts']['original_p1a_control'][primary]['daily_return'] for r in eligible]
    trades = sum(r['accounts']['candidate'][primary]['closed_quote_validated_trades'] for r in eligible)
    c_ci, d_ci = confidence_interval(candidate, spec), confidence_interval(difference, spec)
    expected = scheduled_sessions(date.fromisoformat(spec['not_before_session']),
                                  date.fromisoformat(through or reports[-1]['date'])) if reports else []
    recorded = {r['date'] for r in reports}
    missing = [d for d in expected if d not in recorded]
    latest = reports[-1] if reports else None
    risk_ok = bool(latest) and all(not a['unresolved'] and not a['drawdown_halted'] and not a['daily_loss_halted']
                                  for r in reports for accounts in r['accounts'].values() for a in accounts.values())
    stress_ok = bool(latest) and all(latest['accounts']['candidate'][scenario]['cumulative_net_pnl'] > 0
                                   for scenario in spec['scenarios'])
    gates = {
        'risk_limits_approved': spec['risk']['approved'],
        'minimum_sessions': len(eligible) >= spec['required_sessions'],
        'minimum_funded_trades': trades >= spec['required_closed_trades'],
        'complete_calendar': not missing,
        'all_reports_quote_eligible': bool(reports) and all(r.get('eligible_session') for r in reports),
        'positive_net_expectancy': c_ci['lower'] is not None and c_ci['lower'] > 0,
        'improvement_over_original_p1a_control': d_ci['lower'] is not None and d_ci['lower'] > 0,
        'risk_limits_respected': risk_ok,
        'predeclared_execution_stresses_positive': stress_ok,
    }
    return {'trial_id': spec['trial_id'], 'trial_digest': fingerprint,
            'status': 'review_checkpoint_passed' if all(gates.values()) else 'not_ready',
            'eligible_sessions': len(eligible), 'recorded_sessions': len(reports),
            'required_sessions': spec['required_sessions'], 'closed_quote_validated_trades': trades,
            'required_closed_trades': spec['required_closed_trades'], 'missing_sessions': missing,
            'candidate_vs_cash_interval': c_ci, 'candidate_vs_control_interval': d_ci,
            'gates': gates, 'automatic_live_promotion': False,
            'limitations': ['Quote-sized paper fills are simulations, not broker fill confirmations.',
                            'The 60-session/100-trade target is a review checkpoint; uncertainty can require more data.',
                            'Missing, late, stale, indicative or synthetic evidence never counts as prospective qualified trades.',
                            'All selected sessions and failed executions remain visible; reports are not picked for profitability.']}
