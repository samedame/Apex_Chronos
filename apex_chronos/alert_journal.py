"""Append-only observations and explicit alert-versus-final-model cohorts.

This observer never changes dispatch, deduplication, sizing or cash controls.
Acknowledged Discord messages are not broker fills. Missing original outcomes
remain unresolved; final-bar results are never substituted for live outcomes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import uuid

SCHEMA = 1
ARRAYS = ('O', 'H', 'L', 'C', 'V', 'vix', 'vwap', 'vwap_std', 'noise_upper',
          'rvol_cum', 'ema9', 'ema21', 'iv')
PLAN_FIELDS = ('direction', 'structure', 'K1', 'K2', 'S_entry', 'iv_entry',
               'cost_basis', 'credit', 'width', 'plan')


class IntegrityError(ValueError):
    pass


def value(x):
    """Lossless finite JSON values; nonfinite inputs are explicit tagged values."""
    if is_dataclass(x):
        return value(asdict(x))
    if isinstance(x, dict):
        return {str(k): value(v) for k, v in x.items()}
    if isinstance(x, (tuple, list, set, frozenset)):
        return [value(v) for v in (sorted(x) if isinstance(x, (set, frozenset)) else x)]
    if hasattr(x, 'tolist'):
        return value(x.tolist())
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if isinstance(x, float) and not math.isfinite(x):
        return {'nonfinite_float': str(x)}
    return x


def canonical(x):
    return json.dumps(value(x), sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(x):
    return hashlib.sha256(canonical(x)).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def entry_key(session, ticker, module, minute):
    return f'{session}|ENTRY|{ticker}|{module}|{int(minute)}'


def position_key(session, event):
    if event.kind == 'ENTRY':
        minute = event.minute
    elif event.kind == 'TP1':
        minute = event.data['m_entry']
    elif event.kind == 'EXIT':
        minute = event.data['trade']['m_entry']
    else:
        return None
    return entry_key(session, event.ticker, event.module, minute)


def plan_digest(data):
    return digest({k: data.get(k) for k in PLAN_FIELDS})


def source_hashes():
    root = Path(__file__).parent
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.py'))}


def read_records(directory, session):
    records = []
    for path in sorted((Path(directory) / str(session)).glob('*.json.gz')):
        try:
            encoded = gzip.decompress(path.read_bytes())
            row = json.loads(encoded)
            if (hashlib.sha256(encoded).hexdigest() != path.name.removesuffix('.json.gz')
                    or canonical(row) != encoded or row['schema_version'] != SCHEMA
                    or row['session'] != str(session)):
                raise IntegrityError('journal record identity does not match its content')
        except Exception as error:
            raise IntegrityError(f'invalid journal record {path.name}') from error
        records.append({'record_id': path.name.removesuffix('.json.gz'), **row})
    return records


class Journal:
    def __init__(self, directory, session):
        self.directory = Path(directory) / str(session)
        self.session = str(session)
        date.fromisoformat(self.session)
        self.run_id = uuid.uuid4().hex

    def put(self, kind, payload, *, stable=False):
        row = {'schema_version': SCHEMA, 'session': self.session, 'kind': kind,
               'observed_at': None if stable else utcnow(),
               'run_id': None if stable else self.run_id, 'payload': value(payload)}
        encoded = canonical(row)
        identity = hashlib.sha256(encoded).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'{identity}.json.gz'
        # A hard link is create-exclusive: concurrent writers cannot overwrite
        # a prior record. Incomplete temporary files are never read as records.
        with NamedTemporaryFile(dir=self.directory, prefix='.pending-', delete=False) as temp:
            temporary = Path(temp.name)
            try:
                temp.write(gzip.compress(encoded, mtime=0))
                temp.flush()
                os.fsync(temp.fileno())
                os.chmod(temporary, 0o444)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if gzip.decompress(path.read_bytes()) != encoded:
                        raise IntegrityError('existing journal record was altered')
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        return identity


class Observer:
    """Best-effort instrumentation: a storage failure never suppresses an alert."""
    def __init__(self, cfg, session):
        self.journal = Journal(Path(cfg.state_dir) / 'alert_journal', session)
        self.cfg = cfg
        self.session = str(session)
        self.context_id = None
        self.input_id = None
        self.previous = {}
        self.entries = {}
        self.current = None
        self.error_count = 0
        self.sources = None

    def observe(self, kind, payload):
        try:
            return self.journal.put(kind, payload)
        except Exception as error:
            self.error_count += 1
            # No exception contents, credentials, webhook URL or private data.
            print(f'alert tracking incomplete: {kind} ({type(error).__name__})')
            return None

    def contexts(self, ctxs, excluded):
        self.context_id = self.observe('session_context', {
            'contexts': ctxs, 'excluded': excluded, 'source_sha256': source_hashes(),
            'settings': {'capital': self.cfg.capital, 'slot_fraction': self.cfg.slot_fraction,
                         'cash_controls_enabled': self.cfg.cash_controls_enabled,
                         'tickers': self.cfg.tickers, 'dry_run': self.cfg.dry_run}})

    def inputs(self, feats, degraded, first_minute, last_minute):
        # Record revisions to earlier bars as well as new bars. Full-prefix
        # snapshots every minute would duplicate most data and bloat git.
        changes, next_previous = {}, {}
        for ticker, f in feats.items():
            old = self.previous.get(ticker, {})
            arrays = {name: value(getattr(f, name)) for name in ARRAYS}
            patches = {}
            for name, values in arrays.items():
                prior = old.get(name, [])
                patches[name] = [[i, v] for i, v in enumerate(values)
                                 if i >= len(prior) or prior[i] != v]
            changes[ticker] = {'n': f.n, 'prev_c': f.prev_c, 'arrays': patches}
            next_previous[ticker] = arrays
        identity = self.observe('consumed_inputs', {
            'context_record_id': self.context_id, 'parent_input_record_id': self.input_id,
            'consumed_minutes': [first_minute, last_minute], 'features': changes,
            'degraded_minutes': degraded})
        if identity is not None:
            self.input_id = identity
            self.previous = next_previous

    def event(self, event):
        if self.sources is None:
            sources = source_hashes()
            self.sources = {k: sources[k] for k in ('config.py', 'data.py', 'history.py',
                            'features.py', 'pricing.py', 'position.py', 'engine.py')}
        position = position_key(self.session, event)
        model_entry = self.entries.get(position)
        identity = self.observe('engine_event', {
            'event': event, 'position_id': position, 'input_record_id': self.input_id,
            'context_record_id': self.context_id, 'model_entry_record_id': model_entry,
            'alert_key': f'{self.session}|{event.key}', 'economic_source_sha256': self.sources})
        if event.kind == 'ENTRY' and identity:
            self.entries[position] = identity
        self.current = {'event_record_id': identity, 'position_id': position}

    def intent(self, key, text, actionable, late, confidence):
        return self.observe('delivery_intent', {
            **(self.current or {}), 'alert_key': key, 'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'actionable_entry': bool(actionable and self.current and '|ENTRY|' in (key or '')),
            'delivery_mode': 'dry_run' if self.cfg.dry_run else 'discord' if self.cfg.webhook else 'unconfigured',
            'late_s': late, 'confidence': confidence})

    def result(self, intent_id, acknowledged, raised=False):
        self.observe('delivery_result', {'intent_record_id': intent_id, 'acknowledged': acknowledged,
                                        'transport_raised': raised, 'prior_tracking_errors': self.error_count})


def legacy_snapshot(journal, state_dir, session):
    path = Path(state_dir) / f'sent-{session}.json'
    if not path.exists():
        return None
    encoded = path.read_bytes()
    sent = json.loads(encoded)
    # Existing metadata does not contain original prices/text/input snapshots.
    # Preserve that limitation; never turn it into a delivery/fill receipt.
    fields = ('t', 'posted', 'late_s', 'pos', 'conf')
    return journal.put('legacy_sent_snapshot', {
        'provenance': 'legacy_metadata_only_not_reconstructed_delivery',
        'source_sha256': hashlib.sha256(encoded).hexdigest(),
        'sent': {k: {f: v[f] for f in fields if f in v} for k, v in sent.items() if '|ENTRY|' in k}}, stable=True)


def build_report(directory, session, *, final_record_id=None):
    records = read_records(directory, session)
    by_id = {r['record_id']: r for r in records}
    intents = {r['record_id']: r for r in records if r['kind'] == 'delivery_intent'}
    finals = [r for r in records if r['kind'] == 'final_model_snapshot']
    if final_record_id:
        final = by_id[final_record_id]
        if final['kind'] != 'final_model_snapshot':
            raise IntegrityError('final record reference has wrong kind')
    else:
        final = max(finals, key=lambda r: (r['observed_at'], r['record_id'])) if finals else None
    # Revisions of legacy files may add entries but must not erase prior evidence.
    legacy = {}
    stale = set()
    for record in records:
        if record['kind'] != 'legacy_sent_snapshot':
            continue
        for key, v in record['payload']['sent'].items():
            if v.get('posted'):
                legacy.setdefault(key, []).append({'record_id': record['record_id'], 'metadata': v})
            else:
                stale.add(key)
    acknowledged, pending, simulated = {}, [], []
    simulated_entry_keys = set()
    results = {}
    for row in records:
        if row['kind'] == 'delivery_result':
            intent = row['payload']['intent_record_id']
            if intent is None or intent not in intents:
                pending.append({'reason': 'receipt_missing_intent', 'record_id': row['record_id']})
                continue
            results.setdefault(intent, []).append(row)
    for identity, row in intents.items():
        payload = row['payload']
        event_id = payload.get('event_record_id')
        if event_id is None:
            continue  # operational message, not a position outcome
        if event_id not in by_id:
            raise IntegrityError('delivery intent references missing engine event')
        if payload['delivery_mode'] != 'discord':
            simulated.append(identity)
            if by_id[event_id]['payload']['event']['kind'] == 'ENTRY':
                simulated_entry_keys.add(payload['alert_key'])
            continue
        receipts = results.get(identity, [])
        if not receipts or not any(r['payload']['acknowledged'] is True for r in receipts):
            pending.append({'reason': 'delivery_unconfirmed', 'intent_record_id': identity,
                            'position_id': payload.get('position_id')})
            continue
        event = by_id[event_id]['payload']['event']
        if event['kind'] == 'ENTRY' and not payload['actionable_entry']:
            stale.add(payload['alert_key'])
            continue
        position = payload.get('position_id')
        if position:
            acknowledged.setdefault(position, []).append({'intent': row, 'event': by_id[event_id]})
    for key in simulated_entry_keys:
        if key not in acknowledged:
            legacy.pop(key, None)
    final_trades = {}
    final_keys = set()
    final_available = bool(final and final['payload'].get('available', True))
    if final_available:
        for trade in final['payload']['trades']:
            key = entry_key(session, trade['ticker'], trade['module'], trade['m_entry'])
            if key in final_trades:
                raise IntegrityError('duplicate final-model position identity')
            final_trades[key] = trade
        final_keys = set(final_trades) | set(final['payload'].get('open_position_ids', []))
    positions = []
    for key in sorted(set(legacy) | set(acknowledged)):
        messages = acknowledged.get(key, [])
        entries = [m for m in messages if m['event']['payload']['event']['kind'] == 'ENTRY']
        if not entries and key not in legacy:
            continue
        entries.sort(key=lambda m: (m['intent']['observed_at'], m['intent']['record_id']))
        row = {'position_id': key, 'entry_evidence': 'acknowledged_webhook' if entries else 'legacy_marked_posted_only',
               'entry_record_ids': [m['event']['record_id'] for m in entries],
               'legacy_evidence': legacy.get(key, []), 'acknowledged_entry_attempts': len(entries),
               'broker_verified_pnl': None, 'communicated_model_roi': None,
               'final_model_key_present': key in final_keys if final_available else None,
               'final_model_trade': final_trades.get(key),
               'outcome_status': 'legacy_original_outcome_unavailable' if not entries else 'unresolved_no_acknowledged_exit',
               'flags': []}
        if entries:
            entry = entries[0]['event']
            entry_data = entry['payload']['event']['data']
            row['entry_model_plan'] = entry_data
            row['entry_input_record_id'] = entry['payload'].get('input_record_id')
            if not row['entry_input_record_id']:
                row['flags'].append('entry_consumed_inputs_unavailable')
            if len(entries) > 1:
                row['flags'].append('multiple_acknowledged_entry_attempts')
            exits = [m for m in messages if m['event']['payload']['event']['kind'] == 'EXIT']
            exits.sort(key=lambda m: (m['intent']['observed_at'], m['intent']['record_id']))
            row['acknowledged_exit_record_ids'] = [m['event']['record_id'] for m in exits]
            if exits:
                ex = exits[0]['event']
                originating = by_id.get(ex['payload'].get('model_entry_record_id'))
                if len(entries) > 1:
                    row['outcome_status'] = 'unresolved_duplicate_entry_delivery'
                elif len(exits) > 1:
                    row['outcome_status'] = 'unresolved_duplicate_exit_delivery'
                elif originating is None:
                    row['outcome_status'] = 'unresolved_exit_origin_unavailable'
                elif (plan_digest(originating['payload']['event']['data']) != plan_digest(entry_data)
                      or originating['payload'].get('economic_source_sha256') != entry['payload'].get('economic_source_sha256')):
                    row['outcome_status'] = 'unresolved_entry_plan_revision'
                elif (ex['payload']['event']['data']['trade']['cost_basis'] != entry_data['cost_basis']
                      or ex['payload']['event']['data']['trade']['direction'] != entry_data['direction']):
                    row['outcome_status'] = 'unresolved_exit_basis_mismatch'
                else:
                    required_tp = [r for r in records if r['kind'] == 'engine_event'
                        and r['payload'].get('position_id') == key
                        and r['payload']['event']['kind'] == 'TP1'
                        and r['payload'].get('model_entry_record_id') == originating['record_id']
                        and r['observed_at'] <= ex['observed_at']]
                    delivered_tp = set()
                    for message in messages:
                        if message['event']['payload']['event']['kind'] != 'TP1':
                            continue
                        origin = by_id.get(message['event']['payload'].get('model_entry_record_id'))
                        if (origin and plan_digest(origin['payload']['event']['data']) == plan_digest(entry_data)
                                and message['intent']['observed_at'] <= exits[0]['intent']['observed_at']):
                            delivered_tp.add(message['intent']['payload']['alert_key'])
                    if any(tp['payload']['alert_key'] not in delivered_tp for tp in required_tp):
                        row['outcome_status'] = 'unresolved_scaleout_delivery'
                    else:
                        row['outcome_status'] = 'acknowledged_exit_model_outcome'
                        row['communicated_model_roi'] = ex['payload']['event']['data']['trade']['roi']
                        row['exit_model_trade'] = ex['payload']['event']['data']['trade']
        positions.append(row)
    posted_keys = {p['position_id'] for p in positions}
    closed = [p['communicated_model_roi'] for p in positions if p['communicated_model_roi'] is not None]
    unresolved = [p for p in positions if p['communicated_model_roi'] is None]
    max_tracking_errors = max((r['payload'].get('prior_tracking_errors', 0) for r in records
                              if r['kind'] == 'delivery_result'), default=0)
    return {'schema_version': SCHEMA, 'date': str(session),
            'basis': 'posted_alert_cohort_separate_from_final_bar_model_not_broker_fills',
            'final_model_available': final_available,
            'final_model_observed_at': final['observed_at'] if final else None,
            'final_model_snapshot_id': final['record_id'] if final else None,
            'journal_record_ids': sorted(by_id), 'journal_record_count': len(records),
            'tracking_quality': {'present_record_hashes_verified': True,
                'max_observed_run_tracking_errors': max_tracking_errors,
                'positions_missing_original_input_snapshot': sum(not p.get('entry_input_record_id') for p in positions),
                'flagged_positions': sum(bool(p['flags']) for p in positions)},
            'final_model_snapshot_revisions': [r['record_id'] for r in finals],
            'summary': {'posted_entry_keys': len(positions),
                'acknowledged_webhook_entry_keys': sum(p['entry_evidence'] == 'acknowledged_webhook' for p in positions),
                'legacy_marked_posted_entry_keys': sum(p['entry_evidence'] == 'legacy_marked_posted_only' for p in positions),
                'stale_or_nonactionable_entry_keys': len(stale),
                'final_model_entry_keys': len(final_keys) if final_available else None,
                'posted_keys_matching_final_model': len(posted_keys & final_keys) if final_available else None,
                'posted_only_entry_keys': len(posted_keys - final_keys) if final_available else None,
                'final_model_only_entry_keys': len(final_keys - posted_keys) if final_available else None,
                'closed_communicated_model_outcomes': len(closed), 'unresolved_posted_positions': len(unresolved),
                'mean_communicated_model_roi_closed_subset': sum(closed) / len(closed) if closed else None,
                'complete_posted_cohort_mean_model_roi': sum(closed) / len(closed) if closed and not unresolved else None,
                'broker_verified_trade_count': 0, 'broker_verified_pnl': None,
                'unconfirmed_delivery_attempts': len(pending), 'simulated_or_unconfigured_attempts': len(simulated)},
            'posted_positions': positions,
            'posted_only_entry_ids': sorted(posted_keys - final_keys) if final_available else [],
            'final_model_only_entry_ids': sorted(final_keys - posted_keys) if final_available else [],
            'unconfirmed_deliveries': pending,
            'limitations': ['Webhook acknowledgment is not recipient action or a broker fill.',
                'Historical metadata cannot recover original prices, inputs, message text or outcomes.',
                'Missing or revised original entry/exit evidence stays unresolved; final-model ROI is not substituted.',
                'The journal is application-append-only and content-hash verified, not external WORM storage.']}


def save_report(ledger_dir, session, report):
    directory = Path(ledger_dir) / 'alert_outcomes'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{session}.json'
    with NamedTemporaryFile(mode='wb', dir=directory, prefix='.report-', delete=False) as temp:
        path = Path(temp.name)
        try:
            temp.write(canonical(report) + b'\n')
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(path, target)
        finally:
            path.unlink(missing_ok=True)
    return target


def reconcile(state_dir, ledger_dir, session, trades, *, days=None, excluded=None, open_position_ids=(), final_available=True):
    save_report(ledger_dir, f'{session}.status', {'date': str(session), 'status': 'refreshing', 'checked_at': utcnow()})
    try:
        journal = Journal(Path(state_dir) / 'alert_journal', session)
        legacy_snapshot(journal, state_dir, session)
        input_id = journal.put('final_model_inputs', {'days': days, 'excluded': excluded}) if days is not None else None
        identity = journal.put('final_model_snapshot', {'trades': trades, 'open_position_ids': open_position_ids,
            'available': final_available,
            'input_record_id': input_id, 'source_sha256': source_hashes(),
            'outcome_basis': 'final_bar_model_only_not_original_alert_outcome'})
        report = build_report(Path(state_dir) / 'alert_journal', session, final_record_id=identity)
        save_report(ledger_dir, session, report)
    except Exception as error:
        save_report(ledger_dir, f'{session}.status', {'date': str(session), 'status': 'refresh_failed',
            'checked_at': utcnow(), 'error_class': type(error).__name__})
        raise
    save_report(ledger_dir, f'{session}.status', {'date': str(session), 'status': 'projection_ready',
        'checked_at': utcnow(), 'final_model_snapshot_id': identity,
        'unresolved_posted_positions': report['summary']['unresolved_posted_positions']})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline alert/outcome reconciliation. Never sends alerts or broker requests.')
    parser.add_argument('--state-dir', default='state')
    parser.add_argument('--ledger-dir', default='ledger')
    parser.add_argument('--date', help='YYYY-MM-DD; otherwise all saved dates')
    args = parser.parse_args(argv)
    from .state import read_ledger
    trades = read_ledger(args.ledger_dir)
    sessions = ({args.date} if args.date else {t['date'] for t in trades}
                | {p.stem.removeprefix('sent-') for p in Path(args.state_dir).glob('sent-*.json')})
    for session in sorted(sessions):
        date.fromisoformat(session)
        selected = [t for t in trades if t['date'] == session]
        zero_day_record = Path(args.state_dir) / f'reconcile-{session}.json'
        explicit_zero = (zero_day_record.exists()
                         and json.loads(zero_day_record.read_text()).get('final_entries') == 0)
        report = reconcile(args.state_dir, args.ledger_dir, session,
                           selected, final_available=bool(selected) or explicit_zero)
        print(json.dumps({'date': session, **report['summary']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
