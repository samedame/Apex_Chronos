from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from apex_chronos import alert_journal as J, alerts, config as C, runner, state
from apex_chronos.calendar_nyse import session_open
from apex_chronos.engine import Event

DAY = date(2026, 11, 11)
MODULE = C.MODULES['P1A'].name


def events(*, half=False):
    plan = {'stop_roi': -.26, 'tp1_roi': .9, 'after_tp1_floor': .54,
            'trail_pct_of_peak': .72, 'm_end': 55, 'max_hold': 50}
    data = {'slot': 0, 'direction': 1, 'module_key': 'P1A', 'phase': 'P1',
            'structure': 'single', 'legs': 'BUY 600 CALL', 'K1': 600., 'K2': None,
            'S_entry': 599., 'iv_entry': .2, 'cost_basis': 1., 'rvol_entry': .8,
            'score': 9.2, 'plan': plan, 'credit': None, 'width': None}
    trade = {'ticker': 'SPY', 'date': str(DAY), 'm_entry': 5, 'm_exit': 12, 'hold_mins': 7,
             'direction': 1, 'cost_basis': 1., 'roi': .95 if half else -.28,
             'peak_roi': 1. if half else .1, 'exit_reason': 'profit_trail' if half else 'stop_loss',
             'rvol_entry': .8, 'module': MODULE, 'phase': 'P1', 'score': 9.2, 'slot': 0}
    out = [Event('ENTRY', 'SPY', MODULE, 5, data)]
    if half:
        out.append(Event('TP1', 'SPY', MODULE, 10, {'model_roi': 1., 'm_entry': 5, 'slot': 0}))
    return out + [Event('EXIT', 'SPY', MODULE, 12, {'trade': trade})]


def session(tmp_path, monkeypatch, *, dry_run=False, outcome=True):
    texts = []
    def post(webhook, text, dry_run=False):
        texts.append(text)
        return outcome
    monkeypatch.setattr(alerts, 'post_discord', post)
    cfg = runner.Settings(state_dir=str(tmp_path/'state'), ledger_dir=str(tmp_path/'ledger'),
                          webhook='https://secret.invalid/private-webhook', capital=1500, dry_run=dry_run)
    return runner.LiveSession(cfg, DAY), texts


def dispatch(sess, event, *, late=7):
    return sess.dispatch([event], session_open(DAY) + timedelta(minutes=event.minute + 1, seconds=late))


def report(sess, trades=()):
    return J.reconcile(sess.cfg.state_dir, sess.cfg.ledger_dir, str(DAY), list(trades))


def test_create_exclusive_records_are_idempotent_and_concurrent(tmp_path):
    journal = J.Journal(tmp_path, DAY)
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: journal.put('test', {'a': 1}, stable=True), range(20)))
    assert len(set(identities)) == 1
    records = J.read_records(tmp_path, DAY)
    assert len(records) == 1 and records[0]['payload'] == {'a': 1}
    original = next((tmp_path/str(DAY)).glob('*.json.gz')).read_bytes()
    journal.put('test', {'a': 2}, stable=True)
    assert len(J.read_records(tmp_path, DAY)) == 2
    assert (tmp_path/str(DAY)/f'{identities[0]}.json.gz').read_bytes() == original


def test_altered_record_is_rejected_not_silently_used(tmp_path):
    journal = J.Journal(tmp_path, DAY)
    identity = journal.put('test', {'roi': .1}, stable=True)
    p = tmp_path/str(DAY)/f'{identity}.json.gz'
    p.chmod(0o644)
    row = json.loads(gzip.decompress(p.read_bytes()))
    row['payload']['roi'] = 99
    p.write_bytes(gzip.compress(J.canonical(row)))
    with pytest.raises(J.IntegrityError):
        J.read_records(tmp_path, DAY)
    with pytest.raises(J.IntegrityError):
        journal.put('test', {'roi': .1}, stable=True)


def test_receipt_survives_sent_file_failure_after_success(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    monkeypatch.setattr(state, 'save_sent', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        dispatch(sess, events()[0])
    assert len(texts) == 1
    r = J.build_report(Path(sess.cfg.state_dir)/'alert_journal', DAY)
    assert r['summary']['acknowledged_webhook_entry_keys'] == 1
    assert r['summary']['unresolved_posted_positions'] == 1


def test_posted_only_trade_retains_original_exit_not_final_replacement(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    entry, exit = events()
    dispatch(sess, entry); dispatch(sess, exit)
    unrelated = {**exit.data['trade'], 'ticker': 'QQQ', 'roi': 123.}
    r = report(sess, [unrelated])
    assert r['summary']['posted_only_entry_keys'] == 1
    assert r['summary']['final_model_only_entry_keys'] == 1
    assert r['posted_positions'][0]['communicated_model_roi'] == -.28
    assert r['summary']['complete_posted_cohort_mean_model_roi'] == -.28
    assert r['summary']['broker_verified_pnl'] is None
    assert len(texts) == 2


def test_revised_eod_keeps_all_snapshots_and_never_invents_missing_exit(tmp_path, monkeypatch):
    sess, _ = session(tmp_path, monkeypatch)
    entry, exit = events()
    dispatch(sess, entry)
    first = report(sess, [exit.data['trade']])
    before = {p.name:p.read_bytes() for p in (Path(sess.cfg.state_dir)/'alert_journal'/str(DAY)).glob('*.json.gz')}
    second = report(sess, [{**exit.data['trade'], 'roi': 99.}])
    assert first['final_model_snapshot_id'] != second['final_model_snapshot_id']
    assert len(second['final_model_snapshot_revisions']) == 2
    assert second['posted_positions'][0]['final_model_trade']['roi'] == 99
    assert second['posted_positions'][0]['communicated_model_roi'] is None
    assert second['summary']['complete_posted_cohort_mean_model_roi'] is None
    for name, content in before.items():
        assert (Path(sess.cfg.state_dir)/'alert_journal'/str(DAY)/name).read_bytes() == content


def test_stale_dry_run_unconfigured_and_unconfirmed_are_separate(tmp_path, monkeypatch):
    sess, texts = session(tmp_path/'stale', monkeypatch)
    dispatch(sess, events()[0], late=1000)
    r = report(sess)
    assert r['summary']['posted_entry_keys'] == 0
    assert r['summary']['stale_or_nonactionable_entry_keys'] == 1
    assert texts[0].startswith('⚪ Missed entry')
    sess, _ = session(tmp_path/'dry', monkeypatch, dry_run=True)
    dispatch(sess, events()[0])
    r = report(sess)
    assert r['summary']['posted_entry_keys'] == 0
    assert r['summary']['simulated_or_unconfigured_attempts'] == 1
    sess, _ = session(tmp_path/'uncertain', monkeypatch, outcome=False)
    dispatch(sess, events()[0])
    r = report(sess)
    assert r['summary']['posted_entry_keys'] == 0
    assert r['summary']['unconfirmed_delivery_attempts'] == 1


def test_crash_between_intent_and_result_is_unconfirmed(tmp_path):
    cfg = runner.Settings(state_dir=str(tmp_path))
    observer = J.Observer(cfg, DAY)
    observer.cfg.webhook = 'configured-but-not-stored'
    observer.event(events()[0])
    observer.intent('entry-key', 'original message', True, 7, 80)
    r = J.build_report(tmp_path/'alert_journal', DAY)
    assert r['summary']['posted_entry_keys'] == 0
    assert r['summary']['unconfirmed_delivery_attempts'] == 1


def test_unconfirmed_additional_entry_blocks_complete_cohort_mean(tmp_path,monkeypatch):
    sess, _ = session(tmp_path,monkeypatch)
    entry, exit = events()
    dispatch(sess,entry); dispatch(sess,exit)
    extra=deepcopy(entry);extra.ticker='QQQ'
    monkeypatch.setattr(alerts,'post_discord',lambda *a,**kw:False)
    dispatch(sess,extra)
    r=report(sess)
    assert r['summary']['closed_communicated_model_outcomes']==1
    assert r['summary']['unconfirmed_delivery_attempts']==1
    assert r['summary']['complete_posted_cohort_mean_model_roi'] is None


def test_restart_dedup_does_not_create_acknowledged_delivery(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    entry, exit = events()
    dispatch(sess, entry)
    resumed = runner.LiveSession(sess.cfg, DAY)
    dispatch(resumed, entry); dispatch(resumed, exit)
    r = report(resumed)
    assert len(texts) == 2
    assert r['posted_positions'][0]['acknowledged_entry_attempts'] == 1
    assert r['posted_positions'][0]['communicated_model_roi'] == -.28


def test_restart_changed_entry_basis_is_unresolved(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    entry, exit = events()
    dispatch(sess, entry)
    resumed = runner.LiveSession(sess.cfg, DAY)
    revised = deepcopy(entry); revised.data['cost_basis'] = 2.
    revised_exit = deepcopy(exit); revised_exit.data['trade']['cost_basis'] = 2.
    dispatch(resumed, revised); dispatch(resumed, revised_exit)
    r = report(resumed)
    row = r['posted_positions'][0]
    assert row['entry_model_plan']['cost_basis'] == 1.
    assert row['outcome_status'] == 'unresolved_entry_plan_revision'
    assert row['communicated_model_roi'] is None
    assert len(texts) == 2


def test_missing_scaleout_receipt_prevents_complete_outcome(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    entry, tp, exit = events(half=True)
    dispatch(sess, entry)
    monkeypatch.setattr(alerts, 'post_discord', lambda *a, **kw: False)
    dispatch(sess, tp)
    monkeypatch.setattr(alerts, 'post_discord', lambda *a, **kw: True)
    dispatch(sess, exit)
    r = report(sess)
    assert r['posted_positions'][0]['outcome_status'] == 'unresolved_scaleout_delivery'
    assert r['summary']['complete_posted_cohort_mean_model_roi'] is None


def test_half_exit_dedup_across_restart_preserves_delivered_outcome(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    entry, tp, exit = events(half=True)
    dispatch(sess, entry); dispatch(sess, tp)
    resumed = runner.LiveSession(sess.cfg, DAY)
    dispatch(resumed, entry); dispatch(resumed, tp); dispatch(resumed, exit)
    assert len(texts) == 3
    r = report(resumed)
    assert r['posted_positions'][0]['communicated_model_roi'] == .95


def test_tracking_write_failure_keeps_message_and_sent_state_unchanged(tmp_path, monkeypatch):
    sess, texts = session(tmp_path, monkeypatch)
    monkeypatch.setattr(J.Journal, 'put', lambda *a, **kw: (_ for _ in ()).throw(OSError('secret-provider-response')))
    entry, exit = events()
    assert dispatch(sess, entry) == 1
    assert dispatch(sess, exit) == 1
    assert len(texts) == 2
    assert len(state.load_sent(sess.cfg.state_dir, DAY)) == 2
    assert sess.cfg.cash_controls_enabled is False


def test_input_deltas_preserve_prior_revisions_and_nonfinite_values(tmp_path):
    observer = J.Observer(runner.Settings(state_dir=str(tmp_path)), DAY)
    def feature(values):
        return SimpleNamespace(n=len(values), prev_c=99., **{a:np.array(values,dtype=float) for a in J.ARRAYS})
    observer.inputs({'SPY':feature([1,2])}, {'SPY':set()}, 0,1)
    first = observer.input_id
    observer.inputs({'SPY':feature([1,3,np.nan])}, {'SPY':{2}}, 2,2)
    records = {r['record_id']:r for r in J.read_records(tmp_path/'alert_journal',DAY)}
    patches = records[observer.input_id]['payload']
    assert patches['parent_input_record_id'] == first
    assert patches['features']['SPY']['arrays']['C'] == [[1,3.],[2,{'nonfinite_float':'nan'}]]
    rebuilt = records[first]['payload']['features']['SPY']['arrays']['C']
    array = [v for i,v in rebuilt]
    for i,v in patches['features']['SPY']['arrays']['C']:
        if i == len(array): array.append(v)
        else: array[i]=v
    assert array == [1.,3.,{'nonfinite_float':'nan'}]


def test_failed_input_capture_cannot_reuse_old_evidence_or_complete_outcome(tmp_path,monkeypatch):
    sess, texts=session(tmp_path,monkeypatch)
    f=SimpleNamespace(n=1,prev_c=99.,**{a:np.array([1.]) for a in J.ARRAYS})
    sess.tracking.inputs({'SPY':f},{},0,0)
    old=sess.tracking.input_id
    real=J.Journal.put
    def failed_input(self,kind,payload,**kwargs):
        if kind=='consumed_inputs':raise OSError('storage')
        return real(self,kind,payload,**kwargs)
    monkeypatch.setattr(J.Journal,'put',failed_input)
    sess.tracking.inputs({'SPY':f},{},1,1)
    assert old and sess.tracking.input_id is None and not sess.tracking.previous
    entry,exit=events()
    dispatch(sess,entry);dispatch(sess,exit)
    r=report(sess)
    assert len(texts)==2
    assert r['posted_positions'][0]['outcome_status']=='unresolved_tracking_gap'
    assert r['summary']['complete_posted_cohort_mean_model_roi'] is None


def test_failed_reconstructed_entry_capture_clears_older_origin(tmp_path,monkeypatch):
    sess,_=session(tmp_path,monkeypatch)
    entry=events()[0]
    sess.tracking.event(entry)
    key=J.position_key(DAY,entry)
    assert key in sess.tracking.entries
    monkeypatch.setattr(J.Journal,'put',lambda *a,**kw:(_ for _ in ()).throw(OSError('storage')))
    sess.tracking.event(entry)
    assert key not in sess.tracking.entries


def test_legacy_metadata_is_preserved_without_reconstructed_returns(tmp_path):
    state_dir, ledger = tmp_path/'state', tmp_path/'ledger'
    key = J.entry_key(DAY,'SPY',MODULE,5)
    state.save_sent(str(state_dir),DAY,{key:{'posted':True,'pos':['SPY',MODULE,5],'t':'original-time'},
        J.entry_key(DAY,'QQQ',MODULE,6):{'posted':False}})
    r = J.reconcile(state_dir,ledger,str(DAY),[events()[-1].data['trade']])
    assert r['summary']['legacy_marked_posted_entry_keys']==1
    assert r['summary']['acknowledged_webhook_entry_keys']==0
    assert r['summary']['stale_or_nonactionable_entry_keys']==1
    assert r['posted_positions'][0]['communicated_model_roi'] is None
    journal = J.Journal(state_dir/'alert_journal',DAY)
    before = len(J.read_records(state_dir/'alert_journal',DAY))
    J.legacy_snapshot(journal,state_dir,str(DAY))
    assert len(J.read_records(state_dir/'alert_journal',DAY))==before


def test_unavailable_final_data_are_not_reported_as_zero_trades(tmp_path, monkeypatch):
    sess, _ = session(tmp_path,monkeypatch)
    dispatch(sess,events()[0])
    r = J.reconcile(sess.cfg.state_dir,sess.cfg.ledger_dir,str(DAY),[],final_available=False)
    assert r['final_model_available'] is False
    assert r['summary']['final_model_entry_keys'] is None
    assert r['summary']['posted_only_entry_keys'] is None
    assert r['posted_positions'][0]['final_model_key_present'] is None


def test_corrupt_refresh_marks_previous_projection_stale(tmp_path, monkeypatch):
    sess, _ = session(tmp_path,monkeypatch)
    dispatch(sess,events()[0]); report(sess)
    projection=Path(sess.cfg.ledger_dir)/'alert_outcomes'/f'{DAY}.json'
    before=projection.read_bytes()
    record=next((Path(sess.cfg.state_dir)/'alert_journal'/str(DAY)).glob('*.json.gz'))
    record.chmod(0o644);record.write_bytes(b'corrupt')
    with pytest.raises(J.IntegrityError):report(sess)
    status=json.loads(projection.with_name(f'{DAY}.status.json').read_text())
    assert status['status']=='refresh_failed'
    assert projection.read_bytes()==before


def test_journal_does_not_store_webhook_or_enable_cash_controls(tmp_path,monkeypatch):
    sess, _ = session(tmp_path,monkeypatch)
    sess.tracking.contexts({}, {})
    dispatch(sess,events()[0]); report(sess)
    for r in J.read_records(Path(sess.cfg.state_dir)/'alert_journal',DAY):
        assert 'private-webhook' not in json.dumps(r)
    assert runner.Settings.from_env().cash_controls_enabled is False
