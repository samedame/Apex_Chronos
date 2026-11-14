"""One shadow row per P2 entry: every feature and every pre-declared flag (docs/LAB_PREREG.md, F1-F12).

Causality: every value uses bars 0..m (m = entry bar), archived sessions BEFORE the trade date, option snapshots whose
`taken_at` is at or before the close of bar m, calendar facts known the evening before, and quote-ledger captures tagged
`alert`. Outcome columns (roi, exit) are copied in for convenience and are never an input to any feature or flag.

Flag convention: 1.0 = the trade would be KEPT, 0.0 = it would be withheld, nan = unknown. Flags are logged, never applied.
"""
from __future__ import annotations

import csv
import math
import os
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

from .. import config as C, verdict
from ..pricing import Structure, minutes_to_T
from . import crossasset, events as EV, indicators as I, options_math as om, quote_ledger as QL, snapshots as SN

NAN = float('nan')
INDEX_ETFS = ('SPY', 'QQQ', 'IWM')
LEAD = ['date', 'ticker', 'm_entry', 'm_exit', 'direction', 'side', 'module', 'roi_model', 'roi_fair', 'exit_reason', 'listed_same_day',
        'K1', 'K2', 'width', 'S_entry', 'iv_model_entry', 'credit_model']


def _arr(b: Dict, k: str) -> np.ndarray:
    return np.asarray(b[k], dtype=float)


def prior_series(prior: List[Dict], ticker: str) -> List[Dict[str, np.ndarray]]:
    out = []
    for p in prior:
        t = p.get('tickers', {}).get(ticker)
        if t and len(t.get('C', [])) >= C.MIN_SESSION_BARS:
            out.append({k: _arr(t, k) for k in ('O', 'H', 'L', 'C', 'V')})
    return out


def _f(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else NAN
    except (TypeError, ValueError):
        return NAN


def _flag(cond) -> float:
    return NAN if cond is None or (isinstance(cond, float) and not math.isfinite(cond)) else float(bool(cond))


def trade_structure(trade: Dict, S: float, iv_model: float) -> Structure:
    return Structure(trade['ticker'], 'credit_spread', int(trade['direction']), S, iv_model, minutes_to_T(int(trade['m_entry'])), 0.32)


def evaluate_trade(d: date, trade: Dict, today: Dict, prior: List[Dict], *, state_dir: str, iv_model: Optional[float] = None,
                   roi_fair: Optional[float] = None, day_trades: Sequence[Dict] = (), events: Optional[List[Dict]] = None,
                   earnings_rows: Sequence[Dict] = (), exdiv_rows: Sequence[Dict] = (), hist_vix: Optional[Dict] = None,
                   ledger_recs: Optional[List[Dict]] = None, ev_key: Optional[str] = None, all_tickers_today: Optional[Dict] = None,
                   listed: Optional[bool] = None) -> Dict:
    """`today` = {'O','H','L','C','V','prev_c'} arrays for the ticker (full session; only 0..m is read), plus 'VIX' array under
    key 'vix'. `prior` = archived session dicts (verdict.archived_sessions)."""
    tk, m = trade['ticker'], int(trade['m_entry'])
    O, H, L, Cl, V = (np.asarray(today[k], dtype=float) for k in ('O', 'H', 'L', 'C', 'V'))
    S = float(Cl[m])
    ps = prior_series(prior, tk)
    prior_c = [p['C'] for p in ps]
    row: Dict = {'date': d.isoformat(), 'ticker': tk, 'm_entry': m, 'm_exit': int(trade.get('m_exit', -1)), 'direction': int(trade['direction']),
                 'side': 'put_credit' if int(trade['direction']) == 1 else 'call_credit', 'module': trade.get('module', ''),
                 'roi_model': _f(trade.get('roi')), 'roi_fair': _f(roi_fair), 'exit_reason': trade.get('exit_reason', ''), 'S_entry': S}
    row['listed_same_day'] = _flag(listed if listed is not None else verdict.static_same_day_expiry(tk, d))

    # --- model structure (same strikes the bot used) ------------------------------------------------------------------
    iv0 = _f(iv_model)
    row['iv_model_entry'] = iv0
    K1 = K2 = width = credit = NAN
    if math.isfinite(iv0):
        st = trade_structure(trade, S, iv0)
        K1, K2, width, credit = float(st.K1), float(st.K2), float(st.width), float(st.credit)
    row.update(K1=K1, K2=K2, width=width, credit_model=credit)

    # --- volatility ---------------------------------------------------------------------------------------------------
    n_ret_left = C.SESSION_BARS - 1 - m
    fc = I.rest_of_day_var_forecast(prior_c, Cl, m)
    m14 = I.mean14_rest_forecast(prior_c, m)
    rest = fc['forecast']
    sigma_fair = om.forecast_sigma_ann(rest, n_ret_left)
    row.update(rest_var_forecast=rest, rest_var_mean14=m14, forecast_method=fc['method'], forecast_ratio=fc['ratio'],
               fair_sigma_ann=sigma_fair, sigma_mean14_ann=om.forecast_sigma_ann(m14, n_ret_left),
               rv_morning_ann=I.ann_vol(I.realised_var(Cl, 0, m), m),
               parkinson_ann=_f(math.sqrt(I.parkinson(H[:m + 1], L[:m + 1]) * I.ANN)) if math.isfinite(I.parkinson(H[:m + 1], L[:m + 1])) else NAN,
               garman_klass_ann=_f(math.sqrt(max(I.garman_klass(O[:m + 1], H[:m + 1], L[:m + 1], Cl[:m + 1]), 0) * I.ANN)),
               rogers_satchell_ann=_f(math.sqrt(max(I.rogers_satchell(O[:m + 1], H[:m + 1], L[:m + 1], Cl[:m + 1]), 0) * I.ANN)),
               yang_zhang_ann=_f(math.sqrt(max(I.yang_zhang(O[:m + 1], H[:m + 1], L[:m + 1], Cl[:m + 1]), 0) * I.ANN)) if math.isfinite(I.yang_zhang(O[:m + 1], H[:m + 1], L[:m + 1], Cl[:m + 1])) else NAN,
               jump_ratio=I.jump_ratio(Cl[:m + 1]), max_abs_ret_z=I.max_abs_return_z(Cl[:m + 1]),
               bipower_share=_f(I.bipower_variation(Cl[:m + 1]) / I.realised_var(Cl, 0, m)) if I.realised_var(Cl, 0, m) > 0 else NAN)
    row.update({f'onid_{k}': v for k, v in I.overnight_intraday_split(_f(today.get('prev_c')), float(O[0]), Cl, m).items()})
    ref_var = (fc['baseline_day'] / (C.SESSION_BARS - 1)) if math.isfinite(fc.get('baseline_day', NAN)) else NAN
    row['cusum_stat'] = I.cusum_variance_alarm(Cl[:m + 1], ref_var)['stat']
    row['cusum_alarm'] = I.cusum_variance_alarm(Cl[:m + 1], ref_var)['alarm']

    # --- regime / range -----------------------------------------------------------------------------------------------
    h, l, c, v = H[:m + 1], L[:m + 1], Cl[:m + 1], V[:m + 1]
    row.update(vr5=I.variance_ratio(c, 5), vr10=I.variance_ratio(c, 10), autocorr1=I.autocorr1(c), eff_ratio=I.efficiency_ratio(c),
               eff_ratio30=I.efficiency_ratio(c, 30), hurst=I.hurst_rs(c), chop30=I.choppiness_index(h, l, c, 30),
               squeeze=I.squeeze_flag(h, l, c), bb_width=I.bollinger_width(c), atr30=I.atr_intraday(h, l, c, 30),
               vwap_z=I.vwap_deviation_z(H, L, Cl, V, m), orb15_state=I.orb_state(H, L, Cl, m, 15), first30_ret=I.first_n_return(Cl, float(O[0]), 30),
               bar_ofi30=I.bar_ofi(c, v, 30))
    row['vr5_lt1'] = _flag(row['vr5'] < 1.0) if math.isfinite(row['vr5']) else NAN
    prior_hl = [(float(np.max(p['H'])), float(np.min(p['L']))) for p in ps]
    prior_daily_c = [float(p['C'][-1]) for p in ps]
    prior_hlc = [(float(np.max(p['H'])), float(np.min(p['L'])), float(p['C'][-1])) for p in ps]
    row['atr_daily'] = I.atr_daily(prior_hlc)
    row['day_range_used'] = I.day_range_used(H, L, m, [a - b for a, b in prior_hl])
    row.update(I.nr_inside_flags(prior_hl))
    row.update(I.ibs_and_z(prior_daily_c, prior_hl))
    row.update(I.minutes_since_extreme(H, L, m))
    vp = I.volume_profile(Cl, V, m)
    row.update({f'vp_{k}': v_ for k, v_ in vp.items()})
    row['dist_poc_frac'] = _f(abs(S - vp['poc']) / S) if math.isfinite(vp['poc']) else NAN
    row['round_dist_frac'] = I.round_number_distance(S)['dist_frac']
    row['atr_daily_frac'] = _f(row['atr_daily'] / S) if math.isfinite(row['atr_daily']) else NAN
    na = I.noise_area([(p['O'], p['C']) for p in ps], float(O[0]), _f(today.get('prev_c')), m)
    row.update(na_lower=na['lower'], na_upper=na['upper'], na_sigma=na['sigma'], na_half_width=na['half_width'])
    row['f6_inside_band'] = I.inside_noise_area(S, na)
    sb = I.strike_beyond_band(S, K1, na) if math.isfinite(K1) else NAN
    row['strike_beyond_band'] = sb
    row['f6_strike_ge_1_0'] = _flag(sb >= 1.0) if math.isfinite(sb) else NAN
    row['f6_strike_ge_1_5'] = _flag(sb >= 1.5) if math.isfinite(sb) else NAN
    conf = I.conformal_remaining_range([(p['H'], p['L'], p['C']) for p in ps], m)
    row.update(conformal_up=conf['up'], conformal_down=conf['down'], conformal_n=conf['n'])
    if math.isfinite(K1) and math.isfinite(conf['up']):
        move = (K1 / S - 1.0) if K1 > S else (1.0 - K1 / S)
        bound = conf['up'] if K1 > S else conf['down']
        row['strike_outside_conformal'] = _flag(move > bound)
    else:
        row['strike_outside_conformal'] = NAN
    if all_tickers_today:
        row['breadth_above_vwap'] = I.breadth_above_vwap({t: {k: x[k] for k in ('H', 'L', 'C', 'V')} for t, x in all_tickers_today.items()}, m)
        mc = [all_tickers_today[t]['C'][m] / all_tickers_today[t]['O'][0] - 1.0 for t in EV.MEGACAPS if t in all_tickers_today]
        qq = all_tickers_today.get('QQQ')
        row['megacap_contribution'] = I.megacap_contribution(float(qq['C'][m] / qq['O'][0] - 1.0), mc) if qq else NAN

    # --- touch probability / strike distance / EV at fair vol -------------------------------------------------------------
    row['touch_prob_fair'] = om.touch_probability_var(S, K1, rest) if math.isfinite(K1) else NAN
    row['touch_prob_model_iv'] = om.touch_probability(S, K1, iv0, n_ret_left) if math.isfinite(K1) and math.isfinite(iv0) else NAN
    row['f8_touch_le_25'] = _flag(row['touch_prob_fair'] <= 0.25) if math.isfinite(row['touch_prob_fair']) else NAN
    row['f8_touch_le_35'] = _flag(row['touch_prob_fair'] <= 0.35) if math.isfinite(row['touch_prob_fair']) else NAN
    row['strike_dist_atr30'] = _f(abs(K1 - S) / row['atr30']) if math.isfinite(K1) and math.isfinite(row['atr30']) and row['atr30'] > 0 else NAN
    row['strike_dist_fair_sigmas'] = _f(abs(math.log(K1 / S)) / math.sqrt(rest)) if math.isfinite(K1) and math.isfinite(rest) and rest > 0 else NAN
    row['ev_per_collateral_model_credit'] = (om.ev_per_collateral(credit, width, S, K1, K2, om.minutes_left(m), sigma_fair, int(trade['direction']) == 1)
                                             if math.isfinite(credit) and math.isfinite(sigma_fair) else NAN)
    row['delta_model_short'] = NAN

    # --- option chain (snapshot at or before the entry close) -------------------------------------------------------------
    label = SN.latest_label_at_or_before(state_dir, d, m)
    row['snapshot_label'] = label or ''
    snap = SN.load_snapshot(state_dir, d, label) if label else None
    rows_sd = SN.same_day_rows(snap, tk, d) if snap else []
    spot_snap = _f(((snap or {}).get('roots', {}).get(tk, {}) or {}).get('spot'))
    row['f2_gex_pos'] = row['f1_vrp_ge_1_0'] = row['f1_vrp_ge_1_2'] = row['f1_vrp_ge_1_4'] = NAN
    row.update(iv_short_real=NAN, vrp_ratio=NAN, vrp_ratio_atm=NAN, atm_iv=NAN, straddle_mid=NAN, implied_move=NAN, dist_implied_moves=NAN,
               rr25=NAN, fly25=NAN, short_iv_minus_atm=NAN, term_slope=NAN, gex=NAN, gex_sign=NAN, zero_gamma=NAN, flip_distance_frac=NAN,
               call_wall=NAN, put_wall=NAN, short_at_wall=NAN, pc_volume=NAN, pc_oi=NAN, zero_dte_vol_share=NAN, vanna_exposure=NAN,
               charm_per_hour=NAN, short_volume_oi=NAN, short_oi=NAN)
    if rows_sd and math.isfinite(spot_snap):
        is_call_short = int(trade['direction']) != 1
        side_rows = [r for r in rows_sd if r['t'] == ('c' if is_call_short else 'p')]
        sr = om.nearest_strike_rows(side_rows, K1) if math.isfinite(K1) else None
        row['iv_short_real'] = _f(sr['iv']) if sr and sr.get('iv') else NAN
        row['vrp_ratio'] = om.vrp_ratio(row['iv_short_real'], sigma_fair)
        row['atm_iv'] = om.atm_iv(rows_sd, spot_snap)
        row['vrp_ratio_atm'] = om.vrp_ratio(row['atm_iv'], sigma_fair)
        row['straddle_mid'] = om.atm_straddle_mid(rows_sd, spot_snap)
        row['implied_move'] = _f(row['straddle_mid']) if math.isfinite(row['straddle_mid']) else NAN
        row['dist_implied_moves'] = om.distance_in_implied_moves(S, K1, row['implied_move']) if math.isfinite(K1) else NAN
        row.update({k: _f(v_) for k, v_ in om.skew_metrics(rows_sd, spot_snap, K1 if math.isfinite(K1) else None).items() if k in ('rr25', 'fly25', 'short_iv_minus_atm')})
        row['term_slope'] = om.term_slope(rows_sd, SN.next_expiry_rows(snap, tk, d), spot_snap)
        # open interest at the 09:45 snapshot is the prior-close OI used for the dealer proxy
        oi_snap = SN.load_snapshot(state_dir, d, '0945') or snap
        oi_rows = SN.same_day_rows(oi_snap, tk, d) or rows_sd
        g = om.gex_from_chain(oi_rows, spot_snap)
        row['gex'], row['gex_sign'] = _f(g['gex']), _f(g['sign'])
        row['f2_gex_pos'] = _flag(g['sign'] > 0) if math.isfinite(row['gex_sign']) else NAN
        zg = om.zero_gamma_level(oi_rows, spot_snap, om.minutes_left(m))
        row['zero_gamma'] = zg
        row['flip_distance_frac'] = _f((S - zg) / S) if math.isfinite(zg) else NAN
        walls = om.oi_walls(oi_rows, spot_snap)
        row.update(call_wall=walls['call_wall'], put_wall=walls['put_wall'])
        row['short_at_wall'] = om.wall_coincidence(K1, walls) if math.isfinite(K1) else NAN
        cs = om.chain_stats(rows_sd)
        row.update(pc_volume=cs['pc_volume'], pc_oi=cs['pc_oi'])
        by_exp = (snap or {}).get('roots', {}).get(tk, {}).get('expiries', {})
        row['zero_dte_vol_share'] = om.zero_dte_volume_share(by_exp, d.isoformat())
        vc = om.vanna_charm_exposure(oi_rows, spot_snap, om.minutes_left(m))
        row.update(vanna_exposure=vc['vanna_exposure'], charm_per_hour=vc['charm_exposure_per_hour'])
        row['short_volume_oi'], row['short_oi'] = om.volume_oi_ratio(sr), _f(sr.get('oi')) if sr else NAN
        for thr, name in ((1.0, 'f1_vrp_ge_1_0'), (1.2, 'f1_vrp_ge_1_2'), (1.4, 'f1_vrp_ge_1_4')):
            row[name] = _flag(row['vrp_ratio'] >= thr) if math.isfinite(row['vrp_ratio']) else NAN

    # --- real quotes from the ledger (alert capture) ----------------------------------------------------------------------
    row.update(real_mid_credit=NAN, real_taker_credit=NAN, spread_pct_credit=NAN, quote_age_s=NAN, f4_gate_015=NAN, f4_gate_025=NAN,
               f9_ev_ge_0=NAN, f9_ev_ge_2pct=NAN, ev_per_collateral_real=NAN, xsp_mid_credit=NAN, xsp_spread_pct_credit=NAN, f11_xsp_ge_spy=NAN)
    if ledger_recs and ev_key and math.isfinite(width):
        em = QL.entry_metrics(ledger_recs, ev_key, width)
        row.update(real_mid_credit=em['mid_credit'], real_taker_credit=em['taker_credit'], spread_pct_credit=em['spread_pct_credit'],
                   quote_age_s=em['worst_age_s'], f4_gate_015=em['gate_015'], f4_gate_025=em['gate_025'])
        if math.isfinite(em['mid_credit']) and math.isfinite(sigma_fair):
            ev = om.ev_per_collateral(em['taker_credit'], width, S, K1, K2, om.minutes_left(m), sigma_fair, int(trade['direction']) == 1)
            row['ev_per_collateral_real'] = ev
            row['f9_ev_ge_0'] = _flag(ev >= 0.0) if math.isfinite(ev) else NAN
            row['f9_ev_ge_2pct'] = _flag(ev >= 0.02) if math.isfinite(ev) else NAN
        if tk == 'SPY':
            xm = QL.entry_metrics(ledger_recs, ev_key, 1.0, prefix='xsp_')
            row['xsp_mid_credit'], row['xsp_spread_pct_credit'] = xm['mid_credit'], xm['spread_pct_credit']
            if math.isfinite(xm['taker_credit']) and math.isfinite(em['taker_credit']):
                row['f11_xsp_ge_spy'] = _flag(xm['taker_credit'] / 1.0 >= em['taker_credit'] / max(width, 1e-9))

    # --- events and cross-asset -------------------------------------------------------------------------------------------
    cal = EV.calendar_flags(d, events)
    for k in ('fomc_day', 'cpi_day', 'nfp_day', 'ppi_day', 'pce_day', 'gdp_day', 'claims_day_rule', 'monthly_opex', 'triple_witching', 'vix_expiry',
              'month_end', 'quarter_end', 'turn_of_month', 'half_day', 'day_before_holiday', 'day_after_holiday', 'weekday'):
        row[f'cal_{k}'] = cal[k]
    row['f3_fomc_pass'] = _flag(not EV.fomc_mute(d, m, events))
    ef = EV.earnings_flags(tk, d, list(earnings_rows)) if earnings_rows else {'earnings_window': NAN, 'megacap_reaction_day': NAN}
    row['earnings_window'], row['megacap_reaction_day'] = ef['earnings_window'], ef['megacap_reaction_day']
    xf = EV.exdiv_flags(tk, d, list(exdiv_rows)) if exdiv_rows else {'exdiv_today': NAN, 'exdiv_next_day': NAN}
    row.update(xf)
    spill = ef['megacap_reaction_day'] if tk in ('QQQ', 'SPY') else 0.0
    own = ef['earnings_window'] if tk not in INDEX_ETFS else 0.0
    row['f3_earnings_pass'] = _flag(not (own == 1.0 or spill == 1.0)) if not (math.isnan(own) or math.isnan(spill)) else NAN
    caps = crossasset.latest_capture(state_dir, d, m)
    snap0925, latest_cap = caps['pre'], caps['latest']
    vix_today = float(np.asarray(today.get('vix', [NAN]), dtype=float)[m]) if len(today.get('vix', [])) > m else NAN
    vr = crossasset.vol_regime_features(d, hist_vix or {}, latest_cap, vix_today)
    row.update({f'reg_{k}': v_ for k, v_ in vr.items()})
    vm = vr.get('vix1d_vs_median20', NAN)
    row['f10_vix1d_ok'] = _flag(vm <= 1.5) if math.isfinite(vm) else NAN
    row['btc_overnight_big'] = crossasset.btc_overnight_flag(snap0925) if tk in ('COIN', 'MSTR') else NAN
    row['vix_entry'] = vix_today

    # --- portfolio: correlated index slots --------------------------------------------------------------------------------
    conflict = 0.0
    if tk in INDEX_ETFS:
        for o in day_trades:
            if o is trade or o.get('ticker') == tk and int(o['m_entry']) == m:
                continue
            if o.get('ticker') in INDEX_ETFS and int(o['m_entry']) < m <= int(o.get('m_exit', 10 ** 6)):
                conflict = 1.0
    row['index_slot_conflict'] = conflict
    row['f5_index_cap_pass'] = _flag(conflict == 0.0)
    row['open_after_1530'] = _flag(int(trade.get('m_exit', 0)) > 360)
    return row


# ---- persistence --------------------------------------------------------------------------------------------------------
def shadow_path(ledger_dir: str, name: str = 'p2_shadow.csv') -> Path:
    return Path(ledger_dir) / 'lab' / name


def write_rows(ledger_dir: str, d: date, rows: List[Dict], name: str = 'p2_shadow.csv') -> str:
    """Replace the day's rows in ledger/lab/<name> (append-only across days; the same day is rewritten by the EOD rerun).
    The forward ledger is p2_shadow.csv; the archive replay writes p2_shadow_backfill.csv so the two never mix."""
    p = shadow_path(ledger_dir, name)
    old: List[Dict] = []
    if p.exists():
        with open(p, newline='') as fh:
            old = [r for r in csv.DictReader(fh) if r['date'] != d.isoformat()]
    new = [{k: ('' if isinstance(v, float) and not math.isfinite(v) else v) for k, v in r.items()} for r in rows]
    cols = list(LEAD)
    for r in old + new:
        for k in r:
            if k not in cols:
                cols.append(k)
    allrows = sorted(old + new, key=lambda r: (r['date'], int(r['m_entry']), r['ticker']))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, restval='')
        w.writeheader()
        w.writerows(allrows)
    os.replace(tmp, p)
    return str(p)


def read_rows(path: str) -> List[Dict]:
    with open(path, newline='') as fh:
        return list(csv.DictReader(fh))


def evaluate_day(d: date, days: Dict, trades: Iterable[Dict], *, state_dir: str, fair: Optional[Dict] = None, ivs: Optional[Dict] = None,
                 events: Optional[List[Dict]] = None, prior: Optional[List[Dict]] = None) -> List[Dict]:
    """Rows for every P2 trade of the day. `days` = {ticker: TickerDay} (runner) -- converted here to plain arrays."""
    from ..features import compute_features
    p2 = [t for t in trades if t.get('phase') == 'P2' and t['ticker'] in days]
    if not p2:
        return []
    prior = prior if prior is not None else verdict.archived_sessions(state_dir, d, 20)
    hist_vix = crossasset.load_vixfamily(state_dir)
    earn, divs = EV.load_cache(state_dir, 'earnings'), EV.load_cache(state_dir, 'dividends')
    led = QL.load_ledger(state_dir, d)
    plain = {t: {'O': td.O, 'H': td.H, 'L': td.L, 'C': td.C, 'V': td.V, 'vix': td.vix, 'prev_c': float(td.ctx.prev_c)} for t, td in days.items()}
    rows = []
    for t in p2:
        try:
            key = (d.isoformat(), t['ticker'], int(t['m_entry']))
            iv = (ivs or {}).get(key)
            if iv is None:
                td = days[t['ticker']]
                iv = float(compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix).iv[int(t['m_entry'])])
            ev_key = f"{d.isoformat()}|ENTRY|{t['ticker']}|{t['module']}|{int(t['m_entry'])}"
            rows.append(evaluate_trade(d, t, plain[t['ticker']], prior, state_dir=state_dir, iv_model=iv, roi_fair=(fair or {}).get(key),
                                       day_trades=p2, events=events, earnings_rows=earn, exdiv_rows=divs, hist_vix=hist_vix,
                                       ledger_recs=led, ev_key=ev_key, all_tickers_today=plain))
        except Exception as e:                                       # noqa: BLE001 - one bad trade never blocks the rest
            print(f'lab shadow row failed for {t.get("ticker")} {d}: {type(e).__name__}: {str(e)[:100]}')
    return rows
