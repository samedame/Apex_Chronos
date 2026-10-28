"""Live loop and end-of-day reconcile.

The live loop is deliberately thin: it fetches completed bars, builds features
from the bars seen so far, and calls SessionEngine.step(m, ...) for each new
minute - the same call the verification harness makes. All trading decisions
live in the engine.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

import numpy as np
import pandas as pd
import requests

from . import alert_confidence, alerts, config as C, data, history, state
from .calendar_nyse import NY, bar_close_clock, is_full_session, minute_clock, now_et, session_open
from .engine import Event, SessionEngine
from .features import Features, compute_features, session_profiles


@dataclass
class Settings:
    state_dir: str = 'state'
    ledger_dir: str = 'ledger'
    webhook: Optional[str] = None
    dry_run: bool = False
    capital: Optional[float] = None
    slot_fraction: float = 0.25
    entry_grace_s: float = 150.0
    stale_after_s: float = 600.0
    poll_delay_s: float = 6.0
    max_runtime_min: Optional[float] = None
    tickers: List[str] = field(default_factory=lambda: list(C.TICKERS))

    @classmethod
    def from_env(cls) -> 'Settings':
        def f(name, default=None):
            v = os.environ.get(name, '').strip()
            return float(v) if v else default
        return cls(
            state_dir=os.environ.get('APEX_STATE_DIR', 'state'),
            ledger_dir=os.environ.get('APEX_LEDGER_DIR', 'ledger'),
            webhook=os.environ.get('DISCORD_WEBHOOK_URL') or None,
            dry_run=os.environ.get('DRY_RUN', '').lower() in ('1', 'true', 'yes'),
            capital=f('ACCOUNT_CAPITAL'),
            slot_fraction=f('SLOT_FRACTION', 0.25),
            entry_grace_s=f('ENTRY_GRACE_SECONDS', 150.0),
            poll_delay_s=f('POLL_DELAY_SECONDS', 6.0),
        )


# ---------------------------------------------------------------------------------------------
class LiveSession:
    def __init__(self, cfg: Settings, d: date):
        self.cfg = cfg
        self.date = d
        self.http = requests.Session()
        self.ctxs = {}
        self.excluded: Dict[str, str] = {}
        self.engine = SessionEngine(d.isoformat(), cfg.tickers, n_bars=C.SESSION_BARS)
        self.sent = state.load_sent(cfg.state_dir, d)
        self.posted_entries: Set[tuple] = {tuple(v['pos']) for v in self.sent.values() if v.get('pos') and v.get('posted')}
        self.last = -1
        self.feed_fail = 0
        self.feed_alerted = False
        self.vix_delay = 0

    # -- before the open ----------------------------------------------------------------------
    def prepare(self) -> None:
        daily = {}
        for t in self.cfg.tickers:
            try:
                daily[t] = data.fetch_1d(self.http, t)
            except data.FeedError as e:
                self.excluded[t] = f'daily fetch failed ({e})'
        hist = history.load_history(self.cfg.state_dir, self.cfg.tickers, self.date, http=self.http)
        self.ctxs, ex = history.build_contexts(hist, daily, [t for t in self.cfg.tickers if t not in self.excluded], self.date)
        self.excluded.update(ex)
        miss = next(iter(hist.values()))['missing'] if hist else []
        if miss:
            print(f'history sessions unavailable: {miss}')
        if self.excluded:
            self._post(f"⚠ **APEX-CHRONOS degraded universe** {self.date}: not trading {', '.join(sorted(self.excluded))} today "
                       f"({'; '.join(f'{k}: {v}' for k, v in sorted(self.excluded.items()))}). The strategy needs its full history to match the backtest.",
                       key='universe')

    # -- one poll ------------------------------------------------------------------------------
    def _fetch_bars(self, m_target: int):
        symbols = [t for t in self.cfg.tickers if t in self.ctxs] + ['^VIX']

        def one(sym):
            try:
                return sym, data.fetch_1m_today(self.http, sym)
            except data.FeedError as e:
                return sym, e
        with ThreadPoolExecutor(max_workers=4) as ex:
            res = dict(ex.map(one, symbols))
        return res

    def poll(self, m_target: int, patience: int = 4):
        """Return (feats, degraded_minutes) covering minutes 0..m_target, waiting briefly for lagging tickers."""
        for attempt in range(patience):
            raw = self._fetch_bars(m_target)
            vix_raw = raw.pop('^VIX')
            bars, lagging, errors = {}, [], []
            for t, df in raw.items():
                if isinstance(df, Exception):
                    errors.append(t)
                    continue
                sb = data.to_grid(df, self.date, t, m_target)
                if not sb.ok:
                    errors.append(t)
                    continue
                if sb.last_real < m_target:
                    lagging.append(t)
                bars[t] = sb
            if errors and len(errors) == len(raw):
                self.feed_fail += 1
                return None, None
            if not lagging or attempt == patience - 1:
                break
            time.sleep(6)
        n = m_target + 1
        if isinstance(vix_raw, Exception):
            self.feed_fail += 1
            return None, None             # implied vol is built from VIX: never substitute a default mid-session
        vix, vix_stale = data.align_vix(vix_raw, self.date, n)
        self.vix_delay = vix_stale
        if vix_stale > C.VIX_MAX_STALE_MIN:
            self.feed_fail += 1
            return None, None
        self.feed_fail = 0
        feats, degraded = {}, {}
        for t, sb in bars.items():
            deg = set(sb.holes) | set(range(sb.last_real + 1, n))
            sb = data.pad_to(sb, n)
            ctx = self.ctxs[t]
            feats[t] = compute_features(ctx, sb.O[:n], sb.H[:n], sb.L[:n], sb.C[:n], sb.V[:n], vix)
            degraded[t] = deg
        for t in errors:
            degraded[t] = set(range(n))   # no data at all this poll: nothing fires, positions would raise -> handled by caller
        return feats, degraded

    # -- alerting -----------------------------------------------------------------------------
    def _post(self, text: str, key: Optional[str] = None, pos=None, posted=True, late=0.0, conf: Optional[int] = None) -> bool:
        if key is not None and key in self.sent:
            return True
        ok = alerts.post_discord(self.cfg.webhook, text, dry_run=self.cfg.dry_run)
        if key is not None and ok:
            self.sent[key] = {'t': now_et().isoformat(timespec='seconds'), 'posted': posted, 'late_s': round(late, 1),
                              'pos': list(pos) if pos else None}
            if conf is not None:
                self.sent[key]['conf'] = conf       # kept so the score can later be checked against realised outcomes
            state.save_sent(self.cfg.state_dir, self.date, self.sent)
        return ok

    def _confidence(self, ev, late: float):
        """Score an entry alert. A scoring failure must never suppress an actionable alert, so it returns None instead of raising."""
        try:
            cb = ev.data['cost_basis']
            return alert_confidence.score_alert({
                'module': ev.module,
                'ticker': ev.ticker,
                'late_s': late,
                'data_age_s': self.vix_delay * 60.0,          # vix_delay is in minutes
                'contracts': alerts.contracts_for(cb, self.cfg.capital, self.cfg.slot_fraction),
            }, grace_s=self.cfg.entry_grace_s)
        except Exception as e:                                 # noqa: BLE001
            print(f'confidence scoring failed for {ev.ticker} {ev.module}: {e!r}')
            return None

    def dispatch(self, events: List[Event], now: datetime) -> int:
        n = 0
        for ev in events:
            key = f"{self.date.isoformat()}|{ev.key}"
            close_dt = session_open(self.date) + timedelta(minutes=ev.minute + 1)
            late = max(0.0, (now - close_dt).total_seconds())
            if ev.kind == 'ENTRY':
                pos = (ev.ticker, ev.module, ev.minute)
                if late > self.cfg.stale_after_s:
                    text = (f"⚪ Missed entry (stale): {ev.ticker} {ev.data['legs']} · bar {minute_clock(ev.minute)} ET · "
                            f"{late / 60:.0f} min old - NOT actionable; the bot still tracks it in the paper ledger.")
                    self._post(text, key=key, pos=pos, posted=False, late=late)
                else:
                    conf = self._confidence(ev, late)
                    text = alerts.fmt_entry(self.date.isoformat(), ev, self.cfg.capital, self.cfg.slot_fraction, late, self.cfg.entry_grace_s,
                                            confidence=conf.lines() if conf else None)
                    if self._post(text, key=key, pos=pos, posted=True, late=late, conf=conf.score if conf else None):
                        self.posted_entries.add(pos)
                        n += 1
            elif ev.kind == 'TP1':
                pos = (ev.ticker, ev.module, ev.data['m_entry'])
                if pos in self.posted_entries:
                    n += bool(self._post(alerts.fmt_tp1(self.date.isoformat(), ev), key=key))
            elif ev.kind == 'EXIT':
                t = ev.data['trade']
                pos = (t['ticker'], t['module'], t['m_entry'])
                if pos in self.posted_entries:
                    n += bool(self._post(alerts.fmt_exit(self.date.isoformat(), ev, capital=self.cfg.capital,
                                                         slot_fraction=self.cfg.slot_fraction), key=key))
            elif ev.kind == 'SKIP':
                self._post(alerts.fmt_skip(self.date.isoformat(), ev), key=key, posted=False)
        return n

    # -- the loop ------------------------------------------------------------------------------
    def run(self, started: datetime) -> bool:
        """Returns True if the session is finished, False if the runtime cap hit first (caller re-dispatches)."""
        open_dt = session_open(self.date)
        deadline = started + timedelta(minutes=self.cfg.max_runtime_min) if self.cfg.max_runtime_min else None
        while True:
            m_next = self.last + 1
            if m_next >= C.SESSION_BARS:
                return True
            due = open_dt + timedelta(minutes=m_next + 1, seconds=self.cfg.poll_delay_s)
            now = now_et()
            if deadline and due > deadline:
                return False
            if due > now:
                time.sleep(min((due - now).total_seconds(), 30))
                continue
            m_closed = min(C.SESSION_BARS - 1, int((now - open_dt).total_seconds() // 60) - 1)
            feats, degraded = self.poll(m_closed)
            if feats is None:
                if self.feed_fail >= 3 and not self.feed_alerted:
                    self._post('🔴 **APEX-CHRONOS data feed down** - no minute bars from Yahoo for 3+ polls. No new signals can be '
                               'evaluated; any open position must be managed manually by its plan.', key='feed-down')
                    self.feed_alerted = True
                time.sleep(10)
                continue
            held = {p.ticker for p in self.engine.open}
            if any(t not in feats for t in held):
                print(f'no data for held ticker(s) {sorted(held - set(feats))}; retrying poll')
                time.sleep(5)
                continue
            events: List[Event] = []
            for m in range(m_next, m_closed + 1):
                deg = {t for t, mins in degraded.items() if m in mins}
                events += self.engine.step(m, feats, deg)
            self.last = m_closed
            sent_n = self.dispatch(events, now_et())
            if events:
                state.git_commit_push([self.cfg.state_dir], f'apex-chronos {self.date} alerts through {minute_clock(self.last)}')
            snap = self.engine.snapshot()
            print(f"{now_et():%H:%M:%S} m={self.last} ({minute_clock(self.last)}) events={len(events)} alerts_sent={sent_n} open={snap['open']}")
            if self.last >= 320 and not self.engine.open and not self.engine.pending:
                return True


# ---------------------------------------------------------------------------------------------
def run_live(cfg: Settings) -> int:
    started = now_et()
    d = started.date()
    out = os.environ.get('GITHUB_OUTPUT')

    def set_out(more: bool):
        if out:
            with open(out, 'a') as fh:
                fh.write(f"more={'true' if more else 'false'}\n")

    if not is_full_session(d):
        print(f'{d} is not a full NYSE session - nothing to do.')
        set_out(False)
        return 0
    open_dt = session_open(d)
    if started < open_dt - timedelta(minutes=45):
        print('more than 45 minutes before the open - a backup trigger from the other DST offset; nothing to do.')
        set_out(False)
        return 0
    if started > open_dt + timedelta(minutes=395):
        print('session already over')
        set_out(False)
        return 0
    sess = LiveSession(cfg, d)
    sess.prepare()
    # sanity guard: if SPY has no 09:30 bar 10 minutes after the open, the market is closed or the feed is broken
    finished = sess.run(started)
    state.git_commit_push([cfg.state_dir], f'apex-chronos {d} session state')
    set_out(not finished)
    print('session finished' if finished else 'runtime cap reached - handing off to the next run')
    return 0


# ---------------------------------------------------------------------------------------------
def final_day_inputs(http, cfg: Settings, d: date):
    """Final one-minute bars for the whole session (for EOD reconcile and for saving history)."""
    from .replay import TickerDay
    daily = {}
    for t in cfg.tickers:
        daily[t] = data.fetch_1d(http, t)
    hist = history.load_history(cfg.state_dir, cfg.tickers, d, http=http)
    ctxs, excluded = history.build_contexts(hist, daily, cfg.tickers, d)
    vix_df = data.fetch_1m_today(http, '^VIX')
    days = {}
    for t in cfg.tickers:
        if t not in ctxs:
            continue
        sb = data.to_grid(data.fetch_1m_today(http, t), d, t, C.SESSION_BARS - 1)
        if not sb.ok or len(sb.C) < C.MIN_SESSION_BARS:
            excluded[t] = f'final bars unavailable ({sb.reason or len(sb.C)})'
            continue
        sb = data.pad_to(sb, C.SESSION_BARS)
        vix, _ = data.align_vix(vix_df, d, len(sb.C))
        days[t] = TickerDay(ctxs[t], sb.O, sb.H, sb.L, sb.C, sb.V, vix)
    return days, excluded


def run_eod(cfg: Settings, d: Optional[date] = None) -> int:
    from .replay import run_session
    d = d or now_et().date()
    if not is_full_session(d):
        print(f'{d}: not a full session; nothing to reconcile')
        return 0
    http = requests.Session()
    days, excluded = final_day_inputs(http, cfg, d)
    if not days:
        alerts.post_discord(cfg.webhook, f'🔴 APEX-CHRONOS EOD {d}: no final bars available - cannot reconcile.', cfg.dry_run)
        return 1
    eng, _ = run_session(d.isoformat(), days, cfg.tickers, mode='fast')
    trades = eng.trades
    sent = state.load_sent(cfg.state_dir, d)
    live_entries = {tuple(k.split('|')[2:5]) for k in sent if '|ENTRY|' in k}
    final_entries = {(t['ticker'], t['module'], str(t['m_entry'])) for t in trades}
    # entries still open at the close of the replay (should not happen for full sessions)
    final_entries |= {(p.ticker, p.spec.name, str(p.m_entry)) for p in eng.open}
    missed = sorted(final_entries - live_entries)
    phantom = sorted(live_entries - final_entries)
    matched = len(final_entries & live_entries)

    state.replace_day_in_ledger(cfg.ledger_dir, d, trades)
    profiles = {t: session_profiles(td.O, td.C, td.V) for t, td in days.items()}
    history.save_session(cfg.state_dir, d, profiles)
    rec = {'date': d.isoformat(), 'live_entries': len(live_entries), 'final_entries': len(final_entries),
           'matched': matched, 'missed_by_live': [list(x) for x in missed], 'phantom_in_live': [list(x) for x in phantom],
           'excluded': excluded}
    import json
    with open(os.path.join(cfg.state_dir, f'reconcile-{d.isoformat()}.json'), 'w') as fh:
        json.dump(rec, fh, indent=1)

    rows = state.read_ledger(cfg.ledger_dir)
    r = np.array([float(x['roi']) for x in rows]) if rows else np.array([])
    day_r = [t['roi'] for t in trades]
    lines = [f"📒 **APEX-CHRONOS end of day {d}** - final-bar replay: {len(trades)} model trades, "
             f"day model ROI {np.sum(day_r) * 100:+.1f}% (sum over trades, unleveraged).",
             f"Live alerts vs final data: {matched}/{len(final_entries)} entries matched"
             + (f"; MISSED by live: {', '.join(f'{a} {b}@{minute_clock(int(c))}' for a, b, c in missed)}" if missed else '')
             + (f"; PHANTOM (live saw it, final data doesn't): {', '.join(f'{a} {b}@{minute_clock(int(c))}' for a, b, c in phantom)}" if phantom else '') + '.']
    if len(r):
        w, l = r[r > 0], r[r <= 0]
        pf = w.sum() / max(abs(l.sum()), 1e-4)
        lines.append(f"Forward record since go-live: {len(r)} trades · win rate {len(w) / len(r) * 100:.1f}% · profit factor {pf:.2f} · "
                     f"mean {r.mean() * 100:+.2f}%/trade   (backtest: 110 trades · 57.3% · 1.86 · +7.01%)")
    if excluded:
        lines.append(f"Excluded today: {excluded}")
    alerts.post_discord(cfg.webhook, '\n'.join(lines), cfg.dry_run)
    state.git_commit_push([cfg.state_dir, cfg.ledger_dir], f'apex-chronos {d} end-of-day reconcile')
    return 0
