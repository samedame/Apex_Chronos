import numpy as np

from apex_chronos import signals
from apex_chronos.features import Features, compute_features


def synth(n=120, prev_c=100.0, o0=101.0):
    z = np.full(n, o0)
    return Features(ticker='X', O=z.copy(), H=z + 0.5, L=z - 0.5, C=z.copy(), V=np.ones(n), vix=np.full(n, 18.0), prev_c=prev_c,
                    vwap=z.copy(), vwap_std=np.full(n, 0.2), noise_upper=z + 5, rvol_cum=np.ones(n), ema9=z.copy(), ema21=z.copy(),
                    iv=np.full(n, 0.25))


def test_windows_match_the_backtest():
    assert [(d.module, d.lo, d.hi) for d in signals.DETECTORS] == [
        ('P1A', 5, 60), ('P1B', 18, 65), ('P1C', 16, 105), ('P2', 75, 240), ('P3', 210, 320)]


def test_gap_trap_fires_on_the_sweep_below_the_opening_range_and_not_before():
    f = synth()
    f.H[:5], f.L[:5] = 102.0, 100.8                    # opening range 100.8..102 after a +1% gap
    f.C[7] = 100.4                                     # closes under the range, under VWAP and EMA9, above yesterday's 100
    det = signals.E1GapTrap(f)
    fires = [det.step(m, f) for m in range(0, 20)]
    assert fires[7] == (7, 1) and all(x is None for i, x in enumerate(fires) if i != 7)


def test_gap_trap_needs_the_gap():
    f = synth(o0=100.1)                                # +0.1% gap: below the 0.25% minimum
    f.C[7] = 99.9
    det = signals.E1GapTrap(f)
    assert all(det.step(m, f) is None for m in range(0, 20))


def test_bull_trap_needs_a_pop_first():
    f = synth(o0=100.0, prev_c=100.0)
    f.H[:15] = 101.0
    f.C[20], f.O[20], f.L[19] = 100.2, 100.6, 100.5
    f.ema9[20] = 100.4
    det = signals.E3Orb15Reject(f)
    assert all(det.step(m, f) is None for m in range(0, 30))        # never popped above 101 -> no signal
    f.H[18] = 101.5                                                  # now it popped
    det = signals.E3Orb15Reject(f)
    assert [m for m in range(0, 30) if det.step(m, f)] == [20]


def test_detectors_ignore_the_future(days):
    """On real sessions, replacing every bar after minute m with NaN never changes what a detector decides at m."""
    dts, by_day = days
    checked = 0
    for d in dts[3:12]:
        for t in ('SPY', 'TSLA', 'AMD', 'COIN'):
            td = by_day[d][t]
            f = compute_features(td.ctx, td.O, td.H, td.L, td.C, td.V, td.vix)
            for cls in signals.DETECTORS:
                a, b = cls(f), cls(f)
                for m in range(cls.lo, min(cls.hi, f.n)):
                    g = compute_features(td.ctx, td.O[:m + 1], td.H[:m + 1], td.L[:m + 1], td.C[:m + 1], td.V[:m + 1], td.vix[:m + 1])
                    fa, fb = a.step(m, f), b.step(m, g)
                    assert fa == fb, (d, t, cls.__name__, m)
                    checked += 1
                    if fa:
                        break
    assert checked > 1000
