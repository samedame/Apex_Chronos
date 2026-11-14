"""Muted phases and the pre-registered Phase 2 keep/retire rule."""
import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from apex_chronos import alerts, config as C, phase_review as pr, replay, runner, state
from apex_chronos.calendar_nyse import session_open

ROOT = Path(__file__).resolve().parent.parent


def row(d, roi, phase='P2', m=100, ticker='SPY'):
    return {'ticker': ticker, 'date': d, 'm_entry': str(m), 'roi': str(roi), 'phase': phase}


def days_from(start, n):
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def p2_sample(rois, per_day=2, start='2026-11-16'):
    ds = days_from(start, (len(rois) + per_day - 1) // per_day)
    return [row(ds[i // per_day], r, m=100 + i % per_day) for i, r in enumerate(rois)]


# ---- the frozen rule ---------------------------------------------------------------------------------------------
def test_rule_is_the_one_written_down_on_2026_11_13():
    assert pr.RULE == {'phase': 'P2', 'start_date': '2026-11-16', 'n_required': 100, 'keep_min_mean': 0.01,
                       'keep_lower_bound_above': 0.0, 'stop_cum_floor': -1.00, 'bootstrap_draws': 10000, 'seed': 20261113}
    doc = (ROOT / 'docs' / 'P2_DECISION.md').read_text()
    for needle in ('2026-11-16', '100 trades', '+1.0%', '-100% of collateral', '10,000 draws, seed 20261113'):
        assert needle in doc


def test_the_40_trades_that_picked_p2_do_not_count():
    with open(ROOT / 'ledger' / 'trades.csv', newline='') as fh:
        rows = [r for r in csv.DictReader(fh) if r['date'] <= '2026-11-12']
    assert sum(1 for r in rows if r['phase'] == 'P2') == 40
    rv = pr.p2_review(rows)
    assert rv['n'] == 0 and rv['status'] == pr.COLLECTING and rv['mean'] is None


def test_forward_record_by_phase_matches_the_decision_doc():
    with open(ROOT / 'ledger' / 'trades.csv', newline='') as fh:
        rows = [r for r in csv.DictReader(fh) if r['date'] <= '2026-11-12']
    s = pr.by_phase(rows)
    assert (s['P1']['n'], round(s['P1']['mean'] * 100, 1), round(s['P1']['win_rate'] * 100)) == (74, -3.4, 32)
    assert (s['P2']['n'], round(s['P2']['mean'] * 100, 1), round(s['P2']['win_rate'] * 100)) == (40, 3.3, 72)
    assert (s['P3']['n'], round(s['P3']['mean'] * 100, 1), round(s['P3']['win_rate'] * 100)) == (6, -10.3, 33)


def test_no_verdict_before_100_trades_however_good():
    rv = pr.p2_review(p2_sample([0.10] * 99))
    assert rv['status'] == pr.COLLECTING and rv['n'] == 99 and rv['lower_bound'] is None


def test_keep_needs_both_the_floor_and_a_positive_lower_bound():
    good = [0.12, -0.05, 0.08, 0.02] * 25                      # mean +4.25%, steady
    rv = pr.p2_review(p2_sample(good))
    assert rv['status'] == pr.KEEP and rv['mean'] == pytest.approx(0.0425) and rv['lower_bound'] > 0
    thin = [0.02, -0.01, 0.01, 0.0] * 25                       # mean +0.5%: positive and stable, but under the +1.0% cost floor
    rv = pr.p2_review(p2_sample(thin))
    assert rv['status'] == pr.RETIRE and 0 < rv['mean'] < 0.01 and rv['lower_bound'] > 0
    noisy = [0.60] * 10 + [-0.045] * 90                        # mean +1.95%, all of it from one session in ten: lower bound below 0
    rv = pr.p2_review(p2_sample(noisy, per_day=10))
    assert rv['mean'] > 0.01 and rv['lower_bound'] <= 0 and rv['status'] == pr.RETIRE
    assert pr.p2_review(p2_sample([-0.004] * 100))['status'] == pr.RETIRE


def test_verdict_is_fixed_by_the_first_100_trades():
    first = [0.12, -0.05, 0.08, 0.02] * 25
    rv = pr.p2_review(p2_sample(first + [-0.30] * 40))
    assert rv['status'] == pr.KEEP and rv['n'] == 100 and rv['trades_since_start'] == 140
    assert pr.p2_review(p2_sample(first))['lower_bound'] == rv['lower_bound']      # seeded: same ledger, same answer


def test_early_stop_at_minus_100_percent_of_collateral():
    rv = pr.p2_review(p2_sample([-0.20] * 5))
    assert rv['status'] == pr.STOPPED and rv['n'] == 5 and '5 trades' in rv['why']
    assert pr.p2_is_retired(p2_sample([-0.20] * 5))
    assert pr.p2_review(p2_sample([-0.20] * 4))['status'] == pr.COLLECTING           # -80%: not yet
    # a later recovery does not undo an early stop
    assert pr.p2_review(p2_sample([-0.20] * 5 + [0.20] * 95))['status'] == pr.STOPPED


def test_other_phases_and_earlier_dates_are_ignored():
    rows = p2_sample([0.05] * 10) + [row('2026-11-17', -0.9, phase='P1'), row('2026-11-13', -0.9)]
    rv = pr.p2_review(rows)
    assert rv['n'] == 10 and rv['mean'] == pytest.approx(0.05)


def test_muted_phases_parsing():
    assert pr.parse_muted('P1,P3') == {'P1', 'P3'} and pr.parse_muted(' p1 , p3 ') == {'P1', 'P3'}
    for off in (None, '', 'none', 'OFF', '0'):
        assert pr.parse_muted(off) == frozenset()
    with pytest.raises(ValueError):
        pr.parse_muted('P1,P9')
    assert {pr.phase_of(s.name) for s in C.MODULES.values()} == {'P1', 'P2', 'P3'}


def test_eod_lines_show_the_split_and_the_count():
    rows = p2_sample([0.05] * 10) + [row('2026-11-17', -0.2, phase='P1'), row('2026-11-17', -0.1, phase='P3')]
    a, b = pr.eod_lines(rows, frozenset({'P1', 'P3'}))
    assert 'P1 1 trades · -20.0% · 0% wins (muted, logged only)' in a and 'P2 10 trades · +5.0% · 100% wins (alerted)' in a
    assert '10 of 100 new model trades since 2026-11-16' in b and '**COLLECTING**' in b and 'not fills' in a
    _, b = pr.eod_lines(p2_sample([-0.20] * 5), frozenset({'P1', 'P3'}))
    assert 'RETIRE (stopped early)' in b and 'muted from the next session' in b


# ---- the live loop -----------------------------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.texts = []

    def __call__(self, webhook, text, dry_run=False, retries=4):
        self.texts.append(text)
        return True


def make_session(tmp_path, d, monkeypatch, muted=frozenset(), ledger_rows=None):
    rec = Recorder()
    monkeypatch.setattr(alerts, 'post_discord', rec)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'), webhook='https://x',
                          dry_run=False, muted_phases=frozenset(muted))
    if ledger_rows is not None:
        os.makedirs(cfg.ledger_dir, exist_ok=True)
        with open(os.path.join(cfg.ledger_dir, 'trades.csv'), 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=state.LEDGER_COLUMNS, extrasaction='ignore', restval='0')
            w.writeheader()
            w.writerows(ledger_rows)
    return runner.LiveSession(cfg, date.fromisoformat(d)), rec


@pytest.fixture(scope='module')
def all_events(days, fast_run):
    """Every engine event of the recorded dataset, by date."""
    _, by_day = days
    out = {}
    for d in sorted(fast_run[0]['date'].unique()):
        _, ev = replay.run_session(d, by_day[d], list(C.TICKERS), mode='fast')
        out[d] = ev
    return out


def mixed_day(all_events):
    """A recorded day that has entries in a muted phase and in P2."""
    for d, ev in all_events.items():
        ph = {pr.phase_of(e.module) for e in ev if e.kind == 'ENTRY'}
        if 'P2' in ph and ph & {'P1', 'P3'}:
            return d, ev
    raise AssertionError('no recorded day with both a P2 entry and a P1/P3 entry')


def run_day(sess, ev):
    for e in ev:
        minute = e.minute if e.kind != 'EXIT' else e.data['trade']['m_exit']
        sess.dispatch([e], session_open(sess.date) + timedelta(minutes=minute + 1, seconds=7))


def test_muted_phases_post_nothing_but_are_still_recorded(tmp_path, monkeypatch, all_events):
    d, ev = mixed_day(all_events)
    names = {p: {s.name for s in C.MODULES.values() if s.phase == p} for p in ('P1', 'P2', 'P3')}
    quiet = names['P1'] | names['P3']

    loud, rec_all = make_session(tmp_path / 'all', d, monkeypatch)
    run_day(loud, ev)
    assert any(m in t for t in rec_all.texts for m in quiet) or any(' P1 ' in t or ' P3 ' in t for t in rec_all.texts)

    sess, rec = make_session(tmp_path / 'muted', d, monkeypatch, muted={'P1', 'P3'})
    run_day(sess, ev)
    assert rec.texts, 'P2 alerts must still be posted'
    for t in rec.texts:
        assert not any(m in t for m in quiet), t
        assert ' P1 ·' not in t and ' P3 ·' not in t
    p2_entries = [e for e in ev if e.kind == 'ENTRY' and e.module in names['P2']]
    assert sum(1 for t in rec.texts if 'APEX-CHRONOS ENTRY' in t) == len(p2_entries)
    assert len(rec.texts) < len(rec_all.texts)

    # every entry, muted or not, is in the sent-alert ledger, so the evening reconcile still matches all of them
    sent = state.load_sent(sess.cfg.state_dir, sess.date)
    entry_keys = [k for k in sent if '|ENTRY|' in k]
    assert len(entry_keys) == sum(1 for e in ev if e.kind == 'ENTRY')
    muted_keys = [k for k in entry_keys if sent[k].get('muted')]
    assert muted_keys and all(sent[k]['posted'] is False and k.split('|')[3] in quiet for k in muted_keys)
    assert all(k.split('|')[3] in names['P2'] for k in entry_keys if not sent[k].get('muted'))
    assert all(pos[1] in names['P2'] for pos in sess.posted_entries)

    # a restart neither re-posts P2 nor wakes the muted ones
    again, rec2 = make_session(tmp_path / 'muted', d, monkeypatch, muted={'P1', 'P3'})
    run_day(again, ev)
    assert rec2.texts == []


def test_an_entry_posted_before_the_mute_still_gets_its_exit(tmp_path, monkeypatch, all_events):
    d, ev = mixed_day(all_events)
    quiet = {s.name for s in C.MODULES.values() if s.phase in ('P1', 'P3')}
    entry = next(e for e in ev if e.kind == 'ENTRY' and e.module in quiet)
    exit_ = next(e for e in ev if e.kind == 'EXIT' and e.module == entry.module and e.ticker == entry.ticker
                 and e.data['trade']['m_entry'] == entry.minute)
    sess, rec = make_session(tmp_path, d, monkeypatch)
    sess.dispatch([entry], session_open(sess.date) + timedelta(minutes=entry.minute + 1, seconds=7))
    assert len(rec.texts) == 1
    later, rec2 = make_session(tmp_path, d, monkeypatch, muted={'P1', 'P3'})        # muted mid-day, after the entry went out
    later.dispatch([exit_], session_open(later.date) + timedelta(minutes=exit_.data['trade']['m_exit'] + 1, seconds=7))
    assert len(rec2.texts) == 1 and 'close the' in rec2.texts[0]


def test_default_settings_mute_nothing_and_env_is_read(monkeypatch):
    monkeypatch.delenv('MUTED_PHASES', raising=False)
    assert runner.Settings.from_env().muted_phases == frozenset()
    monkeypatch.setenv('MUTED_PHASES', 'P1,P3')
    assert runner.Settings.from_env().muted_phases == {'P1', 'P3'}
    monkeypatch.setenv('MUTED_PHASES', 'none')
    assert runner.Settings.from_env().muted_phases == frozenset()


def test_p2_mutes_itself_once_its_rule_says_retire(tmp_path, monkeypatch, all_events):
    d, ev = mixed_day(all_events)
    stopped = [dict(r, direction=1) for r in p2_sample([-0.20] * 5)]
    sess, rec = make_session(tmp_path, d, monkeypatch, muted={'P1', 'P3'}, ledger_rows=stopped)
    assert sess.p2_retired and sess.muted == {'P1', 'P2', 'P3'}
    run_day(sess, ev)
    assert not any('APEX-CHRONOS ENTRY' in t for t in rec.texts)
    collecting = [dict(r, direction=1) for r in p2_sample([0.05] * 5)]
    sess, _ = make_session(tmp_path / 'b', d, monkeypatch, muted={'P1', 'P3'}, ledger_rows=collecting)
    assert not sess.p2_retired and sess.muted == {'P1', 'P3'}


# ---- one trigger per job -----------------------------------------------------------------------------------------
def _on(name):
    wf = yaml.safe_load((ROOT / '.github' / 'workflows' / name).read_text())
    return wf, wf.get(True, wf.get('on'))


def test_session_has_one_trigger_and_eod_one_backup():
    wf, on = _on('chronos-session.yml')
    assert 'schedule' not in on and 'workflow_dispatch' in on
    assert {'scheduled_for_et', 'trigger'} <= set(on['workflow_dispatch']['inputs'])      # what the Worker sends
    env = wf['jobs']['session']['steps'][-2]['env']
    assert env['MUTED_PHASES'] == "${{ vars.MUTED_PHASES || 'P1,P3' }}"
    wf, on = _on('chronos-eod.yml')
    assert [c['cron'] for c in on['schedule']] == ['25 21 * * 1-5']
    assert '--skip-if-done' in wf['jobs']['eod']['steps'][-1]['run']


def test_eod_backup_only_skips_a_day_that_was_reconciled_and_posted(tmp_path):
    d = date(2026, 11, 12)
    sd = tmp_path / 'state'
    sd.mkdir()
    assert not runner.eod_already_done(str(sd), d)
    (sd / f'reconcile-{d}.json').write_text(json.dumps({'date': str(d)}))                 # written, summary not confirmed
    assert not runner.eod_already_done(str(sd), d)
    (sd / f'reconcile-{d}.json').write_text(json.dumps({'date': str(d), 'summary_posted': True}))
    assert runner.eod_already_done(str(sd), d)
    cfg = runner.Settings(state_dir=str(sd), ledger_dir=str(tmp_path / 'ledger'), dry_run=True)
    assert runner.run_eod(cfg, d, skip_if_done=True) == 0                                  # returns before any network use
