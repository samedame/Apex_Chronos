"""Disposition of all 110 catalogue items (research of 2026-11-14) for Apex Chronos P2.

status: BUILT   code exists and runs observation-only; `where` names real functions (a test imports every one of them) and
                `cols` names columns of ledger/lab/p2_shadow.csv (a test checks them against a real shadow row).
        DEFER   needs paid data, history the repo lacks or a human check; `note` states the exact trigger that unblocks it.
        REJECT  evidence says no, or it cannot apply to this bot; not built; `note` gives the reason.
        INFO    context, nothing to build.
mode:   LOG data capture | CALC causal feature in the shadow row | FLAG pre-declared keep/mute decision (never applied)
        TEST archive-replay or forward test | RULE static guard
Pre-registered test or flag ids (R1-R8, F1-F12) refer to docs/LAB_PREREG.md.
"""
from __future__ import annotations

import importlib
from typing import Dict, List, NamedTuple, Tuple

I, OM, EV, CA, SN, QL, ST, KS, FI, RQ = ('indicators', 'options_math', 'events', 'crossasset', 'snapshots', 'quote_ledger', 'stats',
                                         'killswitch_alt', 'fills', 'regrade_quotes')


class Item(NamedTuple):
    n: int
    category: str
    item: str
    tier: str
    status: str
    mode: str
    where: Tuple[str, ...]
    cols: Tuple[str, ...]
    note: str


def _b(n, cat, item, tier, mode, where, cols=(), note=''):
    return Item(n, cat, item, tier, 'BUILT', mode, tuple(f'{w[0]}:{w[1]}' for w in where), tuple(cols), note)


def _d(n, cat, item, tier, note):
    return Item(n, cat, item, tier, 'DEFER', '', (), (), note)


def _r(n, cat, item, tier, note):
    return Item(n, cat, item, tier, 'REJECT', '', (), (), note)


def _i(n, cat, item, tier, note):
    return Item(n, cat, item, tier, 'INFO', '', (), (), note)


ITEMS: List[Item] = [
    _b(1, 'Data', 'Real NBBO bid/ask/size ledger at alert, +30 s, +60 s, every minute, exit', 'T1', 'LOG', [(QL, 'QuoteTracker'), (QL, 'entry_metrics'), (QL, 'spread_path')],
       ('real_mid_credit', 'real_taker_credit', 'quote_age_s'), 'Sizes stored raw; unit unverified (item 62).'),
    _b(2, 'Vol', 'IV at the traded strike from the real chain', 'T1', 'CALC', [(OM, 'nearest_strike_rows'), (SN, 'take_snapshot')], ('iv_short_real', 'atm_iv')),
    _b(3, 'Vol', 'VRP at strike = real IV / forecast sigma', 'T1', 'FLAG', [(OM, 'vrp_ratio'), (OM, 'forecast_sigma_ann')],
       ('vrp_ratio', 'f1_vrp_ge_1_0', 'f1_vrp_ge_1_2', 'f1_vrp_ge_1_4'), 'F1; thresholds {1.0, 1.2, 1.4}.'),
    _b(4, 'Vol', 'VIX1D level and VIX1D/VIX ratio', 'T1', 'FLAG', [(CA, 'vol_regime_features'), (CA, 'refresh_cboe_history')],
       ('reg_vix1d_over_vix', 'reg_vix1d_vs_median20', 'f10_vix1d_ok'), 'F10.'),
    _b(5, 'Vol', 'Seasonality-adjusted remaining-session variance forecast', 'T1', 'TEST', [(I, 'rest_of_day_var_forecast'), (I, 'qlike'), (ST, 'diebold_mariano')],
       ('rest_var_forecast', 'rest_var_mean14', 'fair_sigma_ann'), 'R8 (QLIKE + Diebold-Mariano against the 14-session mean).'),
    _b(6, 'Vol', 'Morning realised variance (open to entry)', 'T1', 'CALC', [(I, 'realised_var'), (I, 'ann_vol')], ('rv_morning_ann',)),
    _b(7, 'Vol', 'Overnight vs intraday vol split', 'T2', 'CALC', [(I, 'overnight_intraday_split')]),
    _b(8, 'Vol', 'IV rank / percentile (VIX)', 'T3', 'CALC', [(CA, 'vol_regime_features')], ('reg_vix_rank_prev',)),
    _b(9, 'Vol', 'VIX9D, VIX3M, VIX/VIX3M slope', 'T2', 'CALC', [(CA, 'vol_regime_features')], ('reg_vix_over_vix3m',)),
    _b(10, 'Vol', 'VVIX', 'T2', 'LOG', [(CA, 'capture'), (CA, 'vol_regime_features')], ('reg_vvix_intraday',)),
    _b(11, 'Vol', 'SKEW index', 'T3', 'LOG', [(CA, 'capture')], ('reg_skew_intraday',)),
    _b(12, 'Vol', 'Smile skew / risk reversal at traded strikes', 'T2', 'CALC', [(OM, 'skew_metrics')], ('rr25', 'short_iv_minus_atm')),
    _b(13, 'Vol', 'Smile curvature (butterfly)', 'T3', 'CALC', [(OM, 'skew_metrics')], ('fly25',)),
    _b(14, 'Vol', '0DTE vs next-expiry IV slope', 'T2', 'CALC', [(OM, 'term_slope'), (SN, 'next_expiry_rows')], ('term_slope',)),
    _b(15, 'Vol', 'Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang estimators', 'T2', 'CALC',
       [(I, 'parkinson'), (I, 'garman_klass'), (I, 'rogers_satchell'), (I, 'yang_zhang')], ('parkinson_ann', 'garman_klass_ann', 'rogers_satchell_ann', 'yang_zhang_ann')),
    _b(16, 'Vol', 'Two-scale, realised kernel family, bipower variation', 'T3', 'CALC', [(I, 'two_scale_rv'), (I, 'bipower_variation')], ('bipower_share',)),
    _b(17, 'Vol', 'GARCH(1,1) forecast', 'T3', 'CALC', [(I, 'garch11_forecast')], note='Compared with HAR/seasonality in R8 only if time allows; EGARCH/GJR not built (same data, no extra information expected).'),
    _b(18, 'Vol', 'Jump measures (max |1-min return| z, jump ratio)', 'T2', 'CALC', [(I, 'max_abs_return_z'), (I, 'jump_ratio')], ('max_abs_ret_z', 'jump_ratio')),
    _b(19, 'Vol', 'Implied correlation (COR1M, COR3M)', 'T3', 'LOG', [(CA, 'refresh_cboe_history')], note='Daily history archived to state/lab/vixfamily.csv.'),
    _b(20, 'Vol', 'MOVE, VXN, RVX', 'T3', 'LOG', [(CA, 'capture')], ('reg_vxn_intraday', 'reg_rvx_intraday', 'reg_move_intraday')),
    _b(21, 'Vol', 'ATM straddle implied move; strike distance in implied moves', 'T1', 'CALC',
       [(OM, 'implied_move_from_straddle'), (OM, 'atm_straddle_mid'), (OM, 'distance_in_implied_moves')], ('straddle_mid', 'implied_move', 'dist_implied_moves'),
       'The research note\'s "x 0.8" haircut double-counts; the straddle mid already is the expected absolute move.'),
    _b(22, 'Dealer', 'Net GEX sign (prior-close OI proxy)', 'T1', 'FLAG', [(OM, 'gex_from_chain')], ('gex', 'gex_sign', 'f2_gex_pos'), 'F2. Proxy with an assumed sign convention.'),
    _b(23, 'Dealer', 'Zero-gamma / flip distance', 'T2', 'CALC', [(OM, 'zero_gamma_level')], ('zero_gamma', 'flip_distance_frac')),
    _b(24, 'Dealer', 'Call wall / put wall; coincidence with the short strike', 'T2', 'CALC', [(OM, 'oi_walls'), (OM, 'wall_coincidence')], ('call_wall', 'put_wall', 'short_at_wall')),
    _r(25, 'Dealer', 'Max pain', 'REJ', 'No credible evidence; an expiry-close concept.'),
    _b(26, 'Dealer', 'Charm / vanna exposure', 'T3', 'CALC', [(OM, 'vanna_charm_exposure'), (OM, 'bs_vanna'), (OM, 'bs_charm')], ('vanna_exposure', 'charm_per_hour')),
    _d(27, 'Dealer', 'Customer net 0DTE positioning (Cboe open-close)', 'T3', 'Paid Cboe DataShop; revisit only if a budget is approved after the Feb-2027 verdict.'),
    _b(28, 'Dealer', '0DTE share of option volume', 'T3', 'CALC', [(OM, 'zero_dte_volume_share')], ('zero_dte_vol_share',)),
    _b(29, 'Dealer', 'Put/call volume and OI ratios', 'T3', 'CALC', [(OM, 'chain_stats')], ('pc_volume', 'pc_oi')),
    _r(30, 'Dealer', 'Unusual options activity', 'REJ', 'Paid, no independent evidence of value for range prediction.'),
    _b(31, 'Dealer', 'Monthly opex, triple witching, VIX expiry', 'T2', 'CALC', [(EV, 'calendar_flags'), (EV, 'monthly_opex'), (EV, 'triple_witching'), (EV, 'vix_expiry')],
       ('cal_monthly_opex', 'cal_triple_witching', 'cal_vix_expiry')),
    _i(32, 'Dealer', 'Index rebalances, MOC imbalances', 'T3', 'Fall after P2\'s exit window; triple-witching flag covers the quarterly rebalance day.'),
    _i(33, 'Dealer', 'Leveraged-ETF rebalancing flow', 'T3', 'Late-day effect; covered by the open_after_1530 flag (item 99).'),
    _b(34, 'Dealer', 'Single-name gamma squeeze', 'T3', 'CALC', [(OM, 'gex_from_chain')], ('gex', 'gex_sign'), 'Per-ticker GEX is computed for every single name, not only the indexes.'),
    _b(35, 'Price', 'Opening range breakout state (15 min)', 'T3', 'CALC', [(I, 'orb_state')], ('orb15_state',), 'Previously tested as a signal (null); logged only as a regime descriptor.'),
    _b(36, 'Price', 'Prior-day high/low/close, NR4/NR7 and inside-day flags', 'T2', 'CALC', [(I, 'nr_inside_flags'), (I, 'day_range_used')], ('day_range_used',)),
    _r(37, 'Price', 'Gap size / gap fill as a filter', 'REJ', 'Tested null in Studies A/B/C; the overnight/intraday split (item 7) is logged instead.'),
    _b(38, 'Price', 'VWAP, anchored VWAP, VWAP deviation z', 'T3', 'CALC', [(I, 'vwap_series'), (I, 'vwap_deviation_z'), (I, 'anchored_vwap')], ('vwap_z',)),
    _b(39, 'Price', 'Volume profile (POC, value area)', 'T3', 'CALC', [(I, 'volume_profile')], ('dist_poc_frac',)),
    _b(40, 'Price', 'Noise Area (Zarattini-Aziz-Barbon)', 'T1', 'FLAG', [(I, 'noise_area'), (I, 'inside_noise_area'), (I, 'strike_beyond_band')],
       ('na_half_width', 'f6_inside_band', 'strike_beyond_band', 'f6_strike_ge_1_0', 'f6_strike_ge_1_5'), 'F6; R1-R3.'),
    _b(41, 'Price', 'ATR; strike distance in ATR units', 'T2', 'CALC', [(I, 'atr_intraday'), (I, 'atr_daily')], ('atr30', 'atr_daily_frac', 'strike_dist_atr30')),
    _b(42, 'Price', 'Bollinger width / squeeze, Donchian', 'T3', 'CALC', [(I, 'bollinger_width'), (I, 'squeeze_flag'), (I, 'donchian')], ('bb_width', 'squeeze')),
    _r(43, 'Price', 'Pivot points, Fibonacci levels', 'REJ', 'No credible evidence. (indicators.pivot_levels exists for completeness and is not part of the shadow row.)'),
    _b(44, 'Price', 'Round-number distance', 'T3', 'CALC', [(I, 'round_number_distance')], ('round_dist_frac',)),
    _r(45, 'Price', 'ADX/DMI, Aroon, Supertrend, Ichimoku, MA ribbons', 'REJ', 'Directional tools; no evidence they forecast intraday range; direction already null in Study B.'),
    _r(46, 'Price', 'RSI, stochastics, CCI, Williams %R, MFI, MACD, ROC', 'REJ', 'Same as item 45.'),
    _b(47, 'Price', 'Kaufman efficiency ratio', 'T2', 'CALC', [(I, 'efficiency_ratio')], ('eff_ratio', 'eff_ratio30'), 'Robustness check for R4.'),
    _b(48, 'Price', 'Variance ratio VR(5), 1-minute autocorrelation', 'T1', 'FLAG', [(I, 'variance_ratio'), (I, 'autocorr1')], ('vr5', 'vr10', 'autocorr1', 'vr5_lt1'), 'F7; R4.'),
    _b(49, 'Price', 'Hurst exponent', 'T3', 'CALC', [(I, 'hurst_rs')], ('hurst',)),
    _b(50, 'Price', 'Choppiness index', 'T3', 'CALC', [(I, 'choppiness_index')], ('chop30',)),
    _b(51, 'Price', 'NR4/NR7, inside days', 'T3', 'CALC', [(I, 'nr_inside_flags')]),
    _b(52, 'Price', 'Percent of day range used; minutes since new high/low', 'T2', 'CALC', [(I, 'day_range_used'), (I, 'minutes_since_extreme')], ('day_range_used',)),
    _b(53, 'Price', 'IBS and z-score of the prior close', 'T3', 'CALC', [(I, 'ibs_and_z')]),
    _b(54, 'Price', 'Day-of-week and time-of-day', 'T3', 'CALC', [(EV, 'calendar_flags')], ('cal_weekday', 'm_entry')),
    _b(55, 'Price', 'First-30-minute return (late-day momentum predictor)', 'T2', 'CALC', [(I, 'first_n_return')], ('first30_ret', 'open_after_1530')),
    _i(56, 'Price', 'Trend-day vs range-day classifier', 'T3', 'Covered by Noise Area (item 40) and variance ratio (item 48); a separate classifier would only add parameters.'),
    _d(57, 'Internals', 'NYSE TICK, ADD, VOLD, TRIN', 'T3', 'No free real-time feed; revisit if a free source of the four series is found.'),
    _b(58, 'Internals', '% of tickers above VWAP (breadth proxy)', 'T3', 'CALC', [(I, 'breadth_above_vwap')], ('breadth_above_vwap',), 'From the 12 watched tickers only, not the S&P 500.'),
    _b(59, 'Internals', 'Dispersion / implied correlation', 'T3', 'LOG', [(CA, 'refresh_cboe_history')], note='COR1M/COR3M daily history.'),
    _b(60, 'Internals', 'Mega-cap contribution to QQQ', 'T3', 'CALC', [(I, 'megacap_contribution')], ('megacap_contribution',)),
    _b(61, 'Liquidity', 'Option spread as % of credit', 'T1', 'FLAG', [(OM, 'credit_spread_metrics'), (OM, 'leg_half_spread')], ('spread_pct_credit', 'f4_gate_015', 'f4_gate_025'), 'F4; gates 0.15 and 0.25.'),
    _d(62, 'Liquidity', 'Quote size / depth gates', 'T2', 'Sizes are logged raw with size_unit=unverified. Unblock: confirm Tradier option bidsize/asksize units against a second source, then change snapshots.SIZE_UNIT.'),
    _b(63, 'Liquidity', 'Quote staleness', 'T1', 'FLAG', [(OM, 'quote_age_seconds')], ('quote_age_s', 'f4_gate_015'), 'Stale means older than 5 s.'),
    _r(64, 'Liquidity', 'RVOL by time of day', 'REJ', 'Three thresholds already null; option-chain volume at the strike (item 66) is the better liquidity measure.'),
    _b(65, 'Liquidity', 'Bar-level order-flow imbalance proxy', 'T3', 'CALC', [(I, 'bar_ofi')], ('bar_ofi30',), 'VPIN, Kyle lambda and Lee-Ready need tick data (not available).'),
    _b(66, 'Liquidity', 'Option volume/OI at the traded strike', 'T3', 'CALC', [(OM, 'volume_oi_ratio')], ('short_volume_oi', 'short_oi')),
    _b(67, 'Liquidity', 'Single-name minimum-liquidity check', 'T2', 'CALC', [(OM, 'volume_oi_ratio')], ('short_oi', 'spread_pct_credit')),
    _b(68, 'Event', 'FOMC, CPI, PPI, PCE, NFP, claims, ISM, GDP', 'T1', 'FLAG', [(EV, 'macro_flags'), (EV, 'fomc_mute')], ('cal_fomc_day', 'cal_cpi_day', 'cal_nfp_day', 'f3_fomc_pass'),
       'F3 mutes the FOMC decision day from 12:00 ET; other releases are descriptive. Dates cover Nov 2026 onward only.'),
    _b(69, 'Event', 'Earnings (own name; mega-cap spillover to QQQ/SPY)', 'T1', 'FLAG', [(EV, 'earnings_flags'), (EV, 'refresh_calendar')], ('earnings_window', 'megacap_reaction_day', 'f3_earnings_pass'),
       'Nasdaq public calendar; an unreachable calendar gives nan (unknown), never 0.'),
    _d(70, 'Event', 'Treasury auctions, Fed speakers', 'T3', 'No free structured calendar worth maintaining; revisit if the event-mute test shows event days matter.'),
    _b(71, 'Event', 'Holidays, half days, adjacent closures', 'T2', 'CALC', [(EV, 'adjacent_closure'), (EV, 'calendar_flags')], ('cal_half_day', 'cal_day_before_holiday', 'cal_day_after_holiday')),
    _b(72, 'Event', 'Month-end, quarter-end, turn of month', 'T3', 'CALC', [(EV, 'month_end'), (EV, 'quarter_end'), (EV, 'turn_of_month')], ('cal_month_end', 'cal_quarter_end', 'cal_turn_of_month')),
    _b(73, 'Event', 'Ex-dividend dates', 'T1', 'RULE', [(EV, 'exdiv_flags')], ('exdiv_today', 'exdiv_next_day'), 'Logged; an early-exercise guard matters only for deep in-the-money shorts, which P2 never holds.'),
    _d(74, 'Event', 'Headline NLP / tariff and geopolitical risk score', 'T3', 'Needs a paid news-sentiment API and a validated score; revisit after the verdict.'),
    _r(75, 'Event', 'Social / retail sentiment', 'REJ', 'No evidence for intraday range prediction.'),
    _b(76, 'Cross-asset', 'ES/NQ/RTY futures overnight behaviour', 'T3', 'LOG', [(CA, 'capture')], note='Overnight return and range are stored at 09:25 in state/lab/crossasset/.'),
    _b(77, 'Cross-asset', '5y/10y yields, DXY, USDJPY, oil, gold', 'T3', 'LOG', [(CA, 'capture')], note='Same capture.'),
    _b(78, 'Cross-asset', 'HYG/LQD credit proxy', 'T3', 'LOG', [(CA, 'capture')]),
    _b(79, 'Cross-asset', 'Bitcoin overnight move for COIN/MSTR', 'T2', 'FLAG', [(CA, 'btc_overnight_flag')], ('btc_overnight_big',)),
    _b(80, 'Cross-asset', 'SMH for NVDA/AMD', 'T3', 'LOG', [(CA, 'capture')]),
    _d(81, 'Regime/ML', 'Hidden Markov / Markov-switching', 'T3', 'Needs at least five years of daily history; revisit after the ThetaData/long-history regrade.'),
    _b(82, 'Regime/ML', 'Change-point detection (CUSUM on 1-minute variance)', 'T2', 'CALC', [(I, 'cusum_variance_alarm')], ('cusum_stat', 'cusum_alarm')),
    _r(83, 'Regime/ML', 'k-means / GMM day clustering', 'REJ', 'Too few days; certain overfit.'),
    _r(84, 'Regime/ML', 'Meta-labelling (random forest / gradient boosting)', 'REJ', 'Revisit only after 1,000 real-quote-graded observations.'),
    _b(85, 'Regime/ML', 'Conformal bound on remaining high/low', 'T2', 'CALC', [(I, 'conformal_remaining_range')], ('conformal_up', 'conformal_down', 'strike_outside_conformal')),
    _b(86, 'Regime/ML', 'Touch probability of the short strike', 'T1', 'FLAG', [(OM, 'touch_probability'), (OM, 'touch_probability_var')],
       ('touch_prob_fair', 'touch_prob_model_iv', 'f8_touch_le_25', 'f8_touch_le_35'), 'F8; R5, R6.'),
    _b(87, 'Regime/ML', 'Bayesian shrinkage of per-ticker means', 'T2', 'TEST', [(ST, 'shrunk_ticker_means')], note='Applied at verdict time to the real-quote ledger.'),
    _d(88, 'Structure', 'Delta vs expected-move vs ATR strike selection', 'T2', 'Earlier nulls used model deltas (model IV 2-2.7x too high). Unblock: a real-quote historical regrade (item 110).'),
    _b(89, 'Structure', 'Minimum credit/width on real quotes', 'T1', 'CALC', [(QL, 'entry_metrics'), (OM, 'credit_spread_metrics')], ('real_mid_credit', 'spread_pct_credit')),
    _b(90, 'Structure', 'EV per unit of collateral at fair vol', 'T1', 'FLAG', [(OM, 'ev_per_collateral'), (OM, 'fair_credit_spread_value')],
       ('ev_per_collateral_model_credit', 'ev_per_collateral_real', 'f9_ev_ge_0', 'f9_ev_ge_2pct'), 'F9. Terminal-expiry EV proxy: P2 exits early, so this is a screen, not a forecast.'),
    _d(91, 'Structure', 'Iron condor / butterfly / broken-wing butterfly', 'T3', 'Four legs double spread cost; evidence is net-negative. Unblock: real-quote regrade (item 110) shows the single-sided spread clears costs.'),
    _r(92, 'Structure', 'Ratios, calendars, diagonals with a 1DTE wing', 'REJ', 'The long wing carries the overnight premium the wrong way and adds gap risk.'),
    _d(93, 'Structure', 'Far-OTM long-wing tail hedge', 'T2', 'Unblock: real-quote regrade to measure hedge cost against the avoided tail.'),
    _b(94, 'Structure', 'XSP side-by-side for every SPY alert', 'T1', 'FLAG', [(QL, 'xsp_equivalent_strike'), (SN, 'snapshot_roots')], ('xsp_mid_credit', 'xsp_spread_pct_credit', 'f11_xsp_ge_spy'), 'F11. Section 1256 treatment is reported separately; not a P&L input.'),
    _r(95, 'Structure', 'SPX, /ES, /MES options', 'T3', 'Contract size is far beyond a $1,500 account.'),
    _b(96, 'Execution', 'Fill model (fill vs mid by spread width and latency)', 'T1', 'LOG', [(FI, 'fill_model'), (FI, 'add_fill')]),
    _b(97, 'Execution', 'Human latency (Discord alert to fill)', 'T1', 'LOG', [(FI, 'add_fill')], note='Manual: python -m apex_chronos.lab.fills add ...'),
    _b(98, 'Execution', 'Profit-take 25/50/75% of credit', 'T2', 'LOG', [(QL, 'spread_path')], note='The per-minute spread path is stored; the replay of take-profit levels runs at the verdict, on real quotes only.'),
    _b(99, 'Execution', 'Hard time exit at 15:30', 'T2', 'FLAG', [(I, 'first_n_return')], ('open_after_1530',), 'Counts P2 trades still open after 15:30; descriptive in the replay.'),
    _r(100, 'Execution', 'Stop on underlying instead of spread value', 'REJ', 'Twenty-eight exit variants already null.'),
    _r(101, 'Execution', 'Rolls and adjustments', 'REJ', 'Not viable for an alert-only bot with human latency.'),
    _r(102, 'Execution', 'Delta hedging with shares', 'REJ', 'Not viable at this account size and latency.'),
    _b(103, 'Risk', 'Correlation-aware slot cap (one index ETF at a time)', 'T1', 'FLAG', [(OM, 'aggregate_greeks')], ('index_slot_conflict', 'f5_index_cap_pass'), 'F5; replay reports worst session and CVaR95.'),
    _b(104, 'Risk', 'Aggregate delta/gamma/vega across open slots', 'T2', 'CALC', [(OM, 'aggregate_greeks')], note='Computed from the quote-ledger greeks.'),
    _b(105, 'Risk', 'Fractional Kelly and volatility-target sizing', 'T3', 'CALC', [(ST, 'fractional_kelly'), (ST, 'vol_target_size')], note='Defined only once a real-quote mean exists; no live use.'),
    _b(106, 'Risk', 'Kill-switch alternatives (VIX1D spike, term inversion, 3 stops, equity curve)', 'T2', 'FLAG', [(KS, 'evaluate_all'), (KS, 'replay_rule')], note='Logged to state/lab/kill_alt.json; judged on worst session and CVaR95, not on mean.'),
    Item(107, 'Risk', 'Stress replays (Feb 2018, Mar 2020, Aug 2024, Apr 2025)', 'T1', 'BUILT', 'TEST', ('scripts/lab_stress.py',), (),
         'Harness built and tested on a synthetic crash fixture. The real stress sessions are not in the repo archive (it starts 2026-10-16).'),
    _b(108, 'Risk', 'Risk of ruin from the session-clustered distribution', 'T1', 'TEST', [(ST, 'risk_of_ruin')], note='Run at the verdict.'),
    _i(109, 'Regulatory', 'Pattern-day-trader rule', 'info', 'The $25k minimum was removed (FINRA 4210 amendments, effective 2026-06-04, phase-in to 2027-10-20). Check the broker; sizing is the binding constraint at $1,500.'),
    _b(110, 'Data', 'Historical NBBO regrade of every archived trade', 'T1', 'TEST', [(RQ, 'regrade_table'), (RQ, 'regrade_trade'), (RQ, 'load_quotes')],
       note='Code is ready and tested on synthetic quotes. Unblock: a one-to-two-month ThetaData (or similar) subscription and a CSV export in the documented format.'),
]


def status_counts() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in ITEMS:
        out[it.status] = out.get(it.status, 0) + 1
    return out


def resolve(ref: str):
    """'indicators:qlike' -> the function object."""
    mod, name = ref.split(':')
    return getattr(importlib.import_module(f'apex_chronos.lab.{mod}'), name)


def to_markdown() -> str:
    c = status_counts()
    lines = ['# Lab catalogue: disposition of all 110 research items', '',
             'Generated by `python -m apex_chronos.lab.catalogue --write docs/LAB_CATALOGUE.md`; do not edit by hand. '
             'Status: **BUILT** (observation-only code, imported by a test), **DEFER** (needs data or a check; the trigger is stated), '
             '**REJECT** (evidence against, not built), **INFO** (nothing to build).', '',
             f"Counts: {', '.join(f'{k} {v}' for k, v in sorted(c.items()))} (total {len(ITEMS)}).", '',
             '| # | Category | Item | Tier | Status | Mode | Code | Shadow-row columns | Note |', '|---|---|---|---|---|---|---|---|---|']
    for it in ITEMS:
        lines.append(f"| {it.n} | {it.category} | {it.item} | {it.tier} | {it.status} | {it.mode} | "
                     f"{'<br>'.join('`' + w + '`' for w in it.where)} | {', '.join('`' + x + '`' for x in it.cols)} | {it.note} |")
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', default=None)
    a = ap.parse_args()
    md = to_markdown()
    if a.write:
        with open(a.write, 'w') as fh:
            fh.write(md)
    print(status_counts())
