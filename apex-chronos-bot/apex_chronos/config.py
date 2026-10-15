"""Strategy constants, copied from the original backtest (run_apex_chronos_final.py,
run_1m_options_lab.py, fast_sim.py). Nothing here is tuned or new: every number
has a source line in the original code, noted in the comments.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- universe (run_1m_options_lab.TICKERS) ----------------------------------
TICKERS = ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'META', 'AMD', 'AAPL', 'MSFT', 'AMZN', 'COIN', 'MSTR']
INDEX_0DTE = {'SPY', 'QQQ', 'IWM'}

# fast_sim.sim_trade_vec: half-spread assumption (fraction of option value)
TIGHT_SPREAD_NAMES = ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'AAPL', 'META', 'AMD', 'MSFT', 'AMZN']
HALF_SPREAD_TIGHT = 0.005
HALF_SPREAD_WIDE = 0.008

# run_1m_options_lab.select_strike: names on a $1 strike grid
STEP1_NAMES = ['SPY', 'QQQ', 'IWM', 'AMD', 'AAPL', 'NVDA', 'MSFT', 'AMZN', 'COIN']

RISK_FREE = 0.045           # r in every Black-Scholes call
SESSION_BARS = 390          # 09:30 .. 15:59 inclusive
MINUTES_PER_YEAR_DEN = 390.0 * 252.0
HISTORY_SESSIONS = 14       # lookback for noise-area and RVOL baselines
MIN_HISTORY_SESSIONS = 5    # oracle falls back to a degenerate baseline below this; the bot refuses instead
MIN_SESSION_BARS = 350      # oracle skips sessions with fewer bars

# --- portfolio scheduler (run_apex_chronos_final.schedule_chronos) -----------
N_SLOTS = 3
PHASE_LIMITS = {'P1': 4, 'P2': 2, 'P3': 1}
SLOT_REUSE_DELAY = 1        # slot_free[s] = m_exit + 1 : no new entry in the minute a trade exits

# --- exit ladder (fast_sim.sim_trade_vec) -------------------------------------
AFTER_TP1_FLOOR_FRAC = 0.60     # after the half is sold, floor = 60% of the TP1 gain
TRAIL_PCT_OF_PEAK = 0.72        # ... and trail at 72% of the peak intrabar ROI


@dataclass(frozen=True)
class ModuleSpec:
    """One sub-engine. Field names mirror the sim_trade_vec arguments."""
    name: str            # ledger 'module' column
    short: str           # label used in alerts
    phase: str           # P1 / P2 / P3
    order: int           # position in the oracle's per-ticker candidate loop (tie-break)
    structure: str       # single | debit_spread | credit_spread
    target_delta: float  # only used when structure == 'single' (spreads hard-code their deltas)
    sl_pct: float
    tp1_pct: float
    max_hold_mins: int
    score_base: float    # candidate score = score_base +/- rvol  (see engine)
    score_sign: int      # +1: base + rvol ; -1: base - rvol


MODULES = {
    'P1A': ModuleSpec('Phase1A_GapTrap_Single0DTE', 'Gap-Trap Sweep', 'P1', 0,
                      'single', 0.45, 0.26, 0.90, 50, 10.0, -1),
    'P1B': ModuleSpec('Phase1B_ConfirmedGap_DebitSpread', 'Confirmed Gap + ORB15', 'P1', 1,
                      'debit_spread', 0.50, 0.25, 0.50, 60, 20.0, +1),
    'P1C': ModuleSpec('Phase1C_BullTrap_PutDebitSpread', 'ORB15 Bull-Trap Reject', 'P1', 2,
                      'debit_spread', 0.45, 0.28, 0.45, 65, 15.0, +1),
    'P2': ModuleSpec('Phase2_MiddayPin_CreditSpread', 'Midday Range-Wall Pin', 'P2', 3,
                     'credit_spread', 0.32, 0.20, 0.16, 80, 5.0, +1),
    'P3': ModuleSpec('Phase3_AfternoonVanna_CallSpread', 'Afternoon Vanna Squeeze', 'P3', 4,
                     'debit_spread', 0.45, 0.28, 0.65, 50, 12.0, +1),
}
MODULE_ORDER = ['P1A', 'P1B', 'P1C', 'P2', 'P3']

# Post-signal entry filters (inline in run_apex_chronos_final.py)
P1A_MAX_MINUTE = 35
P1A_MAX_RETRACE = 0.75
P1A_MAX_RVOL = 1.25
P1C_RVOL_MIN, P1C_RVOL_MAX = 0.85, 1.65
P2_MIN_RVOL = 1.00

# Live VIX: Yahoo's ^VIX 1-minute feed runs ~15 minutes behind (the backtest used same-minute VIX).
# The bot tolerates the normal delay but fails closed if the feed is older than this.
VIX_MAX_STALE_MIN = 30
VIX_NORMAL_DELAY_MIN = 20
