import csv
import math
import os
from datetime import date, timedelta

import pytest

from apex_chronos import alert_confidence as AC, alerts, config as C, runner
from apex_chronos.calendar_nyse import session_open

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P1A, P2 = C.MODULES['P1A'].name, C.MODULES['P2'].name


def good(**kw):
    ctx = {'module': P2, 'ticker': 'SPY', 'late_s': 6.0, 'data_age_s': 15 * 60.0, 'contracts': 3}
    ctx.update(kw)
    return ctx


# ---------------------------------------------------------------------------------------------- scoring behaviour
def test_best_achievable_alert_scores_in_tier_a_and_is_bounded():
    c = AC.score_alert(good())
    assert 90 <= c.score <= 100 and c.tier == 'A' and c.execution == 100 and not c.capped and c.drag is None


def test_score_is_always_an_integer_in_1_to_100():
    for module in (P1A, P2, 'P1B', 'nonsense', None):
        for late in (None, 0, 100, 400, 599, 5000, -3, float('nan')):
            for contracts in (None, 0, 1, 5, 'x'):
                for verified in (None, True, False):
                    c = AC.score_alert({'module': module, 'ticker': 'AMD', 'late_s': late, 'data_age_s': 1e9,
                                        'contracts': contracts, 'expiry_verified': verified})
                    assert isinstance(c.score, int) and 1 <= c.score <= 100 and 0 <= c.setup <= 100 and 0 <= c.execution <= 100


def test_tiers_follow_the_cutoffs():
    assert [AC.TIERS[i][1] for i in range(4)] == ['A', 'B', 'C', 'D']
    assert AC.score_alert(good()).tier == 'A'
    assert AC.score_alert(good(ticker='AMD')).tier == 'B'          # unverified single-name expiry
    assert AC.score_alert(good(ticker='AMD', late_s=400.0)).tier in ('C', 'D')


def test_single_name_expiry_is_flagged_and_index_etfs_are_not():
    single = AC.score_alert(good(ticker='NVDA'))
    assert 'NOT verified' in single.drag and 'NVDA' in single.drag
    assert AC.score_alert(good(ticker='QQQ')).score > single.score
    assert AC.score_alert(good(ticker='NVDA', expiry_verified=True)).score == AC.score_alert(good()).score


def test_disqualifiers_cap_the_score_at_tier_d():
    for kw in ({'contracts': 0}, {'expiry_verified': False}, {'late_s': 600.0}, {'late_s': 4000.0}):
        c = AC.score_alert(good(**kw))
        assert c.capped and c.score <= AC.DISQUALIFIED_CAP and c.tier == 'D', kw
    # the "biggest drag" names what disqualified the alert, not a larger-looking but non-fatal factor
    assert 'less than one contract' in AC.score_alert(good(ticker='TSLA', contracts=0)).drag
    assert 'min old' in AC.score_alert(good(ticker='TSLA', late_s=700.0)).drag
    assert 'no same-day expiry' in AC.score_alert(good(ticker='TSLA', expiry_verified=False)).drag
    # a merely weak factor is NOT a disqualifier
    one = AC.score_alert(good(contracts=1))
    assert not one.capped and one.tier == 'A'


def test_unset_capital_is_not_scored_and_does_not_drag_every_alert():
    unset = AC.score_alert(good(contracts=None))
    assert 'sizing' not in [f.name for f in unset.factors]
    assert unset.score == AC.score_alert(good(contracts=3)).score


def test_missing_inputs_never_flatter_an_alert():
    base = AC.score_alert(good())
    for key in ('late_s', 'data_age_s'):
        c = AC.score_alert({k: v for k, v in good().items() if k != key})
        assert c.score < base.score, key
    assert AC.score_alert(good(late_s=float('nan'))).score < base.score


def test_lateness_and_stale_vix_only_ever_lower_the_score():
    prev = 101
    for late in (0, 150, 200, 300, 450, 599):
        s = AC.score_alert(good(ticker='AMD', late_s=late)).score
        assert s <= prev
        prev = s
    prev = 101
    for age_min in (10, 20, 24, 28, 30):
        s = AC.score_alert(good(data_age_s=age_min * 60.0)).score
        assert s <= prev
        prev = s
    # the normal ~15-20 minute Yahoo VIX lag is not penalised
    assert AC.score_alert(good(data_age_s=20 * 60.0)).score == AC.score_alert(good(data_age_s=60.0)).score


def test_grace_window_follows_the_runner_setting():
    assert AC.score_alert(good(late_s=200.0), grace_s=300.0).execution == 100
    assert AC.score_alert(good(late_s=200.0), grace_s=150.0).execution < 100


def test_setup_component_is_honest_about_thin_evidence():
    for short in ('P1B', 'P1C', 'P3'):
        c = AC.score_alert(good(module=C.MODULES[short].name))
        assert c.thin and 'too thin to rank' in c.record_text and c.setup == round(AC.THIN_SETUP * 100)
    for short in ('P1A', 'P2'):
        c = AC.score_alert(good(module=C.MODULES[short].name))
        assert not c.thin and 'A record, not a forecast' in c.record_text
    unknown = AC.score_alert(good(module='mystery'))
    assert unknown.thin and unknown.module == '?'


def test_setup_type_does_not_get_more_weight_than_the_evidence_supports():
    # the two established setups are within a few points of each other; the headline can differ by at most that share
    a = AC.score_alert(good(module=P1A)).score
    b = AC.score_alert(good(module=P2)).score
    assert abs(a - b) <= 5
    assert AC.SETUP_WEIGHT <= 0.15


def test_record_text_reports_both_samples():
    t = AC.score_alert(good(module=P1A)).record_text
    assert 'backtest 51%/47' in t and 'forward' in t and '75 tracked trades' in t


def test_lines_format_and_length():
    c = AC.score_alert(good(ticker='AMD', late_s=200.0))
    txt = c.lines()
    assert txt.startswith(f'Confidence {c.score}/100 (setup {c.setup} · execution {c.execution}) - tier {c.tier}')
    assert 'Biggest drag:' in txt and len(txt) < 520
    assert c.line() == txt


# ------------------------------------------------------------------------------ the evidence table is the real data
def _stats(rows):
    out = {}
    for r in rows:
        k, roi = r['mod'], float(r['roi'])
        n, w, s = out.get(k, (0, 0, 0.0))
        out[k] = (n + 1, w + (roi > 0), s + roi)
    return {k: (n, w, round(s / n, 4)) for k, (n, w, s) in out.items()}


def test_backtest_side_of_the_table_matches_the_bots_own_backtest(fast_run):
    short = {s.name: k for k, s in C.MODULES.items()}
    rows = [{'mod': short[r['module']], 'roi': r['roi']} for r in fast_run[0].to_dict('records')]
    assert _stats(rows) == {k: v['bt'] for k, v in AC.RECORD.items()}


def test_forward_side_of_the_table_matches_the_ledger_up_to_as_of():
    short = {s.name: k for k, s in C.MODULES.items()}
    with open(os.path.join(ROOT, 'ledger', 'trades.csv')) as fh:
        rows = [{'mod': short[r['module']], 'roi': r['roi']} for r in csv.DictReader(fh) if r['date'] <= AC.AS_OF]
    assert _stats(rows) == {k: v['fwd'] for k, v in AC.RECORD.items()}


# ------------------------------------------------------------------------------------------------ the live alert
def real_entries(days, fast_run, limit=None):
    from apex_chronos import replay
    out = []
    for d in sorted(fast_run[0]['date'].unique()):
        _, ev = replay.run_session(d, days[1][d], list(C.TICKERS), mode='fast')
        out += [(d, e) for e in ev if e.kind == 'ENTRY']
        if limit and len(out) >= limit:
            break
    return out


def test_every_real_entry_alert_carries_a_score_and_fits_in_one_discord_message(days, fast_run):
    seen = set()
    for d, e in real_entries(days, fast_run):
        contracts = alerts.contracts_for(e.data['cost_basis'], 25000, 0.25)
        c = AC.score_alert({'module': e.module, 'ticker': e.ticker, 'late_s': 8.0, 'data_age_s': 900.0, 'contracts': contracts})
        text = alerts.fmt_entry(d, e, capital=25000, slot_fraction=0.25, confidence=c.lines())
        assert f'Confidence {c.score}/100' in text and len(text) < alerts.DISCORD_LIMIT
        assert text.index('Confidence') < text.index('Signal:')
        seen.add(e.data['structure'])
    assert seen == {'single', 'debit_spread', 'credit_spread'}


def test_fmt_entry_without_a_score_is_unchanged(days, fast_run):
    d, e = real_entries(days, fast_run, limit=1)[0]
    assert 'Confidence' not in alerts.fmt_entry(d, e, capital=25000)


class Recorder:
    def __init__(self):
        self.texts = []

    def __call__(self, webhook, text, dry_run=False, retries=4):
        self.texts.append(text)
        return True


def make_session(tmp_path, d, monkeypatch, capital=None):
    rec = Recorder()
    monkeypatch.setattr(alerts, 'post_discord', rec)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'), webhook='https://x',
                          dry_run=False, capital=capital)
    return runner.LiveSession(cfg, date.fromisoformat(d)), rec


def entry_event(days, fast_run):
    d, e = real_entries(days, fast_run, limit=1)[0]
    return d, e


def on_time(sess, e):
    return session_open(sess.date) + timedelta(minutes=e.minute + 1, seconds=7)


def test_dispatch_posts_the_score_and_records_it_in_state(tmp_path, monkeypatch, days, fast_run):
    d, e = entry_event(days, fast_run)
    sess, rec = make_session(tmp_path, d, monkeypatch, capital=25000)
    sess.vix_delay = 16
    assert sess.dispatch([e], on_time(sess, e)) == 1
    assert len(rec.texts) == 1 and 'Confidence ' in rec.texts[0] and 'APEX-CHRONOS ENTRY' in rec.texts[0]
    saved = next(v for k, v in sess.sent.items() if '|ENTRY|' in k)
    assert isinstance(saved['conf'], int) and 1 <= saved['conf'] <= 100
    n = len(rec.texts)
    sess.dispatch([e], on_time(sess, e))                                   # dedupe is unaffected
    assert len(rec.texts) == n


def test_a_scoring_failure_never_suppresses_the_entry_alert(tmp_path, monkeypatch, days, fast_run):
    d, e = entry_event(days, fast_run)
    sess, rec = make_session(tmp_path, d, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError('scoring bug')
    monkeypatch.setattr(runner.alert_confidence, 'score_alert', boom)
    assert sess.dispatch([e], on_time(sess, e)) == 1
    assert len(rec.texts) == 1 and 'APEX-CHRONOS ENTRY' in rec.texts[0] and 'Confidence' not in rec.texts[0]
    assert 'conf' not in next(v for k, v in sess.sent.items() if '|ENTRY|' in k)


def test_a_stale_entry_is_still_not_scored_or_actionable(tmp_path, monkeypatch, days, fast_run):
    d, e = entry_event(days, fast_run)
    sess, rec = make_session(tmp_path, d, monkeypatch)
    sess.dispatch([e], session_open(sess.date) + timedelta(minutes=391))
    assert len(rec.texts) == 1 and rec.texts[0].startswith('⚪ Missed entry (stale)') and 'Confidence' not in rec.texts[0]


def test_tp1_and_exit_alerts_are_not_scored(days, fast_run):
    from apex_chronos import replay
    d = sorted(fast_run[0]['date'].unique())[0]
    _, ev = replay.run_session(d, days[1][d], list(C.TICKERS), mode='fast')
    for e in ev:
        if e.kind == 'TP1':
            assert 'Confidence' not in alerts.fmt_tp1(d, e)
        elif e.kind == 'EXIT':
            assert 'Confidence' not in alerts.fmt_exit(d, e)
