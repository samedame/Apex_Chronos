"""Offline synthetic-delivery verification; no webhook, broker or trial evidence."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from apex_chronos import alert_journal as J, alerts, config as C, quote_trial, replay, runner
from apex_chronos.calendar_nyse import session_open
from apex_chronos.features import compute_features
from verification.fixtures import extract


def verify(days):
    cases, messages, closed = [], [], []
    with TemporaryDirectory() as directory, patch('requests.Session.request', side_effect=AssertionError('network forbidden')):
        for session, day in sorted(days.items()):
            eng, events = replay.run_session(session, day, list(C.TICKERS))
            cfg = runner.Settings(state_dir=str(Path(directory)/session/'state'),
                ledger_dir=str(Path(directory)/session/'ledger'), webhook='synthetic://offline-recorder', capital=1500)
            calls = []
            def capture(webhook, text, dry_run=False):
                calls.append(text)
                return True
            with patch.object(alerts, 'post_discord', capture):
                sess = runner.LiveSession(cfg, date.fromisoformat(session))
                sess.tracking.contexts({t:d.ctx for t,d in day.items()}, {})
                fs = {t:compute_features(d.ctx,d.O,d.H,d.L,d.C,d.V,d.vix) for t,d in day.items()}
                def inputs(target, start):
                    prefix = {t:SimpleNamespace(n=target+1,prev_c=f.prev_c,
                        **{name:getattr(f,name)[:target+1] for name in J.ARRAYS}) for t,f in fs.items()}
                    sess.tracking.inputs(prefix,{},start,target)
                midpoint = len(events)//2
                last = -1
                for i, event in enumerate(events):
                    now = session_open(sess.date)+timedelta(minutes=event.minute+1,seconds=7)
                    inputs(event.minute,last+1)
                    sess.dispatch([event],now)
                    last=event.minute
                    if i==midpoint:
                        original_count=len(calls)
                        sess=runner.LiveSession(cfg,sess.date)
                        sess.tracking.contexts({t:d.ctx for t,d in day.items()}, {})
                        inputs(event.minute,0)
                        sess.dispatch(events[:i+1],now)
                        assert len(calls)==original_count, 'restart changed delivered messages'
                report=J.reconcile(cfg.state_dir,cfg.ledger_dir,session,eng.trades)
                summary=report['summary']
                assert summary['acknowledged_webhook_entry_keys']==len(eng.trades)
                assert summary['closed_communicated_model_outcomes']==len(eng.trades)
                assert summary['unresolved_posted_positions']==0
                assert summary['posted_only_entry_keys']==0 and summary['final_model_only_entry_keys']==0
                expected={J.entry_key(session,t['ticker'],t['module'],t['m_entry']):t['roi'] for t in eng.trades}
                for position in report['posted_positions']:
                    assert position['communicated_model_roi']==expected[position['position_id']]
                    assert position['broker_verified_pnl'] is None
                    assert position['entry_input_record_id']
                    closed.append(position['communicated_model_roi'])
                # A changed final replay must retain the original posted-only
                # trade and its original model outcome, not rewrite history.
                changed=J.reconcile(cfg.state_dir,cfg.ledger_dir,session,eng.trades[1:])
                assert changed['summary']['posted_only_entry_keys']==1
                assert changed['summary']['closed_communicated_model_outcomes']==len(eng.trades)
                assert changed['summary']['complete_posted_cohort_mean_model_roi']==summary['complete_posted_cohort_mean_model_roi']
                assert len(changed['final_model_snapshot_revisions'])==2
                messages.extend(calls)
                cases.append({'date':session,'model_trades':len(eng.trades),'delivered_messages':len(calls),
                              'restart_duplicate_messages':0,'outcomes_reconciled':True,'revised_final_did_not_erase_alert':True})
    # Existing golden formatter bytes independently remain pinned by unit tests.
    assert len(closed)==110
    message_hash=hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()
    baseline=json.loads((Path(__file__).parents[1]/'tests/golden/legacy-dispatch.json').read_text())
    assert len(messages)==baseline['messages'] and message_hash==baseline['message_sha256'], 'live dispatch message bytes changed'
    quote_trial.load_spec('trial/cash_quote_trial.json')
    return {'status':'PASS','capture_mode':'offline_synthetic_delivery_not_market_or_broker_evidence',
            'sessions':len(cases),'model_trades':len(closed),'messages':len(messages),
            'message_sha256':message_hash,'matches_pre_tracking_dispatch_bytes':True,
            'mean_communicated_model_roi':float(np.mean(closed)),
            'all_original_outcomes_reconciled':True,'restart_cases':len(cases),
            'revised_final_snapshot_cases':len(cases),'cash_controls_enabled':False,
            'frozen_quote_trial_valid':True,'real_network_requests':0,'real_broker_fills':0,'sessions_detail':cases}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default='verification/fixtures.tar.gz')
    parser.add_argument('--output')
    args=parser.parse_args(argv)
    with TemporaryDirectory() as temporary:
        source=extract(args.data_dir,temporary) if args.data_dir.endswith('.tar.gz') else args.data_dir
        _,days=replay.build_days(source,list(C.TICKERS))
        result=verify(days)
    encoded=json.dumps(result,indent=2,allow_nan=False)+'\n'
    if args.output:Path(args.output).write_text(encoded)
    print(encoded)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
