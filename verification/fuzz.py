"""Perturbed copies of the recorded market, for differential fuzzing.

Three weeks of real data only exercise a handful of the scheduler's branches. This
writes a *different but realistic* market from the real one, so the original
backtest code and the bot can be compared on many more signals, slot collisions
and exit paths. The 1-minute files are perturbed; the daily and 5-minute files are
linked unchanged (so gaps versus the prior close vary, which is what drives the
gap modules).

  prices   per ticker-day: a gap shift (N(0, 0.6%)), an intraday drift (N(0, 1.2%) over the day)
           and a mean-reverting wiggle (AR(1), ~4bp/min) applied identically to O/H/L/C of a bar,
           so every bar stays a valid OHLC bar
  volume   per day lognormal(0.45) x per minute lognormal(0.35)  -> RVOL crosses the filter thresholds
  VIX      per day lognormal(0.20) with a slow wiggle, clipped to [9, 70]
"""
from __future__ import annotations

import os
from typing import List

import numpy as np
import pandas as pd

NY = 'America/New_York'


def _et_date(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.tz_convert(NY).dt.strftime('%Y-%m-%d')


def _ar1(rng, n, phi, sigma):
    e = rng.normal(0.0, sigma, n)
    out = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = phi * acc + e[i]
        out[i] = acc
    return out


def make_fuzz_dataset(src_dir: str, dst_dir: str, seed: int, tickers: List[str]) -> None:
    os.makedirs(os.path.join(dst_dir, '1m'), exist_ok=True)
    for sub in ('1d', '5m'):
        link = os.path.join(dst_dir, sub)
        if not os.path.exists(link):
            os.symlink(os.path.abspath(os.path.join(src_dir, sub)), link)
    rng = np.random.default_rng(1_000_003 * (seed + 1))

    for tkr in tickers:
        df = pd.read_csv(os.path.join(src_dir, '1m', f'{tkr}.csv'), dtype={'timestamp': str})
        day = _et_date(df['timestamp']).values
        px = {c: df[c].values.astype(float).copy() for c in ('open', 'high', 'low', 'close')}
        vol = df['volume'].fillna(0).values.astype(float).copy()
        for d in np.unique(day):
            idx = np.where(day == d)[0]
            n = len(idx)
            t = np.arange(n) / 390.0
            f = np.exp(rng.normal(0, 0.006) + rng.normal(0, 0.012) * t + _ar1(rng, n, 0.995, 0.0004))
            for c in px:
                px[c][idx] = px[c][idx] * f
            vol[idx] = np.round(vol[idx] * np.exp(rng.normal(0, 0.45)) * np.exp(rng.normal(0, 0.35, n)))
        out = df.copy()
        for c in px:
            out[c] = px[c]
        out['volume'] = vol
        out.to_csv(os.path.join(dst_dir, '1m', f'{tkr}.csv'), index=False)

    v = pd.read_csv(os.path.join(src_dir, '1m', 'VIX.csv'), dtype={'timestamp': str})
    day = _et_date(v['timestamp']).values
    close = v['close'].values.astype(float)
    scale = np.ones(len(v))
    for d in np.unique(day):
        idx = np.where(day == d)[0]
        scale[idx] = np.exp(rng.normal(0, 0.20) + _ar1(rng, len(idx), 0.999, 0.002))
    for c in ('open', 'high', 'low', 'close'):
        v[c] = np.clip(v[c].values.astype(float) * scale, 9.0, 70.0)
    v.to_csv(os.path.join(dst_dir, '1m', 'VIX.csv'), index=False)
