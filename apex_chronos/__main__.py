"""python -m apex_chronos <command>

  live       run today's session loop (GitHub Actions: chronos-session.yml)
  eod        end-of-day reconcile against final bars (chronos-eod.yml)
  once       dry-run: evaluate the session so far from live Yahoo data and print what the bot would post
  replay     run the engine over a recorded CSV dataset (used by the verification suite)
  selftest   check config, calendar and (optionally) post a test message to Discord
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from . import __version__, alerts, config as C
from .calendar_nyse import NY, is_full_session, minute_clock, now_et, session_open
from .runner import LiveSession, Settings, run_eod, run_live


def cmd_once(cfg: Settings, args) -> int:
    d = date.fromisoformat(args.date) if args.date else now_et().date()
    if not is_full_session(d):
        print(f'{d} is not a full NYSE session')
        return 0
    cfg.dry_run = True
    cfg.state_dir = args.state_dir or cfg.state_dir
    sess = LiveSession(cfg, d)
    sess.prepare()
    if args.at:
        hh, mm = map(int, args.at.split(':'))
        m_target = hh * 60 + mm - (9 * 60 + 30) - 1
    else:
        now = now_et()
        m_target = min(C.SESSION_BARS - 1, int((now - session_open(d)).total_seconds() // 60) - 1)
    print(f'evaluating {d} through bar {minute_clock(m_target)} (index {m_target})')
    feats, degraded = sess.poll(m_target)
    if feats is None:
        print('feed failure')
        return 1
    events = []
    for m in range(0, m_target + 1):
        deg = {t for t, mins in degraded.items() if m in mins}
        events += sess.engine.step(m, feats, deg)
    far_future = session_open(d) + timedelta(days=1)   # suppress 'stale' logic: this is a dry run of the whole day
    print(f'tickers with features: {sorted(feats)}; excluded: {sess.excluded}')
    print(f"degraded minutes: { {t: sorted(v)[:8] for t, v in degraded.items() if v} }")
    cfg.stale_after_s = 10 ** 9
    sess.dispatch(events, session_open(d) + timedelta(minutes=m_target + 1, seconds=cfg.poll_delay_s))
    print(f'{len(events)} engine events; open positions: {sess.engine.snapshot()["open"]}; trades closed: {len(sess.engine.trades)}')
    return 0


def cmd_replay(args) -> int:
    from .replay import run_all
    tickers = args.tickers.split(',') if args.tickers else C.TICKERS
    df, _ = run_all(args.data_dir, tickers, mode=args.mode, grid=args.grid)
    if args.out:
        df.to_csv(args.out, index=False)
    print(f'{len(df)} trades')
    return 0


def cmd_selftest(cfg: Settings, args) -> int:
    print(f'apex-chronos {__version__}')
    d = now_et().date()
    print(f'today {d}: full session = {is_full_session(d)}')
    print(f'webhook configured: {bool(cfg.webhook)} · capital: {cfg.capital} · slot fraction: {cfg.slot_fraction}')
    if args.post:
        ok = alerts.post_discord(cfg.webhook, '✅ APEX-CHRONOS self-test: Discord webhook works.', cfg.dry_run)
        print('discord post:', 'ok' if ok else 'FAILED')
        return 0 if ok else 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='apex_chronos')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('live')
    p.add_argument('--max-runtime-min', type=float, default=None, help='stop after this many minutes and ask for a hand-off run')
    p = sub.add_parser('eod')
    p.add_argument('--date', default=None)
    p = sub.add_parser('once')
    p.add_argument('--date', default=None)
    p.add_argument('--at', default=None, help='evaluate through the bar closing at HH:MM ET (default: now)')
    p.add_argument('--state-dir', default=None)
    p = sub.add_parser('replay')
    p.add_argument('--data-dir', required=True)
    p.add_argument('--tickers', default=None)
    p.add_argument('--mode', default='fast', choices=['fast', 'livepath'])
    p.add_argument('--grid', action='store_true')
    p.add_argument('--out', default=None)
    p = sub.add_parser('selftest')
    p.add_argument('--post', action='store_true')
    args = ap.parse_args(argv)

    cfg = Settings.from_env()
    if args.cmd == 'live':
        cfg.max_runtime_min = args.max_runtime_min
        return run_live(cfg)
    if args.cmd == 'eod':
        return run_eod(cfg, date.fromisoformat(args.date) if args.date else None)
    if args.cmd == 'once':
        return cmd_once(cfg, args)
    if args.cmd == 'replay':
        return cmd_replay(args)
    if args.cmd == 'selftest':
        return cmd_selftest(cfg, args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
