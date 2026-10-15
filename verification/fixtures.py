"""Trimmed copy of the recorded data, so the verification (and CI) can run without the original data folder.

    python -m verification.fixtures build --data-dir /path/to/workspace/data --out verification/fixtures.tar.gz

Keeps every 1-minute row of the 12 strategy tickers, VIX from 09:00 ET on, the 5-minute rows needed for the
14-session baseline before the first 1-minute day, and the last 80 daily bars. Lines are copied verbatim.
`verify` proves the ORIGINAL backtest code returns the identical ledger on the trimmed copy and on the full data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import tarfile
import tempfile

from apex_chronos import config as C


def _lines(path):
    with open(path, 'r', newline='') as fh:
        return fh.read().splitlines(keepends=True)


def build(data_dir: str, out: str, daily_rows: int = 80) -> dict:
    tickers = list(C.TICKERS)
    first_day = min(l[:10] for l in _lines(os.path.join(data_dir, '1m', 'SPY.csv'))[1:])
    cutoff = (dt.date.fromisoformat(first_day) - dt.timedelta(days=35)).isoformat()   # ~24 sessions before the first 1m day
    sizes = {}
    with tarfile.open(out, 'w:gz') as tar:
        def add(name, lines):
            data = ''.join(lines).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
            sizes[name] = len(data)
        for t in tickers:
            add(f'1m/{t}.csv', _lines(os.path.join(data_dir, '1m', f'{t}.csv')))
            l5 = _lines(os.path.join(data_dir, '5m', f'{t}.csv'))
            add(f'5m/{t}.csv', l5[:1] + [l for l in l5[1:] if l[:10] >= cutoff])
            l1d = _lines(os.path.join(data_dir, '1d', f'{t}.csv'))
            add(f'1d/{t}.csv', l1d[:1] + l1d[1:][-daily_rows:])
        lv = _lines(os.path.join(data_dir, '1m', 'VIX.csv'))
        add('1m/VIX.csv', lv[:1] + [l for l in lv[1:] if l[11:16] >= '09:00'])
    return {'out': out, 'bytes': os.path.getsize(out), 'files': len(sizes), 'cutoff_5m': cutoff}


def extract(tar_path: str, dest: str | None = None) -> str:
    dest = dest or tempfile.mkdtemp(prefix='apex_fixtures_')
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter='data') if sys.version_info >= (3, 12) else tar.extractall(dest)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--data-dir', required=True)
    b.add_argument('--out', default='verification/fixtures.tar.gz')
    args = ap.parse_args(argv)
    print(build(args.data_dir, args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
