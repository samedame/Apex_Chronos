"""Discord alert formatting and delivery.

Messages are built only from engine events; nothing here changes a decision.
Everything is labelled MODEL because the backtest priced options with
Black-Scholes, not real quotes.
"""
from __future__ import annotations

import math
import os
import time
from typing import Dict, List, Optional

import requests

from . import config as C
from .calendar_nyse import bar_close_clock, minute_clock

DISCORD_LIMIT = 1900
_UA = {'User-Agent': 'apex-chronos-bot (github actions)'}  # Discord's edge rejects default python UAs


def pct(x: float, digits: int = 0) -> str:
    return f"{x * 100:+.{digits}f}%"


def contracts_for(cost_basis: float, capital: Optional[float], slot_fraction: float) -> Optional[int]:
    if not capital:
        return None
    return int(math.floor(slot_fraction * capital / (cost_basis * 100.0)))


def _sizing_line(cost_basis: float, capital: Optional[float], slot_fraction: float) -> str:
    per_contract = cost_basis * 100.0
    if not capital:
        return (f"Sizing: model cost ${cost_basis:.2f}/share = ${per_contract:,.0f} per contract. "
                f"Set repo variable ACCOUNT_CAPITAL to get a contract count.")
    n = contracts_for(cost_basis, capital, slot_fraction)
    budget = slot_fraction * capital
    if n == 0:
        return (f"Sizing: slot budget ${budget:,.0f} ({slot_fraction:.0%} of ${capital:,.0f}) is less than one contract "
                f"(${per_contract:,.0f}) -> 0 contracts; this signal does not fit your account.")
    return (f"Sizing: slot budget ${budget:,.0f} ({slot_fraction:.0%} of ${capital:,.0f}) -> {n} contract(s) "
            f"at model ${per_contract:,.0f} each.")


def fmt_entry(date: str, ev, capital=None, slot_fraction=0.25, late_s: float = 0.0, grace_s: float = 150.0,
              confidence: Optional[str] = None) -> str:
    d = ev.data
    side = 'CALL' if d['direction'] == 1 else 'PUT'
    m = ev.minute
    plan = d['plan']
    credit = d['structure'] == 'credit_spread'
    cb = d['cost_basis']
    lines = []
    head = '🟢' if late_s <= grace_s else '🟠'
    late = '' if late_s <= grace_s else f"  ⚠ LATE by {late_s / 60:.1f} min - model entry price may be stale"
    lines.append(f"{head} **APEX-CHRONOS ENTRY** - {ev.ticker} {d['phase']} · slot {d['slot'] + 1}/{C.N_SLOTS}{late}")
    if confidence:
        lines.append(confidence)
    lines.append(f"**{d['legs']}**   exp TODAY (0DTE) - the backtest assumed a same-day expiry")
    lines.append(f"Signal: {ev.module} · bar {minute_clock(m)} closed {bar_close_clock(m)} ET · underlying {d['S_entry']:.2f}")
    if credit:
        lines.append(f"Model credit ${d['credit']:.2f}/share on a ${d['width']:g}-wide spread -> collateral (max loss) ${cb:.2f}/share "
                     f"(${cb * 100:,.0f}/contract). ROI below is measured on the collateral. RVOL {d['rvol_entry']:.2f} · IV(model) {d['iv_entry'] * 100:.0f}%")
    else:
        lines.append(f"Model cost: ${cb:.2f}/share (${cb * 100:,.0f}/contract) · RVOL {d['rvol_entry']:.2f} · IV(model) {d['iv_entry'] * 100:.0f}%")
    lines.append(_sizing_line(cb, capital, slot_fraction))
    sl, tp = plan['stop_roi'], plan['tp1_roi']
    if credit:
        cr = d['credit']
        lines.append(f"Stop: buy the spread back if it costs >= ${cr - sl * cb:.2f} (credit + {-sl * 100:.0f}% of collateral), judged on a 1-min close · "
                     f"take HALF off with a limit buy-back at ${cr - tp * cb:.2f} (credit - {tp * 100:.0f}% of collateral), then floor/trail the rest "
                     f"(floor +{plan['after_tp1_floor'] * 100:.0f}% of collateral, trail 72% of peak) · hard time-stop {bar_close_clock(plan['m_end'])} ET ({plan['max_hold']} min).")
    else:
        lines.append(f"Stop: exit if worth <= {(1 + sl) * 100:.0f}% of your fill cost (down {-sl * 100:.0f}%), judged on a 1-min close · "
                     f"sell HALF with a limit order at +{tp * 100:.0f}% (= {(1 + tp) * 100:.0f}% of fill; model ${cb * (1 + tp):.2f}), then protect the rest: "
                     f"floor +{plan['after_tp1_floor'] * 100:.0f}%, trail 72% of peak profit · hard time-stop {bar_close_clock(plan['m_end'])} ET ({plan['max_hold']} min).")
    lines.append("The bot posts every exit call. All prices above are MODEL prices - compare with your live bid/ask before sending.")
    return '\n'.join(lines)


def fmt_tp1(date: str, ev) -> str:
    d = ev.data
    return (f"🎯 **TP1 - sell HALF** - {ev.ticker} ({ev.module}) · bar {minute_clock(ev.minute)} closed {bar_close_clock(ev.minute)} ET · "
            f"model ROI touched {pct(d['model_roi'])} · slot {d['slot'] + 1}\n"
            f"Keep the runner with the trailing rule (floor +60% of the TP1 gain, trail 72% of peak). The bot posts the exit.")


def fmt_exit(date: str, ev, entry_cost: Optional[float] = None, capital=None, slot_fraction=0.25) -> str:
    t = ev.data['trade']
    reason = {'stop_loss': '🔴 STOP', 'profit_trail': '🟢 PROFIT-TRAIL EXIT', 'time_stop': '⏱ TIME STOP'}[t['exit_reason']]
    action = 'close the WHOLE position now' if t['exit_reason'] != 'profit_trail' else 'close the REMAINING half now'
    pnl = ''
    n = contracts_for(t['cost_basis'], capital, slot_fraction)
    if n:
        usd = t['roi'] * t['cost_basis'] * 100 * n
        pnl = f" · model P&L on {n} contract(s): {'+' if usd >= 0 else '-'}${abs(usd):,.0f}"
    return (f"{reason} - {t['ticker']} ({t['module']}) · exit bar {minute_clock(t['m_exit'])} closed {bar_close_clock(t['m_exit'])} ET · slot {t['slot'] + 1}\n"
            f"**{action}.** Model result: {pct(t['roi'], 1)} on cost (peak {pct(t['peak_roi'])}) after {t['hold_mins']} min{pnl}.")


def fmt_skip(date: str, ev) -> str:
    return f"⚪ {ev.ticker} {ev.module}: {ev.data.get('reason', 'skipped')} (minute {minute_clock(ev.minute)})"


def post_discord(webhook: Optional[str], text: str, dry_run: bool = False, retries: int = 4) -> bool:
    """Post a message. Returns True on success. Never raises."""
    chunks = _split(text)
    if dry_run or not webhook:
        for c in chunks:
            print(f"[discord{' dry-run' if dry_run else ' NOT CONFIGURED'}] {c}\n")
        return bool(dry_run)
    ok_all = True
    for c in chunks:
        ok = False
        for i in range(retries):
            try:
                r = requests.post(webhook, json={'content': c, 'allowed_mentions': {'parse': []}}, headers=_UA, timeout=15)
                if r.status_code in (200, 204):
                    ok = True
                    break
                if r.status_code == 429:
                    wait = 2.0
                    try:
                        wait = float(r.json().get('retry_after', 2.0))
                    except Exception:
                        pass
                    time.sleep(min(wait + 0.2, 15))
                    continue
                print(f"discord HTTP {r.status_code}: {r.text[:200]}")
            except requests.RequestException as e:
                print(f"discord error: {e!r}")
            time.sleep(1.5 * (i + 1))
        ok_all = ok_all and ok
    return ok_all


def _split(text: str) -> List[str]:
    if len(text) <= DISCORD_LIMIT:
        return [text]
    out, cur = [], ''
    for line in text.split('\n'):
        if len(cur) + len(line) + 1 > DISCORD_LIMIT:
            out.append(cur)
            cur = ''
        cur += line + '\n'
    if cur.strip():
        out.append(cur)
    return out
