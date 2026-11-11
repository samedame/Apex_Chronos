"""Timestamped public option-series catalogs. Contains no trading or alert calls."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests

from . import config as C
from .calendar_nyse import NY, is_full_session

SOURCE_URL = 'https://www.nasdaqtrader.com/dynamic/symdir/options.txt'
SCHEMA_VERSION = 1
MAX_CATALOG_AGE = timedelta(hours=24)
MAX_EXPIRY_DAYS = 7


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('catalog timestamps must include a timezone')
    return result.astimezone(timezone.utc)


def option_symbol(ticker, expiry, right, strike):
    if not re.fullmatch('[A-Z]{1,6}', ticker) or right not in ('C', 'P'):
        raise ValueError('unsupported option root or right')
    if not math.isfinite(strike) or not 0 < strike < 100000:
        raise ValueError('invalid option strike')
    units = round(strike * 1000)
    if not math.isclose(units, strike * 1000, abs_tol=1e-6):
        raise ValueError('option strike must have at most three decimal places')
    return f'{ticker}{expiry:%y%m%d}{right}{units:08d}'


def validate_snapshot(snapshot):
    if snapshot['schema_version'] != SCHEMA_VERSION:
        raise ValueError('unsupported catalog schema')
    observed = timestamp(snapshot['observed_at'])
    if not snapshot.get('source'):
        raise ValueError('catalog source is required')
    seen = set()
    for contract in snapshot['contracts']:
        expiry = date.fromisoformat(contract['expiry'])
        expected = option_symbol(contract['ticker'], expiry, contract['right'], contract['strike'])
        if contract['symbol'] != expected or contract['symbol'] in seen:
            raise ValueError('inconsistent or duplicate catalog contract')
        if contract.get('multiplier') != 100 or contract.get('standard') is not True:
            raise ValueError('cash shadow supports standard 100-share contracts only')
        seen.add(contract['symbol'])
    return observed


def parse_nasdaq(lines, observed_at, tickers):
    """Normalize active standard-root series; never invent a strike or expiry."""
    if observed_at.utcoffset() is None:
        raise ValueError('observed_at must include a timezone')
    session = observed_at.astimezone(NY).date()
    reader = csv.DictReader(lines, delimiter='|')
    required = {'Root Symbol', 'Options Type', 'Expiration Date', 'Explicit Strike Price',
                'Underlying Symbol', 'Pending'}
    if not required.issubset(reader.fieldnames or ()):
        raise ValueError('Nasdaq series directory has unexpected columns')
    contracts, creation, skipped = {}, None, 0
    for row in reader:
        root = row.get('Root Symbol', '').strip()
        if root.startswith('File Creation Time:'):
            creation = root.partition(':')[2].strip()
            continue
        ticker = row.get('Underlying Symbol', '').strip()
        if ticker not in tickers or root != ticker or row.get('Pending') != 'N':
            skipped += 1
            continue
        expiry = datetime.strptime(row['Expiration Date'], '%m/%d/%Y').date()
        if not session <= expiry <= session + timedelta(days=MAX_EXPIRY_DAYS):
            continue
        right = row['Options Type'].strip()
        strike = float(row['Explicit Strike Price'])
        symbol = option_symbol(ticker, expiry, right, strike)
        contracts[symbol] = {'symbol': symbol, 'ticker': ticker, 'expiry': expiry.isoformat(),
                             'right': right, 'strike': strike, 'multiplier': 100, 'standard': True}
    if creation is None:
        raise ValueError('Nasdaq directory is missing its file-creation trailer')
    # Publisher timestamp is retained as provenance, never backdated into an
    # observation time. Root equality excludes common adjusted-root series.
    published_date = datetime.strptime(creation[:8], '%m%d%Y').date()
    if published_date != session:
        raise ValueError('Nasdaq directory is not dated for the observed session')
    if not contracts:
        raise ValueError('Nasdaq directory contains no eligible requested series')
    snapshot = {
        'schema_version': SCHEMA_VERSION, 'observed_at': observed_at.astimezone(timezone.utc).isoformat(),
        'source': SOURCE_URL, 'publisher_creation_raw': creation,
        'contracts': sorted(contracts.values(), key=lambda c: c['symbol']),
        'requested_tickers': list(tickers), 'skipped_rows': skipped,
        'limitations': ['Directory membership is not an executable quote or Robinhood approval.',
                        'Standard-root equity/ETF series are modeled with the standard 100-share multiplier; adjusted roots and pending series are excluded.'],
    }
    validate_snapshot(snapshot)
    return snapshot


def save_snapshot(directory, snapshot):
    observed = validate_snapshot(snapshot)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(directory) / f'{observed:%Y-%m-%d}' / f'{observed:%H%M%S%f}-{digest[:12]}.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(gzip.compress(encoded, mtime=0))
            temp.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def load_snapshots(directory, session):
    if directory is None:
        return []
    snapshots = []
    # A 24-hour catalog can cross the UTC date boundary. No later observation
    # is admitted by selection, even when a same-day file was collected at EOD.
    for offset in (-1, 0, 1):
        folder = Path(directory) / str(session + timedelta(days=offset))
        for path in sorted(folder.glob('*.json.gz')):
            encoded = gzip.decompress(path.read_bytes())
            snapshot = json.loads(encoded)
            observed = validate_snapshot(snapshot)
            digest = hashlib.sha256(encoded).hexdigest()
            expected = f'{observed:%H%M%S%f}-{digest[:12]}.json.gz'
            if path.name != expected or folder.name != f'{observed:%Y-%m-%d}':
                raise ValueError('catalog content does not match its observation/digest filename')
            snapshot['_digest'] = digest
            snapshots.append(snapshot)
    return sorted(snapshots, key=lambda s: timestamp(s['observed_at']))


def contracts_at(snapshots, ticker, now):
    eligible = [s for s in snapshots if timedelta(0) <= now - timestamp(s['observed_at']) <= MAX_CATALOG_AGE]
    if not eligible:
        return [], None, 'missing_causal_catalog'
    latest = max(eligible, key=lambda s: timestamp(s['observed_at']))
    contracts = [c for c in latest['contracts'] if c['ticker'] == ticker]
    return contracts, latest, None if contracts else 'ticker_absent_from_catalog'


def collect(directory, tickers=None, http=None, now=None):
    tickers = list(tickers or C.TICKERS)
    http = http or requests.Session()
    requested = now or datetime.now(timezone.utc)
    if not is_full_session(requested.astimezone(NY).date()):
        return None
    with http.get(SOURCE_URL, stream=True, timeout=45) as response:
        response.raise_for_status()
        response.encoding = 'utf-8'
        lines = response.iter_lines(decode_unicode=True)
        # Capture after download/parse so a catalog cannot be used before the
        # program actually obtained it. Explicit now is reserved for fixtures.
        snapshot = parse_nasdaq(lines, requested, tickers)
    if now is None:
        snapshot['observed_at'] = datetime.now(timezone.utc).isoformat()
    return save_snapshot(directory, snapshot)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Archive a public option-series catalog; never sends alerts.')
    parser.add_argument('--state-dir', default='state')
    args = parser.parse_args(argv)
    path = collect(Path(args.state_dir) / 'cash_options')
    print(f'Cash catalog: {path}' if path else 'No full trading session; catalog skipped.')
    if path:
        from .state import git_commit_push
        published = git_commit_push([str(Path(args.state_dir) / 'cash_options')], 'apex-chronos cash-only option catalog [skip ci]')
        if os.environ.get('APEX_GIT_PUSH') == '1' and not published:
            raise RuntimeError('cash catalog was collected but could not be published')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
