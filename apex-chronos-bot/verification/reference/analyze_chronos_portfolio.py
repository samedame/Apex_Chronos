import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from run_1m_options_lab import ALL_DATA, TICKERS, INDEX_0DTE
from fast_sim import sim_trade_vec

# Let's test the exact 1-minute implementation of our Out-of-Sample Verified Strategies on the 21-day 1m dataset
# AND on the 60-day 5m dataset!

# 1) LG2_1m: Confirmed Morning Gap-Up (>0.35%) + ORB15 High + Noise Upper + RVOL > 1.25 (Minutes 18..65)
def sig_1m_LG2_confirmed_gap(d):
    O, H, L, C = d['O'], d['H'], d['L'], d['C']
    prev_c, vwap, vstd, rvol, nu, ema9, ema21 = (
        d['prev_c'], d['vwap'], d['vwap_std'], d['rvol_cum'], d['noise_upper'], d['ema9'], d['ema21']
    )
    gap = (O[0] - prev_c) / prev_c
    if gap < 0.0035: return None
    orb15_h = np.max(H[:15])
    # Require 3 consecutive 1m closes above ORB15_H and above Noise Upper
    for m in range(18, 65):
        z = (C[m] - vwap[m]) / max(vstd[m], C[m]*0.0005)
        if (C[m] > orb15_h and C[m-1] > orb15_h and C[m-2] > orb15_h and
            C[m] > nu[m] and rvol[m] >= 1.25 and z > 0.35 and ema9[m] >= ema21[m] and C[m] > O[m]):
            return (m, 1)
    return None

# 2) E1_1m: Morning Gap-Trap Liquidity Sweep Continuation (Minutes 5..60)
def sig_1m_E1_gap_trap(d):
    O, H, L, C = d['O'], d['H'], d['L'], d['C']
    prev_c, vwap, ema9 = d['prev_c'], d['vwap'], d['ema9']
    gap = (O[0] - prev_c) / prev_c
    orb5_h, orb5_l = np.max(H[:5]), np.min(L[:5])
    if gap > 0.0025:
        for m in range(5, 60):
            if C[m] < orb5_l and C[m] < vwap[m] and C[m] < ema9[m] and C[m] > prev_c:
                return (m, 1)
    elif gap < -0.0025:
        for m in range(5, 60):
            if C[m] > orb5_h and C[m] > vwap[m] and C[m] > ema9[m] and C[m] < prev_c:
                return (m, -1)
    return None

# 3) E3_1m: Morning ORB15 Bull-Trap Reject (Minutes 16..105)
def sig_1m_E3_orb15_reject(d):
    O, H, L, C = d['O'], d['H'], d['L'], d['C']
    orb15_h = np.max(H[:15])
    ema9, rvol = d['ema9'], d['rvol_cum']
    popped = False
    for m in range(16, 105):
        if H[m] > orb15_h: popped = True
        if popped and rvol[m] < 1.6 and C[m] < orb15_h and C[m] < ema9[m] and C[m] < O[m] and C[m] < L[m-1]:
            return (m, -1)
    return None

# 4) U5_1m: Midday Range-Wall Pin Credit Spread (Minutes 75..240)
def sig_1m_U5_midday_pin(d):
    O, H, L, C, ema9 = d['O'], d['H'], d['L'], d['C'], d['ema9']
    morn_h, morn_l = np.max(H[:75]), np.min(L[:75])
    for m in range(75, 240):
        if H[m-1] >= morn_h*0.9995 and C[m] < morn_h and C[m] < O[m] and C[m] < L[m-1] and C[m] < ema9[m]:
            return (m, -1)
        if L[m-1] <= morn_l*1.0005 and C[m] > morn_l and C[m] > O[m] and C[m] > H[m-1] and C[m] > ema9[m]:
            return (m, 1)
    return None

# 5) E7_1m: Afternoon Vanna Capitulation Squeeze (Minutes 210..320)
def sig_1m_E7_pm_vanna(d):
    O, H, L, C = d['O'], d['H'], d['L'], d['C']
    vwap, vstd, ema9, iv = d['vwap'], d['vwap_std'], d['ema9'], d['iv_series']
    flushed = False
    for m in range(210, 320):
        day_sd = (C[m] - O[0]) / (O[0] * iv[m] * np.sqrt(m / (390.0 * 252.0)))
        z_vwap = (C[m] - vwap[m]) / max(vstd[m], C[m]*0.0005)
        if day_sd < -1.3 and z_vwap < -1.1:
            flushed = True
        if flushed and C[m] > O[m] and C[m] > H[m-1] and C[m] > ema9[m]:
            return (m, 1)
    return None

# Test each module on the 1-minute data
modules = [
    ('M_LG2_Single', 'Morning Confirmed Gap+ORB15+Noise (0.48d Single)', sig_1m_LG2_confirmed_gap, 0.48, 'single', 0.28, 0.75, 55),
    ('M_LG2_Spread', 'Morning Confirmed Gap+ORB15+Noise (Debit Spread)', sig_1m_LG2_confirmed_gap, 0.50, 'debit_spread', 0.25, 0.50, 60),
    ('M_E1_Single',  'Morning Gap-Trap Liquidity Sweep (0.45d Single)', sig_1m_E1_gap_trap, 0.45, 'single', 0.26, 0.90, 50),
    ('M_E1_Spread',  'Morning Gap-Trap Liquidity Sweep (Debit Spread)', sig_1m_E1_gap_trap, 0.50, 'debit_spread', 0.22, 0.45, 55),
    ('M_E3_Spread',  'Morning ORB15 Bull-Trap Reject (Put Debit Spread)', sig_1m_E3_orb15_reject, 0.45, 'debit_spread', 0.28, 0.45, 65),
    ('M_E3_Credit',  'Morning ORB15 Bull-Trap Reject (Call Credit Spread)', sig_1m_E3_orb15_reject, 0.32, 'credit_spread', 0.20, 0.16, 90),
    ('M_U5_Credit',  'Midday Range-Wall Pin Harvest (0DTE Credit Spread)', sig_1m_U5_midday_pin, 0.32, 'credit_spread', 0.20, 0.16, 90),
    ('M_E7_Spread',  'Afternoon Vanna Capitulation (Call Debit Spread)', sig_1m_E7_pm_vanna, 0.45, 'debit_spread', 0.28, 0.65, 50),
    ('M_E7_Credit',  'Afternoon Vanna Capitulation (Put Credit Spread)', sig_1m_E7_pm_vanna, 0.32, 'credit_spread', 0.20, 0.16, 75),
]

all_mod_trades = {}
for code, name, fn, delta, struct, sl, tp, hold in modules:
    t_list = []
    for tkr in TICKERS:
        for d in ALL_DATA[tkr]:
            sig = fn(d)
            if sig:
                tr = sim_trade_vec(d, sig[0], sig[1], target_delta=delta, structure=struct,
                                   sl_pct=sl, tp1_pct=tp, max_hold_mins=hold, dte_override=0.0)
                if tr:
                    tr['module'] = code
                    tr['structure'] = struct
                    t_list.append(tr)
    all_mod_trades[code] = t_list
    rois = np.array([t['roi'] for t in t_list])
    w = rois[rois > 0]; l = rois[rois <= 0]
    wr = len(w)/len(rois)*100 if len(rois)>0 else 0
    pf = np.sum(w)/max(np.abs(np.sum(l)), 1e-4)
    avg_h = np.mean([t['hold_mins'] for t in t_list])
    print(f"{code:14s} | {name:52s} | N={len(rois):3d} | WR={wr:5.1f}% | AvgW={np.mean(w)*100:5.1f}% | AvgL={np.abs(np.mean(l))*100:5.1f}% | PF={pf:5.2f} | Exp={np.mean(rois)*100:+6.2f}% | Hold={avg_h:4.1f}m")
