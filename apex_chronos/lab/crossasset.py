"""Volatility-family and cross-asset archive (catalogue items 4, 8-11, 19, 20, 59, 76-80, 82 inputs).

Two sources, both free and public:
  * Yahoo chart API (the same endpoint the bot already uses) for intraday last/overnight values;
  * Cboe's public daily-history CSVs for VIX1D, VIX9D, VIX3M, VIX6M, VVIX, SKEW, COR1M, COR3M.
Every fetch is best-effort: a failure is recorded as missing and never raises into the live session.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .. import data
from ..calendar_nyse import NY

NAN = float('nan')

VOL_SYMBOLS = {'VIX': '^VIX', 'VIX1D': '^VIX1D', 'VIX9D': '^VIX9D', 'VIX3M': '^VIX3M', 'VVIX': '^VVIX', 'SKEW': '^SKEW',
               'VXN': '^VXN', 'RVX': '^RVX', 'MOVE': '^MOVE'}
CROSS_SYMBOLS = {'ES': 'ES=F', 'NQ': 'NQ=F', 'RTY': 'RTY=F', 'BTC': 'BTC-USD', 'UST10Y': '^TNX', 'UST5Y': '^FVX',
                 'DXY': 'DX-Y.NYB', 'USDJPY': 'JPY=X', 'HYG': 'HYG', 'LQD': 'LQD', 'SMH': 'SMH', 'OIL': 'CL=F', 'GOLD': 'GC=F'}
CBOE_HISTORY = ('VIX', 'VIX1D', 'VIX9D', 'VIX3M', 'VIX6M', 'VVIX', 'SKEW', 'COR1M', 'COR3M')
CBOE_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv'


def _last_stats(http, yahoo_sym: str, now: datetime) -> Dict[str, float]:
    """Last price, previous session close, day change, and the overnight return/range since 16:00 ET the day before."""
    payload = data._get(http, yahoo_sym, {'interval': '5m', 'range': '2d', 'includePrePost': 'true'})
    df = data._frame(payload)
    out = {'last': NAN, 'prev_close': NAN, 'chg_pct': NAN, 'overnight_ret': NAN, 'overnight_range': NAN, 'bars': 0}
    if df.empty:
        return out
    meta = ((payload.get('chart') or {}).get('result') or [{}])[0].get('meta', {})
    out['last'] = float(df['close'].iloc[-1])
    pc = meta.get('chartPreviousClose') or meta.get('previousClose')
    out['prev_close'] = float(pc) if pc else NAN
    if pc:
        out['chg_pct'] = float(out['last'] / float(pc) - 1.0)
    day = now.astimezone(NY).date()
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    start = datetime.combine(prev, datetime.min.time(), tzinfo=NY) + timedelta(hours=16)
    end = datetime.combine(day, datetime.min.time(), tzinfo=NY) + timedelta(hours=9, minutes=25)
    seg = df[(df['timestamp'] >= start) & (df['timestamp'] <= min(end, now.astimezone(NY)))]
    out['bars'] = int(len(seg))
    if len(seg) >= 3:
        first, last = float(seg['open'].iloc[0]), float(seg['close'].iloc[-1])
        out['overnight_ret'] = last / first - 1.0
        out['overnight_range'] = float(seg['high'].max() / seg['low'].min() - 1.0)
    return out


def capture(http, now: datetime, groups=('vol', 'cross')) -> Dict[str, Dict[str, float]]:
    """One snapshot of every symbol; a symbol that fails is left out with an `error` marker instead of raising."""
    syms: Dict[str, str] = {}
    if 'vol' in groups:
        syms.update(VOL_SYMBOLS)
    if 'cross' in groups:
        syms.update(CROSS_SYMBOLS)
    out: Dict[str, Dict[str, float]] = {}
    for name, ysym in syms.items():
        try:
            out[name] = _last_stats(http, ysym, now)
        except Exception as e:                                   # noqa: BLE001
            out[name] = {'error': type(e).__name__}
    return out


def save_capture(state_dir: str, d: date, label: str, snap: Dict, taken_at: Optional[datetime] = None) -> str:
    snap = dict(snap)
    snap['_taken_at'] = (taken_at or datetime.now(NY)).isoformat(timespec='seconds')
    p = Path(state_dir) / 'lab' / 'crossasset' / f'{d.isoformat()}.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if p.exists():
        try:
            cur = json.loads(p.read_text())
        except ValueError:
            cur = {}
    cur[label] = snap
    tmp = str(p) + '.tmp'
    Path(tmp).write_text(json.dumps(cur, sort_keys=True, separators=(',', ':')))
    os.replace(tmp, p)
    return str(p)


def load_capture(state_dir: str, d: date) -> Dict:
    p = Path(state_dir) / 'lab' / 'crossasset' / f'{d.isoformat()}.json'
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


# ---- Cboe daily history (EOD archive) -------------------------------------------------------------------------------
def parse_cboe_csv(text: str) -> Dict[str, float]:
    """{YYYY-MM-DD: close} from a Cboe *_History.csv. Date is MM/DD/YYYY in the first column; the close is the last
    numeric column, which is the single value column for SKEW / VVIX and CLOSE for OHLC files."""
    out: Dict[str, float] = {}
    rdr = csv.reader(io.StringIO(text))
    for row in rdr:
        if len(row) < 2:
            continue
        try:
            dt = datetime.strptime(row[0].strip(), '%m/%d/%Y').date()
        except ValueError:
            continue
        vals = []
        for x in row[1:]:
            try:
                vals.append(float(x))
            except ValueError:
                pass
        if vals:
            out[dt.isoformat()] = vals[-1]
    return out


def refresh_cboe_history(http, state_dir: str, syms=CBOE_HISTORY) -> Dict[str, int]:
    """Download each Cboe history file and merge it into state/lab/vixfamily.csv (date, symbol, close). Returns rows per symbol."""
    path = Path(state_dir) / 'lab' / 'vixfamily.csv'
    rows: Dict[tuple, float] = {}
    if path.exists():
        with open(path, newline='') as fh:
            for r in csv.DictReader(fh):
                rows[(r['date'], r['symbol'])] = float(r['close'])
    got = {}
    for s in syms:
        try:
            r = http.get(CBOE_URL.format(sym=s), headers={'User-Agent': 'Mozilla/5.0 (apex-chronos-lab)'}, timeout=20)
            if r.status_code != 200:
                got[s] = 0
                continue
            parsed = parse_cboe_csv(r.text)
            recent = {k: v for k, v in parsed.items() if k >= (date.today() - timedelta(days=400)).isoformat()}
            for k, v in recent.items():
                rows[(k, s)] = v
            got[s] = len(recent)
        except Exception as e:                                   # noqa: BLE001
            print(f'lab cboe history failed for {s}: {type(e).__name__}')
            got[s] = 0
    if any(got.values()):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + '.tmp'
        with open(tmp, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['date', 'symbol', 'close'])
            for (d, s), v in sorted(rows.items()):
                w.writerow([d, s, v])
        os.replace(tmp, path)
    return got


def load_vixfamily(state_dir: str) -> Dict[str, Dict[str, float]]:
    """{symbol: {date: close}}"""
    path = Path(state_dir) / 'lab' / 'vixfamily.csv'
    out: Dict[str, Dict[str, float]] = {}
    if not path.exists():
        return out
    with open(path, newline='') as fh:
        for r in csv.DictReader(fh):
            out.setdefault(r['symbol'], {})[r['date']] = float(r['close'])
    return out


def vol_regime_features(d: date, hist: Dict[str, Dict[str, float]], intraday: Optional[Dict] = None, vix_today: Optional[float] = None) -> Dict[str, float]:
    """VIX-family regime variables for date d. Uses only values dated BEFORE d from the history files (a daily close of
    d is not known at midday) plus, if given, the intraday capture for d (`intraday` = crossasset.capture output)."""
    iso = d.isoformat()

    def prior(sym: str, n: int) -> List[float]:
        h = hist.get(sym, {})
        return [h[k] for k in sorted(h) if k < iso][-n:]
    out: Dict[str, float] = {}
    for s in ('VIX1D', 'VIX9D', 'VIX3M', 'VVIX', 'SKEW', 'COR1M'):
        p = prior(s, 1)
        out[f'{s.lower()}_prev'] = float(p[-1]) if p else NAN
    vix_hist = prior('VIX', 252)
    out['vix_rank_prev'] = NAN
    if len(vix_hist) >= 60:
        cur = vix_hist[-1]
        out['vix_rank_prev'] = float(np.mean(np.array(vix_hist) <= cur))
    v1d = prior('VIX1D', 20)
    out['vix1d_median20'] = float(np.median(v1d)) if len(v1d) >= 10 else NAN
    vix = vix_today if vix_today is not None else NAN
    ivx = (intraday or {}).get('VIX1D', {}).get('last', NAN)
    out['vix1d_intraday'] = float(ivx) if ivx is not None else NAN
    out['vix1d_over_vix'] = float(ivx / vix) if np.isfinite(ivx) and np.isfinite(vix) and vix > 0 else NAN
    out['vix1d_vs_median20'] = float(ivx / out['vix1d_median20']) if np.isfinite(ivx) and np.isfinite(out['vix1d_median20']) and out['vix1d_median20'] > 0 else NAN
    v3 = (intraday or {}).get('VIX3M', {}).get('last', NAN)
    out['vix_over_vix3m'] = float(vix / v3) if np.isfinite(v3) and np.isfinite(vix) and v3 > 0 else NAN
    for k, name in (('VVIX', 'vvix'), ('SKEW', 'skew'), ('VXN', 'vxn'), ('RVX', 'rvx'), ('MOVE', 'move'), ('VIX9D', 'vix9d')):
        val = (intraday or {}).get(k, {}).get('last', NAN)
        out[f'{name}_intraday'] = float(val) if val is not None else NAN
    return out


def btc_overnight_flag(intraday: Dict, threshold: float = 0.03) -> float:
    """79: 1.0 when BTC moved more than `threshold` (absolute) between yesterday's 16:00 ET and 09:25 ET; nan if unknown."""
    r = (intraday or {}).get('BTC', {}).get('overnight_ret', NAN)
    return float(abs(r) > threshold) if r is not None and np.isfinite(r) else NAN


def latest_capture(state_dir: str, d: date, m: int) -> Dict[str, Dict]:
    """(pre-open, latest) captures usable at the close of bar m: {'pre': the 0925 capture, 'latest': newest capture whose
    `_taken_at` is at or before that close}. Missing pieces are empty dicts."""
    from ..calendar_nyse import session_open
    cap = session_open(d) + timedelta(minutes=m + 1)
    allc = load_capture(state_dir, d)
    best, best_t = {}, None
    for label, snap in allc.items():
        try:
            t = datetime.fromisoformat(snap['_taken_at'])
        except (KeyError, TypeError, ValueError):
            continue
        if t <= cap and (best_t is None or t > best_t):
            best, best_t = snap, t
    return {'pre': allc.get('0925', {}), 'latest': best}
