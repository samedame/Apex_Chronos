import numpy as np
import pytest

from apex_chronos import config as C, replay
from apex_chronos.engine import SessionEngine
from apex_chronos.features import compute_features


def features(days, session):
    return {t: compute_features(d.ctx, d.O, d.H, d.L, d.C, d.V, d.vix)
            for t, d in days[1][session].items()}


def test_known_degraded_pending_p1b_entry_is_rejected_without_consuming_a_slot(days):
    fs = features(days, '2026-09-17')
    engine = SessionEngine('2026-09-17', list(C.TICKERS))
    for m in range(35):
        events = engine.step(m, fs, {'AMD'} if m == 34 else frozenset())
    assert not any(e.kind == 'ENTRY' and e.ticker == 'AMD' for e in events)
    assert any(e.kind == 'SKIP' and e.ticker == 'AMD' and 'pending entry' in e.data['reason'] for e in events)
    assert not any(p.ticker == 'AMD' for p in engine.open)


def test_missing_or_nan_pending_bar_is_rejected(days):
    for missing in (True, False):
        fs = features(days, '2026-09-17')
        engine = SessionEngine('2026-09-17', list(C.TICKERS))
        for m in range(34):
            engine.step(m, fs)
        if missing:
            fs.pop('AMD')
        else:
            fs['AMD'].iv[34] = np.nan
        events = engine.step(34, fs)
        assert not any(e.kind == 'ENTRY' and e.ticker == 'AMD' for e in events)


def test_degraded_held_deadline_never_fabricates_or_backdates_an_exit(days):
    # Find an ordinary time-stop position; a degraded deadline must postpone
    # valuation until an actual subsequent observation, preserving the holding.
    for session in days[0]:
        _, baseline = replay.run_session(session, days[1][session], list(C.TICKERS), mode='fast')
        target = next((e for e in baseline if e.kind == 'EXIT' and e.data['trade']['exit_reason'] == 'time_stop'
                       and e.minute < 380), None)
        if target is None:
            continue
        fs = features(days, session)
        engine = SessionEngine(session, list(C.TICKERS))
        for m in range(target.minute + 1):
            events = engine.step(m, fs, {target.ticker} if m == target.minute else frozenset())
        assert not any(e.kind in ('EXIT', 'TP1') and e.ticker == target.ticker for e in events)
        assert any(p.ticker == target.ticker for p in engine.open)
        events = engine.step(target.minute + 1, fs)
        exit_event = next(e for e in events if e.kind == 'EXIT' and e.ticker == target.ticker)
        assert exit_event.minute == target.minute + 1
        return
    raise AssertionError('time-stop fixture not found')


@pytest.mark.parametrize('holes,last_real,stale', [([20], 389, 0), ([], 388, 0), ([], 389, 999)])
def test_eod_does_not_create_final_valuations_from_filled_or_stale_bars(tmp_path, monkeypatch, holes, last_real, stale):
    from datetime import date
    from apex_chronos import data, history, runner
    values = np.ones(390)
    bars = data.SessionBars('SPY', values, values, values, values, values, holes=holes, last_real=last_real)
    monkeypatch.setattr(data, 'fetch_1d', lambda *args: None)
    monkeypatch.setattr(data, 'fetch_1m_today', lambda *args: None)
    monkeypatch.setattr(data, 'to_grid', lambda *args: bars)
    monkeypatch.setattr(data, 'align_vix', lambda *args: (values, stale))
    monkeypatch.setattr(history, 'load_history', lambda *args, **kw: {})
    monkeypatch.setattr(history, 'build_contexts', lambda *args: ({'SPY': None}, {}))
    cfg = runner.Settings(state_dir=str(tmp_path), tickers=['SPY'])
    day, excluded = runner.final_day_inputs(None, cfg, date(2026, 11, 10))
    assert day == {} and 'SPY' in excluded
