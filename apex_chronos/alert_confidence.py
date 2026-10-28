"""Per-alert confidence score (1-100) for APEX-CHRONOS ENTRY alerts.

The score answers one question: "how good is THIS alert, for this bot?". It is a ranking of how actionable the
alert is and how well its setup type has done, NOT a probability that the trade wins - checked directly: scoring
all 165 tracked trades as if each alert had arrived on time, the score has Spearman rho -0.10 (p 0.31) against ROI in
the backtest and +0.01 (p 0.91) in the forward ledger, and tier A (index ETFs) has the weakest model returns. Two
components are always reported separately so an execution-quality 95 is never read as "this will probably win":

  setup      How this setup type (P1A..P3) has actually performed: shrunk win rate and mean ROI per trade,
             pooled over the 110 backtest trades and the live forward ledger.
  execution  Whether the alert can be acted on as written: lateness, VIX-feed freshness, whether a same-day
             expiry is known to exist for the ticker, and whether sizing resolves to at least one contract.

What the evidence did and did not support (reproduce with scripts/refit_confidence.py --checks):

  * Setup TYPE barely separates the five modules once the small samples are shrunk: P1A (75 pooled trades) and
    P2 (55) land within a few points of each other. The backtest favoured P1A (+11.0% vs +1.5% mean ROI), but the
    forward ledger did not repeat it (P1A +1.1% and 36% wins vs P2 +1.8% and 67% wins). So the setup component
    carries only 15% of the score, and P1B / P1C / P3 (9, 17, 9 pooled trades) are scored as a declared neutral
    with a "thin evidence" flag instead of being ranked on a handful of trades.
  * Within a setup type, RVOL at entry, entry minute and the bot's own rank score do not predict ROI (all
    |rho| <= 0.22, p >= 0.39) and change sign between samples. They are deliberately NOT used.
  * The execution weights are judgement, not fitted: there is no sample in which alert lateness or a missing
    expiry could be measured. They encode what docs/LIVE_VS_BACKTEST.md lists as the known live gaps.

Stdlib only. Missing, non-numeric or non-finite values never raise: they score a declared neutral (never a silent
1.0) and are named in the breakdown. The live loop still wraps the call, so a scoring bug cannot suppress an alert.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

from . import config as C

# --------------------------------------------------------------------------------------------------------------
# Setup record: (n trades, wins, mean ROI per trade) per module and sample. Regenerate with
# `python scripts/refit_confidence.py --as-of <date>`; tests pin the backtest side and the forward side up to AS_OF.
AS_OF = '2026-10-28'
RECORD = {
    'P1A': {'bt': (47, 24, 0.1101), 'fwd': (28, 10, 0.0109)},
    'P1B': {'bt': (7, 4, 0.1489), 'fwd': (2, 1, 0.005)},
    'P1C': {'bt': (13, 7, 0.0447), 'fwd': (4, 0, -0.1767)},
    'P2': {'bt': (37, 25, 0.0149), 'fwd': (18, 12, 0.018)},
    'P3': {'bt': (6, 3, 0.0607), 'fwd': (3, 1, -0.0196)},
}

SETUP_WEIGHT = 0.15          # share of the headline score; small because setup type barely discriminates (see above)
SHRINK_K = 40                # pseudo-trades pulling each module toward the pooled average
THIN_N = 30                  # fewer pooled trades than this: do not rank the module, score a declared neutral
THIN_SETUP = 0.40            # neutral for an unproven setup (below the ~0.45-0.50 an established one scores)
WIN_LO, WIN_HI = 0.40, 0.70  # shrunk win rate mapped to 0..1 (coin-flip-ish -> clearly good)
MEAN_LO, MEAN_HI = 0.0, 0.10  # shrunk mean ROI per trade mapped to 0..1 (no edge -> +10% a trade)

# Execution factors: (weight, judgement). Weights are renormalised over the factors that apply to an alert.
W_LATENESS, W_EXPIRY, W_FRESH, W_SIZING = 0.30, 0.40, 0.10, 0.20
UNKNOWN = 0.5                # a missing input scores this, never 1.0
SINGLE_NAME_EXPIRY = 0.5     # same-day expiry not verified against a real chain
ONE_CONTRACT = 0.85          # sizes to exactly one contract: usable, but no room to scale out half at TP1
FRESH_OK_S = C.VIX_NORMAL_DELAY_MIN * 60.0     # the normal Yahoo ^VIX lag is not a defect
FRESH_MAX_S = C.VIX_MAX_STALE_MIN * 60.0       # the bot refuses to evaluate beyond this
STALE_ENTRY_S = 600.0        # runner.Settings.stale_after_s: older than this is not sent as an entry at all
DEFAULT_GRACE_S = 150.0      # runner.Settings.entry_grace_s

DISQUALIFIED_CAP = 49        # an alert that cannot be executed as written is never above tier D
TIERS = ((85, 'A', 'actionable as written'),
         (70, 'B', 'verify the flagged item before acting'),
         (50, 'C', 'reduce size or paper this one'),
         (1, 'D', 'do not act as written'))

_SHORT_BY_NAME = {spec.name: key for key, spec in C.MODULES.items()}


# --------------------------------------------------------------------------------------------------------------
@dataclass
class Factor:
    name: str
    value: float                 # 0..1
    weight: float
    note: str = ''               # shown as the "biggest drag" when this factor loses the most points
    disqualifying: bool = False  # value == 0 here caps the whole alert at tier D


@dataclass
class Confidence:
    score: int
    setup: int
    execution: int
    tier: str
    action: str
    module: str
    record_text: str
    thin: bool
    factors: List[Factor] = field(default_factory=list)
    capped: bool = False
    drag: Optional[str] = None

    def lines(self) -> str:
        out = [f"Confidence {self.score}/100 (setup {self.setup} · execution {self.execution}) - tier {self.tier}, {self.action}",
               self.record_text]
        if self.drag:
            out.append(f"Biggest drag: {self.drag}")
        return '\n'.join(out)

    line = lines   # name used by the other bots' alert_confidence modules


# --------------------------------------------------------------------------------------------------------------
def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pooled(short: str) -> Tuple[int, int, float]:
    rec = RECORD[short]
    n = sum(rec[s][0] for s in rec)
    w = sum(rec[s][1] for s in rec)
    total = sum(rec[s][0] * rec[s][2] for s in rec)
    return n, w, (total / n if n else 0.0)


def _baseline() -> Tuple[float, float]:
    n = w = 0
    total = 0.0
    for short in RECORD:
        pn, pw, pm = _pooled(short)
        n, w, total = n + pn, w + pw, total + pn * pm
    return (w / n, total / n) if n else (0.5, 0.0)


def setup_quality(short: Optional[str]) -> Tuple[float, str, bool]:
    """(quality 0..1, one-line record text, thin?) for a module key such as 'P1A'."""
    if short not in RECORD:
        return THIN_SETUP, 'Setup record: unknown setup type - scored as a neutral.', True
    spec = C.MODULES[short]
    n, w, mean = _pooled(short)
    bt, fwd = RECORD[short]['bt'], RECORD[short]['fwd']

    def rate(t):
        return f"{t[1] / t[0] * 100:.0f}%/{t[0]}" if t[0] else 'none'
    text = (f"Setup record - {spec.short}: won {w / n * 100:.0f}% of {n} tracked trades "
            f"(backtest {rate(bt)}, forward {rate(fwd)}), mean {mean * 100:+.1f}%/trade.")
    if n < THIN_N:
        return THIN_SETUP, text + f" Only {n} trades: too thin to rank, scored as a neutral.", True
    base_w, base_m = _baseline()
    win_s = (w + SHRINK_K * base_w) / (n + SHRINK_K)
    mean_s = (n * mean + SHRINK_K * base_m) / (n + SHRINK_K)
    q = 0.5 * _clip((win_s - WIN_LO) / (WIN_HI - WIN_LO)) + 0.5 * _clip((mean_s - MEAN_LO) / (MEAN_HI - MEAN_LO))
    return q, text + ' A record, not a forecast.', False


# --- execution factors ------------------------------------------------------------------------------------------
def _lateness(late_s, grace_s) -> Factor:
    v = _num(late_s)
    if v is None:
        return Factor('lateness', UNKNOWN, W_LATENESS, 'alert latency unknown')
    if v <= grace_s:
        return Factor('lateness', 1.0, W_LATENESS)
    if v >= STALE_ENTRY_S:
        return Factor('lateness', 0.0, W_LATENESS, f'alert is {v / 60:.0f} min old - the model entry price is stale', True)
    f = 1.0 - 0.7 * (v - grace_s) / (STALE_ENTRY_S - grace_s)
    return Factor('lateness', f, W_LATENESS, f'alert is {v / 60:.1f} min late - 0DTE prices move fast, compare your live quote')


def _freshness(age_s) -> Factor:
    v = _num(age_s)
    if v is None:
        return Factor('freshness', UNKNOWN, W_FRESH, 'VIX feed age unknown')
    if v <= FRESH_OK_S:
        return Factor('freshness', 1.0, W_FRESH)
    f = 1.0 - 0.5 * _clip((v - FRESH_OK_S) / (FRESH_MAX_S - FRESH_OK_S))
    return Factor('freshness', f, W_FRESH, f'VIX feed is {v / 60:.0f} min behind (normal is about {FRESH_OK_S / 60:.0f}) - model IV is stale')


def _expiry(ticker, verified) -> Factor:
    if verified is True:
        return Factor('expiry', 1.0, W_EXPIRY)
    if verified is False:
        return Factor('expiry', 0.0, W_EXPIRY, 'no same-day expiry exists for this ticker today - not tradeable as written', True)
    if isinstance(ticker, str) and ticker in C.INDEX_0DTE:
        return Factor('expiry', 1.0, W_EXPIRY)                 # SPY/QQQ/IWM list daily expiries (docs/LIVE_VS_BACKTEST.md)
    name = ticker if isinstance(ticker, str) and ticker else 'this ticker'
    return Factor('expiry', SINGLE_NAME_EXPIRY, W_EXPIRY,
                  f'same-day expiry NOT verified against a real chain - may not exist for {name} today')


def _sizing(contracts) -> Optional[Factor]:
    if contracts is None:
        return None            # ACCOUNT_CAPITAL unset: identical on every alert, so it is not scored (the alert already says so)
    v = _num(contracts)
    if v is None:
        return Factor('sizing', UNKNOWN, W_SIZING, 'sizing unknown')
    if v < 1:
        return Factor('sizing', 0.0, W_SIZING, 'slot budget is less than one contract - this signal does not fit your account', True)
    if v < 2:
        return Factor('sizing', ONE_CONTRACT, W_SIZING, 'sizes to a single contract - nothing to scale out at TP1')
    return Factor('sizing', 1.0, W_SIZING)


# --------------------------------------------------------------------------------------------------------------
def score_alert(ctx: Mapping, grace_s: float = DEFAULT_GRACE_S) -> Confidence:
    """Score one ENTRY alert.

    ctx keys, all optional (pass what the caller has):
      module           ledger module name ('Phase1A_GapTrap_Single0DTE') or short key ('P1A')
      ticker           'SPY', 'NVDA', ...
      late_s           seconds between the signal bar's close and now
      data_age_s       age of the VIX feed in seconds
      contracts        contract count from the sizing rule; None when ACCOUNT_CAPITAL is unset
      expiry_verified  True / False if a real chain was checked; None (default) when it was not
    """
    mod = ctx.get('module')
    short = (mod if mod in C.MODULES else _SHORT_BY_NAME.get(mod)) if isinstance(mod, str) else None
    q, record_text, thin = setup_quality(short)

    factors = [_lateness(ctx.get('late_s'), grace_s),
               _expiry(ctx.get('ticker'), ctx.get('expiry_verified')),
               _freshness(ctx.get('data_age_s'))]
    sz = _sizing(ctx.get('contracts'))
    if sz is not None:
        factors.append(sz)

    wsum = sum(f.weight for f in factors)
    exec_q = sum(f.value * f.weight for f in factors) / wsum
    overall = SETUP_WEIGHT * q + (1.0 - SETUP_WEIGHT) * exec_q
    capped = any(f.disqualifying and f.value == 0.0 for f in factors)
    score = int(round(_clip(overall * 100.0, 1.0, 100.0)))
    if capped:
        score = min(score, DISQUALIFIED_CAP)
    score = max(1, min(100, score))

    # the drag is whatever costs the most points; for a capped alert it is the factor that capped it
    pool = [f for f in factors if f.disqualifying and f.value == 0.0] or factors
    drag, worst = None, 0.0
    for f in pool:
        loss = (1.0 - f.value) * f.weight / wsum
        if f.note and loss > worst + 1e-9 and loss >= 0.03:
            drag, worst = f.note, loss

    tier, action = next((t, a) for cut, t, a in TIERS if score >= cut)
    return Confidence(score=score, setup=int(round(q * 100)), execution=int(round(exec_q * 100)), tier=tier,
                      action=action, module=short or '?', record_text=record_text, thin=thin,
                      factors=factors, capped=capped, drag=drag)
