"""Live-session glue for the lab: schedules chain snapshots and cross-asset captures, and feeds P2 entries/exits to the quote tracker.

Everything here is observation-only and failure-proof: each public method swallows its own exceptions, nothing returns a value
the caller uses to change an alert, and the whole thing is off unless Settings.lab_enabled is true.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from .. import tradier, verdict
from ..calendar_nyse import NY, session_open
from . import crossasset, quote_ledger as QL, snapshots as SN

PRE_OPEN_CAPTURE_LABEL = '0925'
MIDDAY_CAPTURE_LABELS = {'1130': 120, '1330': 240}        # cross-asset refresh at these snapshot labels (minutes after the open)


class LabRuntime:
    def __init__(self, state_dir: str, d: date, tickers: List[str], http, post: Optional[Callable[[str, str], None]] = None,   # post(key, text)
                 snapshot_labels: Optional[List[str]] = None, quotes_fn=None):
        self.state_dir, self.date, self.tickers, self.http, self.post = state_dir, d, list(tickers), http, post
        self.labels = snapshot_labels or list(SN.SNAPSHOT_LABELS)
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.spots: Dict[str, float] = {}
        self.tracker = QL.QuoteTracker(state_dir, d, post=post, spot_lookup=self._spot)
        self._quotes_fn = quotes_fn or tradier.quotes

    # -- helpers -------------------------------------------------------------------------------------------------------
    def _spot(self, root: str) -> Optional[float]:
        try:
            q = self._quotes_fn([root]).get(root.upper()) or {}
            v = q.get('last') or q.get('bid')
            return float(v) if v else None
        except Exception:                                           # noqa: BLE001
            return None

    def _label_time(self, label: str) -> datetime:
        return datetime.combine(self.date, datetime.min.time(), tzinfo=NY).replace(hour=int(label[:2]), minute=int(label[2:]))

    # -- pre-open ----------------------------------------------------------------------------------------------------------
    def pre_open(self) -> None:
        """09:25 capture of vol/cross-asset symbols and the Cboe history refresh. Never raises."""
        try:
            snap = crossasset.capture(self.http, datetime.now(NY))
            crossasset.save_capture(self.state_dir, self.date, PRE_OPEN_CAPTURE_LABEL, snap)
        except Exception as e:                                      # noqa: BLE001
            print(f'lab pre-open capture failed: {type(e).__name__}')
        try:
            crossasset.refresh_cboe_history(self.http, self.state_dir)
        except Exception as e:                                      # noqa: BLE001
            print(f'lab Cboe history refresh failed: {type(e).__name__}')
        try:
            from . import events as EV
            days = [self.date + timedelta(days=i) for i in range(-1, 8)]
            syms = set(self.tickers) | set(EV.MEGACAPS)
            for kind in ('earnings', 'dividends'):
                EV.refresh_calendar(self.http, self.state_dir, kind, days, syms)
        except Exception as e:                                      # noqa: BLE001
            print(f'lab calendar refresh failed: {type(e).__name__}')

    # -- background schedule -------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name='lab-schedule', daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        pending = sorted(self.labels)
        while pending and not self.stop.is_set():
            label = pending[0]
            when = self._label_time(label)
            now = datetime.now(NY)
            if when <= now - timedelta(minutes=20):                  # a restarted session skips labels that are long past
                pending.pop(0)
                continue
            if when > now:
                self.stop.wait(min((when - now).total_seconds(), 30.0))
                continue
            pending.pop(0)
            try:
                r = SN.take_snapshot(self.state_dir, self.date, label, self.tickers)
                print(f'lab snapshot {label}: {sorted(r.get("roots", []) or [])} {r.get("skipped", "")}')
                if label in MIDDAY_CAPTURE_LABELS:
                    crossasset.save_capture(self.state_dir, self.date, label, crossasset.capture(self.http, datetime.now(NY), groups=('vol',)))
            except Exception as e:                                  # noqa: BLE001
                print(f'lab snapshot {label} failed: {type(e).__name__}')

    # -- event hooks -----------------------------------------------------------------------------------------------------------
    def on_event(self, ev, key: str, alerted: bool) -> None:
        try:
            if ev.kind == 'ENTRY' and ev.data.get('phase') == 'P2':
                self.tracker.on_entry(key, ev.ticker, ev.minute, ev.data, alerted)
            elif ev.kind == 'EXIT' and (ev.data.get('trade') or {}).get('phase') == 'P2':
                t = ev.data['trade']
                entry_key = f"{self.date.isoformat()}|ENTRY|{t['ticker']}|{t['module']}|{t['m_entry']}"
                self.tracker.on_exit(entry_key, 'exit')
        except Exception as e:                                      # noqa: BLE001
            print(f'lab hook failed: {type(e).__name__}')

    def close(self) -> None:
        try:
            self.tracker.close_all()
            deadline = time.time() + 15
            while time.time() < deadline and (self.tracker.active or self.tracker.due):
                self.tracker.run_due()
                time.sleep(1.0)
        except Exception as e:                                      # noqa: BLE001
            print(f'lab close failed: {type(e).__name__}')
        self.stop.set()


def eod(cfg, d: date, days: Dict, trades: List[Dict], fair: Optional[Dict] = None) -> int:
    """End-of-day lab pass: rewrite today's rows of ledger/lab/p2_shadow.csv from the final bars. Never raises."""
    try:
        from . import shadow_eval as SE, events as EV
        rows = SE.evaluate_day(d, days, trades, state_dir=cfg.state_dir, fair=fair, events=EV.load_events())
        if rows:
            SE.write_rows(cfg.ledger_dir, d, rows)
        return len(rows)
    except Exception as e:                                          # noqa: BLE001
        print(f'lab EOD pass failed for {d}: {type(e).__name__}: {str(e)[:100]}')
        return 0
