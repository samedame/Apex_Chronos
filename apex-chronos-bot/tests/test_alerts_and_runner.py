from datetime import timedelta

import pytest

from apex_chronos import alerts, config as C, replay, runner, state
from apex_chronos.calendar_nyse import session_open


def events_for(days, date_str):
    dts, by_day = days
    eng, ev = replay.run_session(date_str, by_day[date_str], list(C.TICKERS), mode='fast')
    return ev


def first(ev, kind, pred=lambda e: True):
    return next(e for e in ev if e.kind == kind and pred(e))


def busiest_date(fast_run):
    return fast_run[0].groupby('date').size().idxmax()


def test_entry_alerts_for_every_structure(days, fast_run):
    seen = set()
    for d in sorted(fast_run[0]['date'].unique()):
        for e in events_for(days, d):
            if e.kind == 'ENTRY' and e.data['structure'] not in seen:
                seen.add(e.data['structure'])
                text = alerts.fmt_entry(d, e, capital=25000, slot_fraction=0.25)
                assert e.ticker in text and 'MODEL' in text and 'TODAY (0DTE)' in text and len(text) < alerts.DISCORD_LIMIT
                assert 'contract(s)' in text
                if e.data['structure'] == 'credit_spread':
                    assert 'collateral' in text and 'buy the spread back' in text
                else:
                    assert 'sell HALF' in text
    assert seen == {'single', 'debit_spread', 'credit_spread'}


def test_sizing_helpers():
    assert alerts.contracts_for(1.00, 25000, 0.25) == 62
    assert alerts.contracts_for(1.00, None, 0.25) is None
    assert 'ACCOUNT_CAPITAL' in alerts._sizing_line(2.0, None, 0.25)
    assert 'less than one contract' in alerts._sizing_line(30.0, 5000, 0.25)


def test_long_messages_are_split_without_loss():
    text = '\n'.join(f'line {i} ' + 'x' * 80 for i in range(60))
    parts = alerts._split(text)
    assert len(parts) > 1 and all(len(p) <= alerts.DISCORD_LIMIT for p in parts)
    assert ''.join(parts).replace('\n', '') == text.replace('\n', '')


def test_dry_run_never_touches_the_network():
    assert alerts.post_discord('https://discord.invalid/x', 'hello', dry_run=True) is True
    assert alerts.post_discord(None, 'hello') is False


class Recorder:
    def __init__(self):
        self.texts = []

    def __call__(self, webhook, text, dry_run=False, retries=4):
        self.texts.append(text)
        return True


@pytest.fixture
def busy(days, fast_run):
    d = busiest_date(fast_run)
    return d, events_for(days, d)


def make_session(tmp_path, d, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(alerts, 'post_discord', rec)
    cfg = runner.Settings(state_dir=str(tmp_path / 'state'), ledger_dir=str(tmp_path / 'ledger'), webhook='https://x', dry_run=False)
    from datetime import date
    return runner.LiveSession(cfg, date.fromisoformat(d)), rec


def test_each_event_is_alerted_once_even_across_restarts(tmp_path, monkeypatch, busy):
    d, ev = busy
    sess, rec = make_session(tmp_path, d, monkeypatch)
    now = session_open(sess.date) + timedelta(minutes=391, seconds=1)
    # entries are 'late' relative to `now`, so judge freshness per event: dispatch each at its own bar close
    n_first = 0
    for e in ev:
        minute = e.minute if e.kind != 'EXIT' else e.data['trade']['m_exit']
        n_first += sess.dispatch([e], session_open(sess.date) + timedelta(minutes=minute + 1, seconds=7))
    sent_first = len(rec.texts)
    assert sent_first > 0 and n_first > 0
    for e in ev:                                                       # the same minutes seen again (e.g. a catch-up replay)
        sess.dispatch([e], now)
    assert len(rec.texts) == sent_first
    sess2, _ = make_session(tmp_path, d, monkeypatch)                  # a fresh process reading the saved ledger
    sess2.dispatch(ev, now)
    assert len(rec.texts) == sent_first
    assert any('ENTRY' in t for t in rec.texts) and any(k in t for t in rec.texts for k in ('STOP', 'PROFIT-TRAIL', 'TIME STOP'))


def test_a_stale_entry_is_not_actionable_and_its_exits_stay_silent(tmp_path, monkeypatch, busy):
    d, ev = busy
    sess, rec = make_session(tmp_path, d, monkeypatch)
    late = session_open(sess.date) + timedelta(minutes=391)             # hours after the signals
    sess.dispatch(ev, late)
    n_entries = sum(1 for e in ev if e.kind == 'ENTRY')
    assert n_entries > 0 and len(rec.texts) == n_entries and all(t.startswith('⚪ Missed entry (stale)') for t in rec.texts)
    assert not any('TP1' in t or 'STOP' in t or 'TIME STOP' in t or 'PROFIT-TRAIL' in t for t in rec.texts)
    assert sess.posted_entries == set()


def test_ledger_rewrite_is_idempotent(tmp_path, fast_run):
    trades = fast_run[0]
    from datetime import date
    d = date.fromisoformat(trades['date'].iloc[0])
    day = trades[trades['date'] == d.isoformat()].to_dict('records')
    state.replace_day_in_ledger(str(tmp_path), d, day)
    state.replace_day_in_ledger(str(tmp_path), d, day)
    rows = state.read_ledger(str(tmp_path))
    assert len(rows) == len(day)
    assert [r['ticker'] for r in rows] == [t['ticker'] for t in sorted(day, key=lambda t: (t['m_entry'], t['ticker']))]
