import os
import json
import numpy as np
import pandas as pd
from scipy.stats import norm
import warnings
warnings.filterwarnings('ignore')

# ==============================================================================
# 1. BLACK-SCHOLES-MERTON 1-MINUTE INTRADAY OPTIONS PRICING ENGINE
# ==============================================================================
def bs_option_price_and_delta(S, K, T_years, r, sigma, is_call=True):
    T = np.maximum(T_years, 1.0 / (390.0 * 252.0 * 60.0)) # min 1 second
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        nd1 = norm.cdf(d1)
        nd2 = norm.cdf(d2)
        price = S * nd1 - K * np.exp(-r * T) * nd2
        delta = nd1
    else:
        nd1 = norm.cdf(-d1)
        nd2 = norm.cdf(-d2)
        price = K * np.exp(-r * T) * nd2 - S * nd1
        delta = -nd1
    return np.maximum(price, 0.01), delta

def select_strike(S, T_years, sigma, target_abs_delta, is_call=True, ticker='SPY'):
    # Analytical strike for target delta: d1 = norm.ppf(target_abs_delta) for call
    T = np.maximum(T_years, 1e-5)
    if is_call:
        d1_target = norm.ppf(np.clip(target_abs_delta, 0.05, 0.95))
    else:
        d1_target = -norm.ppf(np.clip(target_abs_delta, 0.05, 0.95))
    K_raw = S * np.exp(-d1_target * sigma * np.sqrt(T) + 0.5 * sigma * sigma * T)
    # Snap to realistic exchange strike grid
    if ticker in ['SPY', 'QQQ', 'IWM', 'AMD', 'AAPL', 'NVDA', 'MSFT', 'AMZN', 'COIN']:
        step = 1.0
    elif S < 400:
        step = 1.0
    else:
        step = 2.5
    return np.round(K_raw / step) * step

# ==============================================================================
# 2. LOAD AND PREPARE ALL 1-MINUTE DATA + INTRADAY INDICATORS
# ==============================================================================
TICKERS = ['SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'META', 'AMD', 'AAPL', 'MSFT', 'AMZN', 'COIN', 'MSTR']
INDEX_0DTE = {'SPY', 'QQQ', 'IWM'}

# Load 1-minute VIX
vix_df = pd.read_csv('/home/user/data/1m/VIX.csv')
vix_df['timestamp'] = pd.to_datetime(vix_df['timestamp'], utc=True).dt.tz_convert('America/New_York')
vix_df = vix_df.set_index('timestamp')['close'].sort_index()

# Also load 5m data to compute 14-day lookback baselines (Noise area, ATR, RVOL baselines) prior to the first 1m day
def prepare_ticker_data(ticker):
    df1m = pd.read_csv(f'/home/user/data/1m/{ticker}.csv')
    df1m['timestamp'] = pd.to_datetime(df1m['timestamp'], utc=True).dt.tz_convert('America/New_York')
    df1m = df1m.sort_values('timestamp').reset_index(drop=True)
    df1m['date'] = df1m['timestamp'].dt.strftime('%Y-%m-%d')
    df1m['hm'] = df1m['timestamp'].dt.strftime('%H:%M')
    # Filter regular trading hours 09:30 to 15:59
    df1m = df1m[(df1m['hm'] >= '09:30') & (df1m['hm'] <= '15:59')].copy().reset_index(drop=True)

    # Load 1d data for 14-day ATR and daily realized vol multiplier
    df1d = pd.read_csv(f'/home/user/data/1d/{ticker}.csv')
    df1d['timestamp'] = pd.to_datetime(df1d['timestamp'], utc=True).dt.tz_convert('America/New_York')
    df1d['date'] = df1d['timestamp'].dt.strftime('%Y-%m-%d')
    df1d['prev_close'] = df1d['close'].shift(1)
    df1d['tr'] = np.maximum(df1d['high'] - df1d['low'],
                 np.maximum(np.abs(df1d['high'] - df1d['prev_close']),
                            np.abs(df1d['low'] - df1d['prev_close'])))
    df1d['atr14'] = df1d['tr'].rolling(14).mean().shift(1)
    df1d['ret'] = np.log(df1d['close'] / df1d['prev_close'])
    df1d['rvol20'] = df1d['ret'].rolling(20).std().shift(1) * np.sqrt(252)
    daily_map = df1d.set_index('date')[['prev_close', 'atr14', 'rvol20']].to_dict('index')

    # Map VIX 1m onto df1m
    df1m['vix'] = vix_df.reindex(df1m['timestamp'], method='ffill').values
    df1m['vix'] = df1m['vix'].fillna(18.0)

    # Build day-by-day structures
    days = sorted(df1m['date'].unique())
    day_records = []
    
    # Track historical intraday move profiles & cumulative volume profiles
    hist_abs_moves = [] # list of 390-arrays of |C_m / O_0 - 1|
    hist_cum_vols = []  # list of 390-arrays of cumulative volume

    # Pre-seed hist_abs_moves and hist_cum_vols from 5m data for the 14 days prior to days[0]
    df5m = pd.read_csv(f'/home/user/data/5m/{ticker}.csv')
    df5m['timestamp'] = pd.to_datetime(df5m['timestamp'], utc=True).dt.tz_convert('America/New_York')
    df5m['date'] = df5m['timestamp'].dt.strftime('%Y-%m-%d')
    df5m['hm'] = df5m['timestamp'].dt.strftime('%H:%M')
    df5m = df5m[(df5m['hm'] >= '09:30') & (df5m['hm'] <= '15:59') & (df5m['date'] < days[0])]
    for d5, g5 in df5m.groupby('date'):
        if len(g5) >= 70:
            o0 = g5['open'].iloc[0]
            moves_5m = np.abs(g5['close'].values / o0 - 1.0)
            cumv_5m = np.cumsum(g5['volume'].fillna(0).values)
            # Interpolate to 390 minutes
            x_old = np.linspace(0, 389, len(moves_5m))
            x_new = np.arange(390)
            hist_abs_moves.append(np.interp(x_new, x_old, moves_5m))
            hist_cum_vols.append(np.interp(x_new, x_old, cumv_5m))

    for d in days:
        g = df1m[df1m['date'] == d].copy().reset_index(drop=True)
        if len(g) < 350:
            continue
        # Ensure 390 bars (pad or reindex if minor missing minutes)
        n_bars = len(g)
        O = g['open'].values
        H = g['high'].values
        L = g['low'].values
        C = g['close'].values
        V = g['volume'].fillna(0).values
        VIX = g['vix'].values
        
        d_info = daily_map.get(d, {})
        prev_c = d_info.get('prev_close', O[0])
        if pd.isna(prev_c): prev_c = O[0]
        atr14 = d_info.get('atr14', O[0] * 0.015)
        if pd.isna(atr14) or atr14 <= 0: atr14 = O[0] * 0.015
        rvol20 = d_info.get('rvol20', 0.20)
        if pd.isna(rvol20) or rvol20 <= 0: rvol20 = 0.20

        # Intraday VWAP & VWAP std bands
        typ = (H + L + C) / 3.0
        cum_v = np.maximum(np.cumsum(V), 1.0)
        cum_vp = np.cumsum(typ * V)
        vwap = cum_vp / cum_v
        cum_vp2 = np.cumsum((typ ** 2) * V)
        vwap_var = np.maximum(cum_vp2 / cum_v - vwap ** 2, (O[0] * 0.0005) ** 2)
        vwap_std = np.sqrt(vwap_var)

        # Noise Area bands (14-day lookback)
        lookback_moves = hist_abs_moves[-14:] if len(hist_abs_moves) >= 5 else [np.linspace(0.001, 0.008, n_bars)]
        avg_move_prof = np.mean([m[:n_bars] if len(m) >= n_bars else np.pad(m, (0, n_bars - len(m)), 'edge') for m in lookback_moves], axis=0)
        upper_ref = max(O[0], prev_c)
        lower_ref = min(O[0], prev_c)
        noise_upper = upper_ref * (1.0 + avg_move_prof)
        noise_lower = lower_ref * (1.0 - avg_move_prof)

        # Relative Volume (RVOL) profile at minute m
        lookback_vols = hist_cum_vols[-14:] if len(hist_cum_vols) >= 5 else [cum_v]
        avg_cum_v = np.mean([v[:n_bars] if len(v) >= n_bars else np.pad(v, (0, n_bars - len(v)), 'edge') for v in lookback_vols], axis=0)
        rvol_cum = cum_v / np.maximum(avg_cum_v, 1.0)

        # 1m Volume Surge Ratio (vs 20m SMA)
        vol_sma20 = pd.Series(V).rolling(20, min_periods=3).mean().values
        vol_surge = V / np.maximum(vol_sma20, 1.0)

        # EMAs (9, 21, 50)
        ema9 = pd.Series(C).ewm(span=9, adjust=False).mean().values
        ema21 = pd.Series(C).ewm(span=21, adjust=False).mean().values
        ema50 = pd.Series(C).ewm(span=50, adjust=False).mean().values

        # RSI(2) and RSI(14)
        delta_c = pd.Series(C).diff()
        gain = delta_c.clip(lower=0)
        loss = -delta_c.clip(upper=0)
        rs2 = gain.ewm(alpha=1/2, adjust=False).mean() / np.maximum(loss.ewm(alpha=1/2, adjust=False).mean(), 1e-6)
        rsi2 = (100 - 100 / (1 + rs2)).fillna(50).values
        rs14 = gain.ewm(alpha=1/14, adjust=False).mean() / np.maximum(loss.ewm(alpha=1/14, adjust=False).mean(), 1e-6)
        rsi14 = (100 - 100 / (1 + rs14)).fillna(50).values

        # 1m ATR(14) & Bollinger(20,2) vs Keltner(20, 1.5) Squeeze
        prev_bar_c = np.roll(C, 1)
        prev_bar_c[0] = O[0]
        tr_1m = np.maximum(H - L, np.maximum(np.abs(H - prev_bar_c), np.abs(L - prev_bar_c)))
        atr_1m = pd.Series(tr_1m).rolling(14, min_periods=3).mean().values
        sma20_c = pd.Series(C).rolling(20, min_periods=5).mean().values
        std20_c = pd.Series(C).rolling(20, min_periods=5).std().fillna(0).values
        bb_upper = sma20_c + 2.0 * std20_c
        bb_lower = sma20_c - 2.0 * std20_c
        kc_upper = sma20_c + 1.5 * atr_1m
        kc_lower = sma20_c - 1.5 * atr_1m
        in_squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
        # Squeeze fire: was in squeeze within last 3 bars and now breaks out of BB
        was_sq = pd.Series(in_squeeze.astype(int)).rolling(5, min_periods=1).max().shift(1).fillna(0).values > 0
        sq_fire_long = was_sq & (C > bb_upper) & (C > ema21)
        sq_fire_short = was_sq & (C < bb_lower) & (C < ema21)

        # Day of week (0=Mon .. 4=Fri)
        dow = pd.Timestamp(d).dayofweek
        if ticker in INDEX_0DTE:
            base_dte_days = 0.0
        else:
            # Weekly Friday options: Fri=0, Thu=1, Wed=2, Tue=3, Mon=4
            base_dte_days = float(max(0, 4 - dow))

        # Intraday dynamic IV series
        # Base IV combines live 1m VIX and stock's realized vol multiplier
        vol_mult = max(1.0, rvol20 / 0.16) if ticker not in ['SPY', 'IWM'] else (1.25 if ticker == 'IWM' else 1.0)
        if ticker == 'QQQ': vol_mult = 1.18
        # Realistic intraday IV coupled to 1m VIX + stock vol multiplier + spot-vol skew
        base_iv = (VIX / 100.0) * vol_mult
        ret_from_open = (C - O[0]) / O[0]
        skew_adj = np.where(ret_from_open < 0, -1.5 * ret_from_open, -0.25 * ret_from_open)
        iv_series = np.clip(base_iv * (1.0 + skew_adj), 0.10, 2.20)

        day_records.append({
            'ticker': ticker, 'date': d, 'dow': dow, 'n_bars': n_bars,
            'O': O, 'H': H, 'L': L, 'C': C, 'V': V, 'VIX': VIX,
            'prev_c': prev_c, 'atr14': atr14, 'rvol20': rvol20,
            'vwap': vwap, 'vwap_std': vwap_std,
            'noise_upper': noise_upper, 'noise_lower': noise_lower,
            'rvol_cum': rvol_cum, 'vol_surge': vol_surge,
            'ema9': ema9, 'ema21': ema21, 'ema50': ema50,
            'rsi2': rsi2, 'rsi14': rsi14, 'atr_1m': atr_1m,
            'bb_upper': bb_upper, 'bb_lower': bb_lower,
            'sq_fire_long': sq_fire_long, 'sq_fire_short': sq_fire_short,
            'base_dte_days': base_dte_days, 'iv_series': iv_series
        })

        # Update history for next day
        cur_moves = np.abs(C / O[0] - 1.0)
        hist_abs_moves.append(cur_moves)
        hist_cum_vols.append(cum_v)

    return day_records

print("Loading & computing 1-minute microstructure arrays across all 12 tickers...")
ALL_DATA = {}
for tkr in TICKERS:
    ALL_DATA[tkr] = prepare_ticker_data(tkr)
    print(f"  {tkr:5s}: {len(ALL_DATA[tkr])} full trading days prepared")
