"""Independent quote-only paper trial. No imports from alert delivery or runner."""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import requests

from . import cash_catalog, cash_shadow, config as C, data, history, quote_trial
from .calendar_nyse import NY, is_full_session, session_open
from .engine import SessionEngine
from .features import compute_features
from .option_quotes import AlpacaOPRA, TradierOPRA, Quote, archive_batch, cipher, digest, load_archive
from .trial_engine import PaperAccount


def now_utc():
    return datetime.now(timezone.utc)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, indent=2, allow_nan=False)
    with NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(encoded + '\n')
            temp.close()
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


class TrialSession:
    def __init__(self, spec, session, *, previous=None, observation_only=False):
        self.spec, self.session = spec, session
        self.fingerprint = quote_trial.validate_spec(spec)
        self.previous = previous
        self.observation_only = observation_only
        self.records, self.quotes = [], {}
        self.accounts = {}
        for arm in quote_trial.ARMS:
            self.accounts[arm] = {name: PaperAccount(spec, session, arm, settings,
                                  previous['accounts'][arm][name] if previous else None,
                                  funding_enabled=not observation_only)
                                  for name, settings in spec['scenarios'].items()}

    def ingest(self, record):
        from .cash_catalog import timestamp
        observed = timestamp(record['observed_at'])
        if observed.astimezone(NY).date().isoformat() != self.session:
            raise ValueError('quote trial record falls outside its session')
        if self.records and (observed < timestamp(self.records[-1]['observed_at']) or record['sequence'] <= self.records[-1]['sequence']):
            raise ValueError('duplicate or out-of-order quote trial record')
        for row in record['candidates']:
            if timestamp(row['observed_at']) > observed:
                raise ValueError('future candidate in quote capture')
            for account in self.accounts[row['arm']].values():
                account.candidate(row)
        for raw in record['quotes']:
            quote = Quote(**raw)
            if cash_catalog.timestamp(quote.observed_at) > observed:
                raise ValueError('future observation in quote capture')
            self.quotes[quote.symbol] = quote
        available = {} if record['errors'] else self.quotes
        for accounts in self.accounts.values():
            for account in accounts.values():
                account.tick(observed, available)
        self.records.append(record)

    def report(self):
        from .cash_catalog import timestamp
        opened = session_open(date.fromisoformat(self.session)).astimezone(timezone.utc)
        active = [r for r in self.records if timestamp(r['observed_at']) >= opened]
        covered = {m for r in self.records for m in r['covered_minutes']}
        errors = sum(bool(r['errors']) for r in self.records)
        gaps = max(((timestamp(b['observed_at']) - timestamp(a['observed_at'])).total_seconds()
                    for a, b in zip(active, active[1:])), default=0)
        first = timestamp(self.records[0]['observed_at']) if self.records else None
        last = timestamp(self.records[-1]['observed_at']) if self.records else None
        full_window = first is not None and first <= opened and last >= opened + timedelta(minutes=self.spec['session_minimum_minutes'])
        synthetic = any(r.get('capture_mode') != 'live_api' for r in self.records)
        account_results = {arm: {name: a.summary() for name, a in scenarios.items()} for arm, scenarios in self.accounts.items()}
        unresolved = any(a['unresolved'] for scenarios in account_results.values() for a in scenarios.values())
        holding_gaps = any(a['quote_gap_polls'] for scenarios in account_results.values() for a in scenarios.values())
        verified_polls = sum(any(q.get('provider') == self.spec['provider'] and Quote(**q).rejection(timestamp(r['observed_at']),
                             max_age_seconds=self.spec['quote_max_age_seconds'],
                             max_spread_fraction=self.spec['max_spread_fraction']) is None for q in r['quotes']) for r in active)
        quote_coverage = verified_polls / len(active) if active else 0.0
        rejections = Counter()
        for r in active:
            for raw in r['quotes']:
                q = Quote(**raw)
                reason = ('provider_mismatch' if q.provider != self.spec['provider'] else
                          q.rejection(timestamp(r['observed_at']), max_age_seconds=self.spec['quote_max_age_seconds'],
                                      max_spread_fraction=self.spec['max_spread_fraction']))
                if reason:
                    rejections[reason] += 1
        valid_window = (full_window and not errors and gaps <= self.spec['quote_max_age_seconds']
                        and set(range(86)).issubset(covered) and not unresolved and not holding_gaps
                        and quote_coverage >= self.spec['min_quote_poll_coverage'])
        in_period = self.session >= self.spec['not_before_session']
        eligible = bool(valid_window and in_period and self.spec['risk']['approved'] and not synthetic and not self.observation_only)
        return {'schema_version': 1, 'date': self.session, 'trial_id': self.spec['trial_id'],
                'trial_digest': self.fingerprint, 'synthetic': synthetic, 'eligible_session': eligible,
                'status': 'observation_only' if self.observation_only else 'complete_quote_session' if eligible else 'incomplete_quote_session',
                'counts_for_trial': eligible,
                'first_observed_at': first.isoformat() if first else None,
                'last_observed_at': last.isoformat() if last else None,
                'parent': {'date': self.previous['date'], 'digest': digest(self.previous)} if self.previous else None,
                'quality': {'full_collection_window': full_window, 'covered_bar_count': len(covered),
                            'error_polls': errors, 'max_poll_gap_seconds': gaps, 'unresolved_positions': unresolved,
                            'holding_quote_gaps': holding_gaps, 'after_trial_start': in_period,
                            'verified_quote_poll_fraction': quote_coverage,
                            'quote_rejections': dict(rejections),
                            'quote_records': sum(len(r['quotes']) for r in self.records)},
                'accounts': account_results,
                'raw_data_storage': 'Authenticated encrypted archives; raw prices/sizes/fills are not published.',
                'live_alerts_changed': False, 'automatic_live_promotion': False}


class Candidates:
    def __init__(self, spec, session, state_dir, http):
        self.spec, self.session, self.state_dir = spec, session, Path(state_dir)
        hist = history.load_history(str(state_dir), spec['tickers'], session, allow_rebuild=False)
        daily = {t: data.fetch_1d(http, t) for t in spec['tickers']}
        self.contexts, excluded = history.build_contexts(hist, daily, spec['tickers'], session)
        if excluded:
            raise ValueError('quote trial lacks complete prior-session baselines')
        self.engines = {arm: SessionEngine(str(session), spec['tickers'], n_slots=0, n_bars=390, **filters)
                        for arm, filters in spec['arms'].items()}
        self.last = -1
        self.reference_symbols = set()
        self.catalogs = cash_catalog.load_snapshots(self.state_dir / 'cash_options', session)

    def update(self, requested_at):
        m_target = min(389, int((requested_at - session_open(self.session)).total_seconds() // 60) - 1)
        if m_target <= self.last or m_target < 0:
            return [], []
        def fetch(ticker):
            with requests.Session() as http:
                return ticker, data.fetch_1m_today(http, ticker)
        with ThreadPoolExecutor(max_workers=4) as pool:
            frames = dict(pool.map(fetch, [*self.spec['tickers'], '^VIX']))
        bars = {t: data.to_grid(frames[t], self.session, t, m_target) for t in self.spec['tickers']}
        if any(not b.ok for b in bars.values()):
            raise ValueError('quote trial underlying session bars are unavailable')
        n = min(b.last_real for b in bars.values()) + 1
        vix, stale = data.align_vix(frames['^VIX'], self.session, n)
        if stale > C.VIX_MAX_STALE_MIN:
            raise ValueError('quote trial underlying volatility feed is stale')
        fs = {t: compute_features(self.contexts[t], b.O[:n], b.H[:n], b.L[:n], b.C[:n], b.V[:n], vix)
              for t, b in bars.items()}
        observed = now_utc()
        if not self.reference_symbols:
            for ticker in self.spec['tickers']:
                contracts, _, _ = cash_catalog.contracts_at(self.catalogs, ticker, observed)
                if contracts:
                    f = fs[ticker]
                    for direction in (1, -1):
                        contract = cash_shadow.choose_contract(contracts, self.session, n - 1, ticker, direction,
                                                               float(f.C[-1]), float(f.iv[-1]))
                        if contract:
                            self.reference_symbols.add(contract['symbol'])
        new, covered = [], list(range(self.last + 1, n))
        for m in covered:
            for arm, engine in self.engines.items():
                before = len(engine.candidate_log)
                engine.step(m, fs)
                for c in engine.candidate_log[before:]:
                    if c['module'] != C.MODULES['P1A'].name:
                        continue
                    signal_at = session_open(self.session) + timedelta(minutes=m + 1)
                    row = {**c, 'arm': arm, 'signal_at': signal_at.astimezone(timezone.utc).isoformat(),
                           'observed_at': observed.isoformat(),
                           'candidate_id': digest([str(self.session), arm, c['ticker'], m])}
                    f = fs[c['ticker']]
                    contracts, snapshot, error = cash_catalog.contracts_at(self.catalogs, c['ticker'], signal_at.astimezone(timezone.utc))
                    if (not np.isfinite(f.C[:m + 1]).all() or np.any(f.C[:m + 1] <= 0)
                            or np.any(f.V[:m + 1] <= 0) or not np.isfinite(f.iv[m]) or f.iv[m] <= 0):
                        row['skip_reason'] = 'invalid_underlying_prefix'
                    elif not contracts:
                        row['skip_reason'] = error
                    else:
                        contract = cash_shadow.choose_contract(contracts, self.session, m, c['ticker'], c['direction'], float(f.C[m]), float(f.iv[m]))
                        if contract is None:
                            row['skip_reason'] = 'no_listed_contract'
                        else:
                            row.update(contract=contract, catalog_digest=snapshot['_digest'],
                                       catalog_observed_at=snapshot['observed_at'])
                    new.append(row)
        self.last = n - 1
        return new, covered


def health(ledger_dir, spec, status):
    result = {'trial_id': spec['trial_id'], 'trial_digest': quote_trial.validate_spec(spec),
              'checked_at': now_utc().isoformat(), 'status': status,
              'eligible_sessions_added': 0, 'quote_validated_trades_added': 0,
              'live_alerts_changed': False}
    save_json(Path(ledger_dir) / 'quote_trials' / 'setup-status.json', result)
    if os.environ.get('APEX_GIT_PUSH') == '1':
        from .state import git_commit_push
        if not git_commit_push([str(Path(ledger_dir) / 'quote_trials')], 'apex-chronos quote trial setup status [skip ci]'):
            raise RuntimeError('quote trial setup status could not be published')
    return result


def collect(spec, state_dir, ledger_dir, *, max_runtime_minutes=140):
    from .state import git_commit_push
    fingerprint = quote_trial.validate_spec(spec)
    started = now_utc()
    session = started.astimezone(NY).date()
    key = os.environ.get('CHRONOS_QUOTE_ARCHIVE_KEY', '')
    missing = []
    access = bool(os.environ.get('TRADIER_API_KEY')) if spec['provider'] == 'tradier_realtime' else bool(os.environ.get('APCA_API_KEY_ID') and os.environ.get('APCA_API_SECRET_KEY'))
    if not access:
        missing.append('quote_credentials')
    if not key:
        missing.append('archive_key')
    if missing:
        return health(ledger_dir, spec, 'awaiting_' + '_and_'.join(missing))
    cipher(key)  # key must validate before a network call or plaintext capture
    opened = session_open(session).astimezone(timezone.utc)
    active_trial = (spec['operating_mode'] == 'paper_evaluation' and spec['risk']['approved']
                    and (spec['provider'] == 'alpaca_opra' or spec['size_units_confirmed']))
    end_minute = spec['session_maximum_minutes'] if active_trial else C.SESSION_BARS
    if (not is_full_session(session) or started < opened - timedelta(minutes=20)
            or started >= opened + timedelta(minutes=end_minute)):
        return health(ledger_dir, spec, 'outside_collection_window')
    if active_trial and str(session) < spec['not_before_session']:
        return health(ledger_dir, spec, 'before_frozen_trial_start')
    report_root = Path(ledger_dir) / 'quote_trials' / spec['trial_id'] if active_trial else Path(ledger_dir) / 'quote_observations' / fingerprint[:16]
    existing_path = report_root / f'{session}.json'
    if existing_path.exists():
        existing = json.loads(existing_path.read_text())
        if existing.get('quality', {}).get('full_collection_window') and existing.get('trial_digest') == fingerprint and not existing.get('synthetic'):
            return {'trial_id': spec['trial_id'], 'status': 'session_already_complete'}
    previous = quote_trial.previous_report(Path(ledger_dir) / 'quote_trials', str(session), spec) if active_trial else None
    replay = TrialSession(spec, str(session), previous=previous, observation_only=not active_trial)
    archive_dir = Path(state_dir) / 'quote_trial_archive' if active_trial else Path(state_dir) / 'quote_observation_archive' / fingerprint[:16]
    old = load_archive(archive_dir, str(session), fingerprint, key)
    watch = set()
    for record in old:
        replay.ingest(record)
        watch.update(c['contract']['symbol'] for c in record['candidates'] if 'contract' in c)
    provider = (TradierOPRA(os.environ.get('TRADIER_API_KEY'), sizes_confirmed=spec['size_units_confirmed'])
                if spec['provider'] == 'tradier_realtime' else AlpacaOPRA.from_env())
    with requests.Session() as http:
        candidates = Candidates(spec, session, state_dir, http)
    # Catch-up never reconstructs old quote fills. Repeated candidate IDs are
    # ignored, and genuinely late new signals expire under the frozen lag rule.
    buffer = []
    flushed_at = now_utc()
    sequence = old[-1]['sequence'] + 1 if old else 0
    last_bucket = None
    minimum_end = opened + timedelta(minutes=spec['session_minimum_minutes'])
    if not active_trial:
        minimum_end = min(opened + timedelta(minutes=C.SESSION_BARS),
                          max(minimum_end, started + timedelta(minutes=spec['session_minimum_minutes'])))
    try:
        while True:
            current = now_utc()
            if current >= opened + timedelta(minutes=end_minute):
                break
            if (current - started).total_seconds() >= max_runtime_minutes * 60:
                break
            new, covered, errors, quotes = [], [], [], []
            bucket = int(current.timestamp() // 60)
            try:
                if current >= opened and current.second >= 6 and bucket != last_bucket:
                    new, covered = candidates.update(current)
                    last_bucket = bucket
                watch.update(c['contract']['symbol'] for c in new if 'contract' in c)
                watch.update(candidates.reference_symbols)
                quotes = [asdict(q) for q in provider.latest(watch)]
            except Exception as error:
                # Only stable error class names enter the public health/quality
                # counters. No provider response, token, header or quote is logged.
                errors = [type(error).__name__]
            observed = now_utc()
            record = {'sequence': sequence, 'observed_at': observed.isoformat(), 'capture_mode': 'live_api',
                      'candidates': new, 'covered_minutes': covered, 'quotes': quotes, 'errors': errors,
                      'requested_symbols': len(watch)}
            replay.ingest(record)
            buffer.append(record)
            sequence += 1
            if (observed - flushed_at).total_seconds() >= 60:
                archive_batch(archive_dir, str(session), fingerprint, buffer, key)
                buffer = []
                flushed_at = observed
            flat = all(not a.open and not a.pending_entries for accounts in replay.accounts.values() for a in accounts.values())
            if observed >= minimum_end and flat:
                break
            time.sleep(spec['poll_seconds'])
    finally:
        archive_batch(archive_dir, str(session), fingerprint, buffer, key)
        report = replay.report()
        # Public report contains aggregate account metrics only. Raw quotes,
        # displayed sizes, exact contracts, fills and candidates stay encrypted.
        path = report_root / f'{session}.json'
        save_json(path, report)
        status = quote_trial.progress(Path(ledger_dir) / 'quote_trials', spec, through=str(session))
        save_json(Path(ledger_dir) / 'quote_trials' / spec['trial_id'] / 'STATUS.json', status)
        if os.environ.get('APEX_GIT_PUSH') == '1':
            publish_paths = [str(archive_dir), str(Path(ledger_dir) / 'quote_trials')]
            if not active_trial:
                publish_paths.append(str(Path(ledger_dir) / 'quote_observations'))
            if not git_commit_push(publish_paths,
                                   f'apex-chronos {session} quote paper trial [skip ci]'):
                raise RuntimeError('quote trial archive/report could not be published')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'collect', 'status'))
    parser.add_argument('--spec', default='trial/cash_quote_trial.json')
    parser.add_argument('--state-dir', default='state')
    parser.add_argument('--ledger-dir', default='ledger')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        if Path(args.spec).exists():
            raise ValueError('refusing to overwrite an existing frozen trial specification')
        save_json(args.spec, quote_trial.proposed_spec())
        print('Prepared prospective trial; risk limits and data access must be configured before activation.')
        return 0
    spec = quote_trial.load_spec(args.spec)
    if args.command == 'status':
        result = quote_trial.progress(Path(args.ledger_dir) / 'quote_trials', spec)
    else:
        result = collect(spec, args.state_dir, args.ledger_dir)
    # Keep public console output free of market data and secrets.
    print(json.dumps({k: result[k] for k in ('trial_id', 'status', 'eligible_sessions',
                                           'closed_quote_validated_trades', 'eligible_session') if k in result}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
