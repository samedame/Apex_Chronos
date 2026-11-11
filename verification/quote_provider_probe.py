"""One read-only connection probe. Never counts as prospective trial evidence."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from apex_chronos import cash_catalog, config as C, data
from apex_chronos.calendar_nyse import NY
from apex_chronos.option_quotes import TradierOPRA, archive_batch, cipher, digest
from apex_chronos.quote_collector import save_json


def main():
    observed = datetime.now(timezone.utc)
    session = observed.astimezone(NY).date()
    key = os.environ.get('CHRONOS_QUOTE_ARCHIVE_KEY', '')
    cipher(key)
    snapshots = cash_catalog.load_snapshots('state/cash_options', session)
    def one(ticker):
        with requests.Session() as http:
            frame = data.fetch_1m_today(http, ticker)
        return ticker, float(frame.close.iloc[-1])
    with ThreadPoolExecutor(max_workers=4) as pool:
        spots = dict(pool.map(one, C.TICKERS))
    symbols = []
    for ticker, spot in spots.items():
        contracts, _, error = cash_catalog.contracts_at(snapshots, ticker, observed)
        if error:
            raise RuntimeError('No causally observed catalog is available for this probe')
        for right in ('C', 'P'):
            choices = [c for c in contracts if c['right'] == right and c['expiry'] >= str(session)]
            if not choices:
                continue
            expiry = min(c['expiry'] for c in choices)
            choices = [c for c in choices if c['expiry'] == expiry]
            symbols.append(min(choices, key=lambda c: abs(c['strike'] - spot))['symbol'])
    provider = TradierOPRA(os.environ.get('TRADIER_API_KEY'))
    rows = provider.raw(symbols)
    ended = datetime.now(timezone.utc)
    fingerprint = digest({'purpose': 'connection_probe_only', 'provider': 'tradier_realtime'})
    record = {'sequence': 0, 'observed_at': ended.isoformat(), 'capture_mode': 'connection_probe_only',
              'raw_quotes': rows, 'counts_for_trial': False}
    path = archive_batch('state/quote_probe_archive', str(session), fingerprint, [record], key)
    age = lambda q, side: (ended - datetime.fromtimestamp(q[f'{side}_date'] / 1000, timezone.utc)).total_seconds()
    fresh = [q for q in rows if q.get('type') == 'option' and q.get('contract_size') == 100
             and all(0 <= age(q, side) <= 15 for side in ('bid', 'ask'))]
    result = {'status': 'probe_completed', 'provider': 'tradier_realtime',
              'requested_symbols': len(symbols), 'received_quotes': len(rows),
              'fresh_both_sides_15s': len(fresh),
              'whole_sizes_present': sum(type(q.get('bidsize')) is int and type(q.get('asksize')) is int for q in rows),
              'reported_lot_sizes': sorted({str(q.get('lot_size')) for q in rows}),
              'reported_contract_sizes': sorted({str(q.get('contract_size')) for q in rows}),
              'encrypted_archive': str(path), 'archive_context_digest': fingerprint,
              'observed_at': ended.isoformat(), 'counts_for_trial': False,
              'live_alerts_changed': False, 'orders_sent': 0}
    save_json('quote-probe-summary.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
