# Setup

About 15 minutes. Nothing here places an order: the bot only posts to Discord.

## 1. Put the repo on GitHub

```bash
cd apex-chronos-bot
git init -b main && git add -A && git commit -m "APEX-CHRONOS alert bot"
gh repo create samedame/apex-chronos-bot --public --source . --push      # or --private, see the cost note below
```

**Public or private? It is mostly a cost decision.** The live loop polls every minute from the open to the close, about
5.5-6.7 hours a day, roughly **7,000-8,400 Actions minutes a month**. GitHub's standard Linux runners are free in public
repositories. In a private repository you get 2,000 (Free plan) or 3,000 (Pro plan) minutes a month, then
$0.006/minute ([GitHub billing docs](https://docs.github.com/en/billing/concepts/product-billing/github-actions)), which comes to about
$30-36 a month at this usage. Options:

| Option | Cost | Trade-off |
|---|---|---|
| Public repo | $0 | The strategy code, `state/` and `ledger/` (every alert the bot sent, no secrets) are world-readable. The webhook stays in a secret. |
| Private repo | ~$30-36/month | Everything stays private. |
| Private repo + self-hosted runner (your Mac / a small VPS) | $0 | Needs a machine that is on from 09:15 to 16:30 ET. Add `runs-on: self-hosted` to the session job. |

## 2. Secrets and variables (Settings -> Secrets and variables -> Actions)

| Name | Kind | Value |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | secret | the channel webhook for APEX-CHRONOS alerts |
| `ACCOUNT_CAPITAL` | variable | optional. Account size in dollars. If unset, alerts show cost per contract instead of a contract count (same convention as the other bots). |
| `SLOT_FRACTION` | variable | optional. Fraction of capital per slot. Default `0.25`. The backtest sized 25 / 33 / 42% depending on the equity tier, so `0.25` is the conservative end. |
| `DRY_RUN` | variable | optional. `true` prints alerts in the Actions log instead of posting to Discord. |

Settings -> Actions -> General -> Workflow permissions: leave "Read and write permissions" on (the bot commits `state/`).

## 3. Smoke tests

1. Actions -> **tests** -> Run workflow. It runs the unit tests and the whole verification against the backtest (about 3 minutes).
2. Locally: `pip install -r requirements.txt` then `DISCORD_WEBHOOK_URL=... python -m apex_chronos selftest --post` sends a test message.
3. Any weekday during market hours: `python -m apex_chronos once` prints what the bot would have posted so far today, using live Yahoo data, without posting.
4. Actions -> **chronos-session** -> Run workflow with `DRY_RUN=true` set as a variable: watch one session in the log before going live.

## 4. Scheduling (important: do not rely on GitHub's native cron)

On this account native `schedule:` runs land hours late (see the 2026-09-25 incident write-up), so every other bot is started
by the Cloudflare Worker `open-scheduler` through `workflow_dispatch`. Do the same here. Add two jobs to the Worker's job list,
using the same fields the existing jobs use:

| id | repo | workflow | ref | target (ET) | grace | days |
|---|---|---|---|---|---|---|
| `chronos-session` | `samedame/apex-chronos-bot` | `chronos-session.yml` | `main` | **09:20** | 10 min (usable until 09:30; later starts still catch up) | trading days (holidays skipped by the Worker's table) |
| `chronos-eod` | `samedame/apex-chronos-bot` | `chronos-eod.yml` | `main` | **16:25** | 30 min | trading days |

Both workflows already declare the `scheduled_for_et` and `trigger` inputs the Worker sends, so the dispatch will not be rejected.
`GH_PAT` on the Worker needs `repo` + `workflow` scope on this repository too.

The two cron lines in each workflow are only a backup for DST (13:28 and 14:28 UTC for the session). A backup that fires more than
45 minutes before the open, or after the close, exits immediately. A late start is harmless: the engine replays the minutes it missed
and the sent-alert ledger in `state/` prevents duplicates. An entry that is more than 10 minutes old when the bot first sees it is
posted as "Missed entry (stale), not actionable" instead.

## 5. Go live gradually

1. **Shadow week.** Leave `ACCOUNT_CAPITAL` unset and trade nothing. Every evening the **EOD** post compares what the live loop alerted with a
   replay on the final bars and lists any entry that was missed or phantom. Zero differences for 5 days is the bar for trusting the live data path.
2. **Check the model against your broker.** Each entry alert prints MODEL prices. Compare with your live bid/ask for a week before sizing up.
   See `docs/LIVE_VS_BACKTEST.md` for why they can differ a lot.
3. **Forward test before capital.** The backtest is 110 trades over 21 sessions. `ledger/trades.csv` collects the bot's own forward record
   (daily, from final bars) and the EOD post prints it next to the backtest's 110 trades / 57.3% / profit factor 1.86 / +7.01% per trade.
   Decide the sample size you need before looking (your other strategies use a pre-registered gate; the same discipline applies here).

## Operating notes

- **Calendar:** `apex_chronos/calendar_nyse.py` covers NYSE holidays and early closes through 2027. Early-close days are skipped because the strategy was only ever tested on full 390-minute sessions. Update it each December.
- **History:** the noise-area and volume baselines need the previous 14 full sessions. The bot keeps them in `state/history/` (added by the EOD job) and rebuilds missing days from Yahoo (about 30 days of 1-minute history are available). A ticker with fewer than 5 prior sessions is excluded for the day, announced in Discord, rather than traded on a degraded baseline.
- **If Yahoo is down** for 3 polls in a row you get a "data feed down" message and the bot keeps retrying. Open positions then have to be managed from their printed plan.
- **If ^VIX is stale** (Yahoo's index feed normally runs about 15 minutes behind), the bot keeps working up to 30 minutes of staleness; beyond that it stops evaluating rather than guess the implied volatility.
- **Manual re-run of a day:** `python -m apex_chronos eod --date 2026-10-15`.
- **Re-running the proof:** `python -m verification.verify_against_backtest --data-dir verification/fixtures.tar.gz` (or `--data-dir` pointing at the original `data/` folder for the full 24-ticker version).
