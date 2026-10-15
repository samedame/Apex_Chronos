import numpy as np
from scipy.stats import norm
from run_1m_options_lab import ALL_DATA, TICKERS, INDEX_0DTE, bs_option_price_and_delta, select_strike

def sim_trade_vec(day_rec, m_entry, direction, target_delta=0.48,
                  structure='single', sl_pct=0.25, tp1_pct=0.35,
                  max_hold_mins=50, dte_override=0.0):
    n_bars = day_rec['n_bars']
    if m_entry >= n_bars - 15 or m_entry < 1:
        return None
    ticker = day_rec['ticker']
    O, H, L, C = day_rec['O'], day_rec['H'], day_rec['L'], day_rec['C']
    iv_series = day_rec['iv_series']
    base_dte = dte_override if dte_override is not None else (0.0 if ticker in INDEX_0DTE else day_rec['base_dte_days'])

    is_call = (direction == 1)
    S_entry = C[m_entry]
    iv_entry = iv_series[m_entry]
    T_entry = (base_dte * 390.0 + (390.0 - m_entry) + 25.0) / (390.0 * 252.0)
    half_spread = 0.005 if ticker in ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'AAPL', 'META', 'AMD', 'MSFT', 'AMZN'] else 0.008

    if structure == 'single':
        K1 = select_strike(S_entry, T_entry, iv_entry, target_delta, is_call, ticker)
        mid_entry, _ = bs_option_price_and_delta(S_entry, K1, T_entry, 0.045, iv_entry, is_call)
        cost_basis = max(float(mid_entry) * (1.0 + half_spread) + 0.01, 0.15)
    elif structure == 'debit_spread':
        K1 = select_strike(S_entry, T_entry, iv_entry, 0.58, is_call, ticker)
        K2 = select_strike(S_entry, T_entry, iv_entry, 0.28, is_call, ticker)
        if K1 == K2: K2 = K1 + (2.0 if is_call else -2.0)
        p1, _ = bs_option_price_and_delta(S_entry, K1, T_entry, 0.045, iv_entry, is_call)
        p2, _ = bs_option_price_and_delta(S_entry, K2, T_entry, 0.045, iv_entry * 1.01, is_call)
        cost_basis = max(float(p1 - p2) * (1.0 + half_spread) + 0.015, 0.20)
    elif structure == 'credit_spread':
        opp_call = not is_call
        K1 = select_strike(S_entry, T_entry, iv_entry, 0.32, opp_call, ticker)
        K2 = select_strike(S_entry, T_entry, iv_entry, 0.10, opp_call, ticker)
        if K1 == K2: K2 = K1 + (2.0 if opp_call else -2.0)
        p1, _ = bs_option_price_and_delta(S_entry, K1, T_entry, 0.045, iv_entry, opp_call)
        p2, _ = bs_option_price_and_delta(S_entry, K2, T_entry, 0.045, iv_entry * 1.02, opp_call)
        credit = max(float(p1 - p2) * (1.0 - half_spread) - 0.015, 0.12)
        width = abs(K2 - K1)
        cost_basis = max(width - credit, 0.25)

    m_end = min(n_bars - 5, m_entry + max_hold_mins)
    m_arr = np.arange(m_entry + 1, m_end + 1)
    T_arr = (base_dte * 390.0 + (390.0 - m_arr) + 25.0) / (390.0 * 252.0)
    iv_arr = iv_series[m_arr]
    S_worst_arr = L[m_arr] if is_call else H[m_arr]
    S_best_arr  = H[m_arr] if is_call else L[m_arr]
    S_close_arr = C[m_arr]

    def val_vec(S_v, T_v, iv_v):
        if structure == 'single':
            mid, _ = bs_option_price_and_delta(S_v, K1, T_v, 0.045, iv_v, is_call)
            return np.maximum(mid * (1.0 - half_spread) - 0.01, 0.0)
        elif structure == 'debit_spread':
            p1, _ = bs_option_price_and_delta(S_v, K1, T_v, 0.045, iv_v, is_call)
            p2, _ = bs_option_price_and_delta(S_v, K2, T_v, 0.045, iv_v * 1.01, is_call)
            return np.maximum(np.maximum(p1 - p2, 0.0) * (1.0 - half_spread) - 0.015, 0.0)
        elif structure == 'credit_spread':
            opp_call = not is_call
            p1, _ = bs_option_price_and_delta(S_v, K1, T_v, 0.045, iv_v, opp_call)
            p2, _ = bs_option_price_and_delta(S_v, K2, T_v, 0.045, iv_v * 1.02, opp_call)
            debit_close = np.maximum(p1 - p2, 0.0) * (1.0 + half_spread) + 0.015
            return np.maximum(cost_basis + (credit - debit_close), 0.0)

    roi_worst_arr = (val_vec(S_worst_arr, T_arr, iv_arr) - cost_basis) / cost_basis
    roi_best_arr  = (val_vec(S_best_arr,  T_arr, iv_arr) - cost_basis) / cost_basis
    roi_close_arr = (val_vec(S_close_arr, T_arr, iv_arr) - cost_basis) / cost_basis

    peak_roi = 0.0
    half_scaled = False
    locked_half1 = 0.0
    dyn_sl = -sl_pct

    for idx in range(len(m_arr)):
        m = int(m_arr[idx])
        rw = float(roi_worst_arr[idx])
        rb = float(roi_best_arr[idx])
        rc = float(roi_close_arr[idx])

        if (not half_scaled) and (rb >= tp1_pct):
            half_scaled = True
            locked_half1 = tp1_pct
            dyn_sl = tp1_pct * 0.60

        if rb > peak_roi:
            peak_roi = rb
        if half_scaled and peak_roi > tp1_pct:
            dyn_sl = max(dyn_sl, peak_roi * 0.72)

        if half_scaled and rw <= dyn_sl:
            return {'ticker': ticker, 'date': day_rec['date'], 'm_entry': m_entry, 'm_exit': m,
                    'hold_mins': m - m_entry, 'direction': direction, 'cost_basis': cost_basis,
                    'roi': 0.5 * locked_half1 + 0.5 * dyn_sl, 'peak_roi': peak_roi,
                    'exit_reason': 'profit_trail', 'rvol_entry': float(day_rec['rvol_cum'][m_entry])}

        if (not half_scaled) and rc <= dyn_sl:
            return {'ticker': ticker, 'date': day_rec['date'], 'm_entry': m_entry, 'm_exit': m,
                    'hold_mins': m - m_entry, 'direction': direction, 'cost_basis': cost_basis,
                    'roi': rc, 'peak_roi': peak_roi,
                    'exit_reason': 'stop_loss', 'rvol_entry': float(day_rec['rvol_cum'][m_entry])}

    final_roi = 0.5 * locked_half1 + 0.5 * float(roi_close_arr[-1]) if half_scaled else float(roi_close_arr[-1])
    return {'ticker': ticker, 'date': day_rec['date'], 'm_entry': m_entry, 'm_exit': int(m_end),
            'hold_mins': int(m_end - m_entry), 'direction': direction, 'cost_basis': cost_basis,
            'roi': final_roi, 'peak_roi': peak_roi,
            'exit_reason': 'time_stop', 'rvol_entry': float(day_rec['rvol_cum'][m_entry])}
