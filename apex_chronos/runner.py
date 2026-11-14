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
from pathlib import Path
from typing import Dict, List, Optional, Set

import numpy as np
import pandas as pd
import requests

from . import alert_confidence, alerts, config as C, data, history, phase_review, state, verdict
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
    slot_fraction: float = 0.25                # production default is verdict.DEFAULT_SLOT_FRACTION (10%), set in from_env()
    entry_grace_s: float = 150.0
    stale_after_s: float = 600.0
    poll_delay_s: float = 6.0
    max_runtime_min: Optional[float] = None
    tickers: List[str] = field(default_factory=lambda: list(C.TICKERS))
    cash_controls_enabled: bool = False
    muted_phases: frozenset = frozenset()      # phases whose alerts are not posted; they are still evaluated and logged
    # 2026-11-14 verdict controls (docs/VERDICT_2026-11-14.md): P1/P3 retired from alerts, P2 alerted only when a
    # same-day expiry is listed, kill switch, P2 fair-vol grading and the raw-bar archive. On in production (from_env);
    # off by default here so the verification harness and legacy tests exercise the unchanged engine and alerts.
    verdict_controls: bool = False

    @classmethod
    def from_env(cls) -> 'Settings':
        def f(name, default=None):
            v = os.environ.get(name, '').strip()
            return float(v) if v else default
        enabled = os.environ.get('CASH_CONTROLS_ENABLED', '').strip().lower()
        if enabled not in ('', '0', 'false', 'no', '1', 'true', 'yes'):
            raise ValueError('CASH_CONTROLS_ENABLED must be explicitly true or false')
        return cls(
            state_dir=os.environ.get('APEX_STATE_DIR', 'state'),
            ledger_dir=os.environ.get('APEX_LEDGER_DIR', 'ledger'),
            webhook=os.environ.get('DISCORD_WEBHOOK_URL') or None,
            dry_run=os.environ.get('DRY_RUN', '').lower() in ('1', 'true', 'yes'),
            capital=f('ACCOUNT_CAPITAL'),
            slot_fraction=f('SLOT_FRACTION', verdict.DEFAULT_SLOT_FRACTION),
            entry_grace_s=f('ENTRY_GRACE_SECONDS', 150.0),
            poll_delay_s=f('POLL_DELAY_SECONDS', 6.0),
            cash_controls_enabled=enabled in ('1', 'true', 'yes'),
            muted_phases=phase_review.parse_muted(os.environ.get('MUTED_PHASES')),
            verdict_controls=True,
        )

    @property
    def retired_phases(self) -> frozenset:
        return verdict.RETIRED_PHASES if self.verdict_controls else frozenset()


# ---------------------------------------------------------------------------------------------
class LiveSession:
    def __init__(self, cfg: Settings, d: date):
        self.cfg = cfg
        self.date = d
        self.http = requests.Session()
        self.ctxs = {}
        self.excluded: Dict[str, str] = {}
        self.engine = SessionEngine(d.isoformat(), cfg.tickers, n_bars=C.SESSION_BARS,
                                    protect_degraded=cfg.cash_controls_enabled)
        self.sent = state.load_sent(cfg.state_dir, d)
        self.posted_entries: Set[tuple] = {tuple(v['pos']) for v in self.sent.values() if v.get('pos') and v.get('posted')}
        self.last = -1
        self.feed_fail = 0
        self.feed_alerted = False
        self.vix_delay = 0
        self.muted = frozenset(cfg.muted_phases)
        self.p2_retired = False
        try:
            # The P2 rule enforces itself: once the ledger says RETIRE, P2 alerts stop without anyone editing a setting.
            self.p2_retired = phase_review.p2_is_retired(state.read_ledger(cfg.ledger_dir))
        except Exception as e:                                 # noqa: BLE001 - a broken ledger must not stop the session
            print(f'P2 review unavailable ({e!r}); P2 alerts stay as configured')
        if self.p2_retired:
            self.muted = self.muted | {phase_review.RULE['phase']}
        # Retired phases are muted in code: no repo variable can turn their alerts back on (they are still logged).
        self.muted = self.muted | cfg.retired_phases
        self.kill = verdict.load_kill_switch(cfg.state_dir) if cfg.verdict_controls else {'tripped': False, 'reasons': []}
        if self.kill.get('tripped'):
            self.muted = self.muted | frozenset(C.PHASE_LIMITS)
        self.listing: Dict[tuple, str] = {}

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
        if self.muted:
            print(f"muted phases (evaluated and logged, not alerted): {', '.join(sorted(self.muted))}"
                  + (' - P2 by its own retire rule' if self.p2_retired else ''))
        if self.kill.get('tripped'):
            self._post("🛑 **APEX-CHRONOS kill switch tripped** - " + '; '.join(self.kill.get('reasons', [])) +
                       ". No alerts today; every phase is still recorded in the ledger. It clears by itself at the first "
                       "evening review where neither trigger holds.", key='kill-switch')
        if self.p2_retired and phase_review.RULE['phase'] not in self.cfg.muted_phases:
            self._post("⚪ **APEX-CHRONOS: P2 alerts are now muted** - the Phase 2 decision rule in docs/P2_DECISION.md reached "
                       "RETIRE. All three phases are still recorded in the ledger every evening.", key='p2-retired')
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

    def _confidence(self, ev, late: float, contracts: Optional[int] = None):
        """Score an entry alert. A scoring failure must never suppress an actionable alert, so it returns None instead of raising."""
        try:
            cb = ev.data['cost_basis']
            return alert_confidence.score_alert({
                'module': ev.module,
                'ticker': ev.ticker,
                'late_s': late,
                'data_age_s': self.vix_delay * 60.0,          # vix_delay is in minutes
                'contracts': contracts if contracts is not None else alerts.contracts_for(cb, self.cfg.capital, self.cfg.slot_fraction),
            }, grace_s=self.cfg.entry_grace_s)
        except Exception as e:                                 # noqa: BLE001
            print(f'confidence scoring failed for {ev.ticker} {ev.module}: {e!r}')
            return None

    def _dispatch_cash(self, ev: Event, now: datetime, key: str, late: float) -> int:
        """Explicit opt-in only. No exception can fall back to an unguarded entry."""
        from .cash_controls import CashBook, CashControlError
        if key in self.sent:
            return 0
        try:
            if self.cfg.dry_run:
                raise CashControlError('dry run cannot reserve real account funds')
            book = CashBook(Path(self.cfg.state_dir) / 'cash-controls.enc')
            if ev.kind == 'ENTRY':
                if now < session_open(self.date) + timedelta(minutes=ev.minute + 1) or late > self.cfg.entry_grace_s:
                    raise CashControlError('entry is outside the fresh-alert window')
                reservation = book.reserve(key, ev, now, catalog_dir=Path(self.cfg.state_dir) / 'cash_options',
                                           slot_fraction=self.cfg.slot_fraction)
                self._cash_delivery_barrier(book, key, key)
                conf = self._confidence(ev, late, contracts=reservation['quantity'])
                text = alerts.fmt_entry(str(self.date), ev, self.cfg.capital, self.cfg.slot_fraction,
                                        late, self.cfg.entry_grace_s, confidence=conf.lines() if conf else None,
                                        cash_reservation=reservation,
                                        listing=self.listing.get((ev.ticker, ev.module, ev.minute)))
                pos = (ev.ticker, ev.module, ev.minute)
                ok = self._post(text, key=key, pos=pos, posted=True, late=late, conf=conf.score if conf else None)
                if ok:
                    self.posted_entries.add(pos)
                return int(ok)
            if ev.kind in ('TP1', 'EXIT'):
                minute = ev.data['m_entry'] if ev.kind == 'TP1' else ev.data['trade']['m_entry']
                identity = f'{self.date}|ENTRY|{ev.ticker}|{ev.module}|{minute}'
                qty = book.request_exit(identity, key, ev.kind)
                self._cash_delivery_barrier(book, identity, key)
                text = (f"**APEX-CHRONOS cash exit signal** {ev.ticker}: {ev.kind} — sell up to {qty} confirmed whole "
                        f"contract(s). Reconcile actual sale fills before proceeds become settled funds. "
                        f"Reservation: {identity}; exit reference: {key}. MODEL signal, not a broker fill.")
                return int(self._post(text, key=key, posted=False))
        except Exception as error:
            # Stable messages only: never print private ledger/fill/provider data.
            reason = str(error) if isinstance(error, CashControlError) else 'cash controls unavailable; reconciliation required'
            self._post(f'⚪ Cash action blocked: {ev.ticker} {ev.kind}: {reason}. '
                       'Reconcile orders and manage any actual holding manually.', key=key, posted=False)
        return 0

    def _cash_delivery_barrier(self, book, identity, key):
        from .cash_controls import CashControlError
        if not book.claim_delivery(identity, key):
            raise CashControlError('cash alert delivery already attempted; reconcile before retrying')
        if os.environ.get('APEX_GIT_PUSH') == '1':
            if not state.git_commit_push([str(book.path)], f'apex-chronos {self.date} private cash reservation'):
                raise CashControlError('cash reservation could not be published durably; action blocked')
        elif os.environ.get('GITHUB_ACTIONS') == 'true':
            raise CashControlError('durable cash-book publishing is required on an ephemeral runner')

    def _unlisted(self, ev: Event, key: str, late: float, now: datetime) -> bool:
        """True if this ENTRY has no same-day expiry listed for its ticker, so it must not be alerted.

        The engine and the ledger are untouched (the trade is still recorded); only the Discord alert is withheld,
        and its TP1/EXIT stay silent because the entry was never posted.
        """
        if not self.cfg.verdict_controls or ev.kind != 'ENTRY':
            return False
        pos = (ev.ticker, ev.module, ev.minute)
        listed, source = verdict.listed_same_day(ev.ticker, self.date, now, Path(self.cfg.state_dir) / 'cash_options')
        self.listing[pos] = source
        if listed:
            return False
        if key not in self.sent:
            self._post(f"⚪ {ev.ticker} {ev.module} at {minute_clock(ev.minute)}: no same-day expiry listed for {ev.ticker} "
                       f"today ({source}) - not actionable, recorded in the paper ledger only.", key=key, pos=pos,
                       posted=False, late=late)
            if key in self.sent:
                self.sent[key]['unlisted'] = True
                state.save_sent(self.cfg.state_dir, self.date, self.sent)
        return True

    def _silenced(self, ev: Event, key: str, late: float) -> bool:
        """True if this event belongs to a muted phase and must not reach Discord.

        A muted ENTRY is written to the sent-alert ledger (posted=False, muted=True) so the evening reconcile still
        counts it as seen; its TP1/EXIT are dropped with it. An entry that WAS posted always gets its TP1 and EXIT,
        even if its phase is muted later the same day. No cash is reserved for a muted entry.
        """
        if phase_review.phase_of(ev.module) not in self.muted:
            return False
        if ev.kind == 'ENTRY':
            if key not in self.sent:
                self.sent[key] = {'t': now_et().isoformat(timespec='seconds'), 'posted': False, 'muted': True,
                                  'late_s': round(late, 1), 'pos': [ev.ticker, ev.module, ev.minute]}
                state.save_sent(self.cfg.state_dir, self.date, self.sent)
                print(f"muted (logged only): {ev.ticker} {ev.module} bar {minute_clock(ev.minute)}")
            return True
        if ev.kind in ('TP1', 'EXIT'):
            m_entry = ev.data['m_entry'] if ev.kind == 'TP1' else ev.data['trade']['m_entry']
            return (ev.ticker, ev.module, m_entry) not in self.posted_entries
        return ev.kind == 'SKIP'

    def dispatch(self, events: List[Event], now: datetime) -> int:
        n = 0
        for ev in events:
            key = f"{self.date.isoformat()}|{ev.key}"
            close_dt = session_open(self.date) + timedelta(minutes=ev.minute + 1)
            late = max(0.0, (now - close_dt).total_seconds())
            if self._silenced(ev, key, late):
                continue
            if self._unlisted(ev, key, late, now):
                continue
            if self.cfg.cash_controls_enabled and ev.kind in ('ENTRY', 'TP1', 'EXIT'):
                n += self._dispatch_cash(ev, now, key, late)
                continue
            if ev.kind == 'ENTRY':
                pos = (ev.ticker, ev.module, ev.minute)
                if late > self.cfg.stale_after_s:
                    text = (f"⚪ Missed entry (stale): {ev.ticker} {ev.data['legs']} · bar {minute_clock(ev.minute)} ET · "
                            f"{late / 60:.0f} min old - NOT actionable; the bot still tracks it in the paper ledger.")
                    self._post(text, key=key, pos=pos, posted=False, late=late)
                else:
                    conf = self._confidence(ev, late)
                    text = alerts.fmt_entry(self.date.isoformat(), ev, self.cfg.capital, self.cfg.slot_fraction, late, self.cfg.entry_grace_s,
                                            confidence=conf.lines() if conf else None,
                                            listing=self.listing.get((ev.ticker, ev.module, ev.minute)))
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
        if cfg.cash_controls_enabled and (sb.holes or sb.last_real != C.SESSION_BARS - 1
                or not all(np.isfinite(a).all() for a in (sb.O, sb.H, sb.L, sb.C, sb.V))
                or any(np.any(a <= 0) for a in (sb.O, sb.H, sb.L, sb.C))):
            excluded[t] = f'final bars unavailable ({sb.reason or len(sb.C)})'
            continue
        sb = data.pad_to(sb, C.SESSION_BARS)
        vix, stale = data.align_vix(vix_df, d, len(sb.C))
        if cfg.cash_controls_enabled and (stale > C.VIX_MAX_STALE_MIN or not np.isfinite(vix).all()):
            excluded[t] = 'final volatility data unavailable or stale'
            continue
        days[t] = TickerDay(ctxs[t], sb.O, sb.H, sb.L, sb.C, sb.V, vix)
    return days, excluded


def eod_already_done(state_dir: str, d: date) -> bool:
    """True if this session was reconciled AND its summary reached Discord (or was a dry run)."""
    import json
    try:
        with open(os.path.join(state_dir, f'reconcile-{d.isoformat()}.json')) as fh:
            return bool(json.load(fh).get('summary_posted'))
    except (OSError, ValueError):
        return False


def run_eod(cfg: Settings, d: Optional[date] = None, skip_if_done: bool = False) -> int:
    from .replay import run_session
    d = d or now_et().date()
    if not is_full_session(d):
        print(f'{d}: not a full session; nothing to reconcile')
        return 0
    if skip_if_done and eod_already_done(cfg.state_dir, d):
        print(f'{d}: already reconciled and posted by an earlier run; this backup run has nothing to do')
        return 0
    http = requests.Session()
    days, excluded = final_day_inputs(http, cfg, d)
    if not days:
        alerts.post_discord(cfg.webhook, f'🔴 APEX-CHRONOS EOD {d}: no final bars available - cannot reconcile.', cfg.dry_run)
        return 1
    protected = {'engine_kwargs': {'protect_degraded': True}} if cfg.cash_controls_enabled else {}
    eng, final_events = run_session(d.isoformat(), days, cfg.tickers, mode='fast', **protected)
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
    if cfg.verdict_controls:
        try:
            verdict.save_bars(cfg.state_dir, d, days)
        except Exception as e:                                 # noqa: BLE001 - the archive must never block the reconcile
            print(f'bar archive failed for {d}: {e!r}')
        try:
            verdict.write_grading(cfg.ledger_dir, d, verdict.grade_p2_day(
                d, days, trades, Path(cfg.state_dir) / 'cash_options', session_open(d), state_dir=cfg.state_dir))
        except Exception as e:                                 # noqa: BLE001
            print(f'P2 grading failed for {d}: {e!r}')
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
    if rows:
        try:
            muted = set(cfg.muted_phases) | set(cfg.retired_phases)
            if phase_review.p2_is_retired(rows):
                muted.add(phase_review.RULE['phase'])
            lines += phase_review.eod_lines(rows, frozenset(muted))
        except Exception as e:                                 # noqa: BLE001 - the summary must still go out
            print(f'phase review failed for {d}: {e!r}')
    if rows and cfg.verdict_controls:
        try:
            lines += verdict.grading_lines(cfg.ledger_dir)
            ks = verdict.kill_switch_status(rows, cfg.slot_fraction, alerted_phases=frozenset(C.PHASE_LIMITS) - cfg.retired_phases)
            verdict.save_kill_switch(cfg.state_dir, ks)
            lines.append(verdict.kill_switch_line(ks))
        except Exception as e:                                 # noqa: BLE001 - the summary must still go out
            print(f'verdict controls failed for {d}: {e!r}')
    if excluded:
        lines.append(f"Excluded today: {excluded}")
    rec['summary_posted'] = bool(alerts.post_discord(cfg.webhook, '\n'.join(lines), cfg.dry_run))
    with open(os.path.join(cfg.state_dir, f'reconcile-{d.isoformat()}.json'), 'w') as fh:
        json.dump(rec, fh, indent=1)
    try:
        from . import shadow
        report = shadow.compare(
            d.isoformat(), days, cfg.tickers, eng, rows,
            capital=cfg.capital if cfg.capital is not None else 1500.0,
            slot_fraction=cfg.slot_fraction, ledger_dir=cfg.ledger_dir, baseline_events=final_events,
        )
        report['excluded_tickers'] = excluded
        try:
            from . import cash_shadow
            report['cash_only'] = cash_shadow.evaluate(
                d.isoformat(), days, cfg.tickers, capital=1500.0, slot_fraction=0.25,
                ledger_dir=cfg.ledger_dir, catalog_dir=Path(cfg.state_dir) / 'cash_options',
            )
        except Exception as cash_error:
            report['cash_only'] = {'date': d.isoformat(), 'status': 'error', 'error': str(cash_error)}
            print(f'cash-only shadow failed for {d}: {cash_error!r}')
        try:
            from . import quote_trial
            report['cash_quote_trial'] = quote_trial.daily_attachment(cfg.ledger_dir, d.isoformat())
        except Exception as trial_error:
            report['cash_quote_trial'] = {'date': d.isoformat(), 'status': 'error', 'error': str(trial_error)}
            print(f'quote trial status failed for {d}: {type(trial_error).__name__}')
        shadow.save_report(cfg.ledger_dir, report)
    except Exception as e:
        print(f'shadow comparison failed for {d}: {e!r}')
    state.git_commit_push([cfg.state_dir, cfg.ledger_dir], f'apex-chronos {d} end-of-day reconcile')
    return 0
