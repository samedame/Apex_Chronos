import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from run_1m_options_lab import ALL_DATA, TICKERS, INDEX_0DTE
from fast_sim import sim_trade_vec
from analyze_chronos_portfolio import (
    sig_1m_LG2_confirmed_gap, sig_1m_E1_gap_trap, sig_1m_E3_orb15_reject,
    sig_1m_U5_midday_pin, sig_1m_E7_pm_vanna
)

# Build the refined, regime-conditioned signals for APEX-CHRONOS 1M
candidates = []
for tkr in TICKERS:
    for d in ALL_DATA[tkr]:
        # Phase I-A: E1 Gap-Trap Liquidity Sweep (Early sweep m<=35, Retrace < 0.75, Low-to-Mod RVOL <= 1.25)
        s1 = sig_1m_E1_gap_trap(d)
        if s1:
            m, direc = s1
            retrace = abs(d['C'][m] - d['O'][0]) / max(abs(d['O'][0] - d['prev_c']), 1e-4)
            if m <= 35 and retrace < 0.75 and d['rvol_cum'][m] <= 1.25:
                tr = sim_trade_vec(d, m, direc, target_delta=0.45, structure='single',
                                   sl_pct=0.26, tp1_pct=0.90, max_hold_mins=50, dte_override=0.0)
                if tr:
                    tr['module'] = 'Phase1A_GapTrap_Single0DTE'
                    tr['phase'] = 'P1'
                    tr['score'] = 10.0 - d['rvol_cum'][m] # Prefer cleaner low-volume sweeps
                    candidates.append(tr)

        # Phase I-B: LG2 Confirmed Gap+ORB15+NoiseUpper (5m Boundary Close, High RVOL >= 1.25)
        s2 = sig_1m_LG2_confirmed_gap(d)
        if s2:
            m5 = min(d['n_bars'] - 20, s2[0] + (4 - (s2[0] % 5)))
            tr = sim_trade_vec(d, m5, s2[1], target_delta=0.50, structure='debit_spread',
                               sl_pct=0.25, tp1_pct=0.50, max_hold_mins=60, dte_override=0.0)
            if tr:
                tr['module'] = 'Phase1B_ConfirmedGap_DebitSpread'
                tr['phase'] = 'P1'
                tr['score'] = 20.0 + d['rvol_cum'][m5] # Highest priority when confirmed
                candidates.append(tr)

        # Phase I-C: E3 ORB15 Bull-Trap Absorption Reject Above VWAP (0.85 <= RVOL < 1.65 & C >= VWAP)
        s3 = sig_1m_E3_orb15_reject(d)
        if s3:
            m, direc = s3
            if 0.85 <= d['rvol_cum'][m] < 1.65 and d['C'][m] >= d['vwap'][m]:
                tr = sim_trade_vec(d, m, direc, target_delta=0.45, structure='debit_spread',
                                   sl_pct=0.28, tp1_pct=0.45, max_hold_mins=65, dte_override=0.0)
                if tr:
                    tr['module'] = 'Phase1C_BullTrap_PutDebitSpread'
                    tr['phase'] = 'P1'
                    tr['score'] = 15.0 + d['rvol_cum'][m]
                    candidates.append(tr)

        # Phase II: U5 Midday Range-Wall Pin Credit Spread (RVOL >= 1.00)
        s4 = sig_1m_U5_midday_pin(d)
        if s4:
            m, direc = s4
            if d['rvol_cum'][m] >= 1.00:
                tr = sim_trade_vec(d, m, direc, target_delta=0.32, structure='credit_spread',
                                   sl_pct=0.20, tp1_pct=0.16, max_hold_mins=80, dte_override=0.0)
                if tr:
                    tr['module'] = 'Phase2_MiddayPin_CreditSpread'
                    tr['phase'] = 'P2'
                    tr['score'] = 5.0 + d['rvol_cum'][m]
                    candidates.append(tr)

        # Phase III: E7 Afternoon Vanna Capitulation Squeeze (13:30 - 14:50 ET)
        s5 = sig_1m_E7_pm_vanna(d)
        if s5:
            m, direc = s5
            tr = sim_trade_vec(d, m, direc, target_delta=0.45, structure='debit_spread',
                               sl_pct=0.28, tp1_pct=0.65, max_hold_mins=50, dte_override=0.0)
            if tr:
                tr['module'] = 'Phase3_AfternoonVanna_CallSpread'
                tr['phase'] = 'P3'
                tr['score'] = 12.0 + d['rvol_cum'][m]
                candidates.append(tr)

df_c = pd.DataFrame(candidates)

# Strict Capital Slot Scheduler (3 Concurrent Capital Slots: e.g., 30% of equity per slot, max 90% deployed at once)
# Phase budgets per day: up to 4 Phase-1 trades, up to 2 Phase-2 trades, up to 1 Phase-3 trade
def schedule_chronos(df_in, n_slots=3):
    df_s = df_in.sort_values(['date', 'm_entry', 'score'], ascending=[True, True, False]).copy()
    taken = []
    for d, g in df_s.groupby('date', sort=True):
        slot_free = [-1] * n_slots
        p_counts = {'P1': 0, 'P2': 0, 'P3': 0}
        p_limits = {'P1': 4, 'P2': 2, 'P3': 1}
        used_tkr_phase = set()
        for _, row in g.iterrows():
            ph = row['phase']
            if p_counts[ph] >= p_limits[ph]:
                continue
            if (row['ticker'], ph) in used_tkr_phase:
                continue
            for s_idx in range(n_slots):
                if row['m_entry'] >= slot_free[s_idx]:
                    slot_free[s_idx] = row['m_exit'] + 1
                    p_counts[ph] += 1
                    used_tkr_phase.add((row['ticker'], ph))
                    rc = row.to_dict()
                    rc['slot'] = s_idx
                    taken.append(rc)
                    break
    return pd.DataFrame(taken)

df_exec = schedule_chronos(df_c, n_slots=3)
df_exec.to_csv('/home/user/apex_chronos_final_trades.csv', index=False)

print(f"\n==========================================================================================")
print(f"   APEX-CHRONOS 1M MASTER PORTFOLIO RESULTS (21 TRADING DAYS, 114,554 1-MINUTE BARS)")
print(f"==========================================================================================")
for mod, grp in df_exec.groupby('module'):
    r = grp['roi'].values
    w = r[r > 0]; l = r[r <= 0]
    pf = np.sum(w)/max(np.abs(np.sum(l)), 1e-4)
    print(f"  {mod:34s} | N={len(r):3d} | WR={len(w)/len(r)*100:5.1f}% | AvgW={np.mean(w)*100:5.1f}% | AvgL={np.abs(np.mean(l))*100:5.1f}% | PF={pf:5.2f} | Exp={np.mean(r)*100:+6.2f}%")

r_all = df_exec['roi'].values
w_all = r_all[r_all > 0]; l_all = r_all[r_all <= 0]
pf_all = np.sum(w_all)/max(np.abs(np.sum(l_all)), 1e-4)
print("-" * 98)
print(f"  {'MASTER COMBINED PORTFOLIO':34s} | N={len(r_all):3d} ({len(r_all)/21:.2f}/day) | WR={len(w_all)/len(r_all)*100:5.1f}% | AvgW={np.mean(w_all)*100:5.1f}% | AvgL={np.abs(np.mean(l_all))*100:5.1f}% | PF={pf_all:5.2f} | Exp={np.mean(r_all)*100:+6.2f}%")

def neg_k(f):
    if np.any(1.0 + f * r_all <= 0.05): return 1e9
    return -np.mean(np.log(1.0 + f * r_all))
f_star = minimize_scalar(neg_k, bounds=(0.0, 0.95), method='bounded').x
print(f"\nExact Optimal Kelly Fraction per Trade: f* = {f_star*100:.1f}%")

for label, f_slot in [
    ('Tier 1: Institutional Half-Kelly (25% per slot)', 0.25),
    ('Tier 2: Optimal Geometric Growth (33% per slot, 3 slots = 100% cash)', 0.33),
    ('Tier 3: High-Conviction Sprint (42% per slot, sequential reinvestment)', 0.42),
]:
    eq = 1500.0
    peak = 1500.0
    max_dd = 0.0
    d_rets = []
    for d, g in df_exec.groupby('date', sort=True):
        eq0 = eq
        for _, row in g.iterrows():
            eq = eq * (1.0 + f_slot * row['roi'])
            if eq > peak: peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd: max_dd = dd
        d_rets.append(eq / eq0 - 1.0)
    d_rets = np.array(d_rets)
    sharpe = (np.mean(d_rets) / np.std(d_rets)) * np.sqrt(252)
    sortino = (np.mean(d_rets) / np.std(d_rets[d_rets < 0])) * np.sqrt(252) if np.any(d_rets < 0) else 99.0
    g_day = np.log(eq / 1500.0) / 21.0
    cdgr = (np.exp(g_day) - 1.0) * 100
    days_100k = np.log(100000.0 / 1500.0) / g_day
    proj_53d = 1500.0 * np.exp(g_day * 53.0)
    print(f"\n>>> {label} <<<")
    print(f"  21-Day Realized Account Balance : ${eq:,.2f} ({(eq/1500-1)*100:+.1f}% net gain in 21 trading days)")
    print(f"  Daily Compound Growth Rate      : +{cdgr:.2f}% / trading day (Win Days: {np.sum(d_rets>0)}/{len(d_rets)} = {np.mean(d_rets>0)*100:.1f}%)")
    print(f"  Max Portfolio Drawdown          : {max_dd*100:.2f}%")
    print(f"  Annualized Sharpe / Sortino     : {sharpe:.2f} / {sortino:.2f}")
    print(f"  Days to Turn $1,500 -> $100,000 : {days_100k:.1f} trading days (~{days_100k/21:.1f} months)")
    print(f"  53-Trading-Day (Dec 31) Equity  : ${proj_53d:,.2f}")
