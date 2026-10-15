from datetime import date, datetime

from apex_chronos import calendar_nyse as cal


def test_holidays_weekends_and_early_closes_are_not_traded():
    assert not cal.is_full_session(date(2026, 11, 26))      # Thanksgiving
    assert not cal.is_full_session(date(2026, 11, 28))      # Saturday
    assert cal.is_trading_day(date(2026, 11, 27)) and not cal.is_full_session(date(2026, 11, 27))   # 1pm close: never backtested
    assert not cal.is_full_session(date(2026, 12, 25)) and not cal.is_full_session(date(2027, 6, 18))
    assert cal.is_full_session(date(2026, 10, 15))


def test_previous_sessions_skip_non_trading_days():
    prev = cal.prev_full_sessions(date(2026, 11, 30), 3)
    assert prev == [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]    # skips the 27th (early close) and the 26th (holiday)


def test_session_clock_labels():
    assert cal.minute_clock(0) == '09:30' and cal.minute_clock(389) == '15:59' and cal.bar_close_clock(389) == '16:00'
    op = cal.session_open(date(2026, 10, 15))
    assert op.hour == 9 and op.minute == 30 and op.utcoffset().total_seconds() == -4 * 3600     # EDT
    assert cal.session_open(date(2026, 12, 1)).utcoffset().total_seconds() == -5 * 3600          # EST
