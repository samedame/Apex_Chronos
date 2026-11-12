"""Read-only historical-data inventory. Never a trading or fill simulation."""
import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from apex_chronos.option_quotes import archive_batch, cipher, digest


def rows_at(payload, group, child):
    parent = payload.get(group) if isinstance(payload, dict) else None
    rows = parent.get(child) if isinstance(parent, dict) else None
    return [rows] if isinstance(rows, dict) else rows if isinstance(rows, list) else []


def main():
    key = os.environ['CHRONOS_QUOTE_ARCHIVE_KEY']
    crypt = cipher(key)
    plan = json.loads(gzip.decompress(crypt.decrypt(Path('verification/historical-probe-plan.enc').read_bytes())))
    token = os.environ['TRADIER_API_KEY']
    summaries, records = [], []
    with requests.Session() as http:
        for request in plan['requests']:
            observed = datetime.now(timezone.utc)
            summary = {k: request[k] for k in ('category', 'ticker', 'endpoint')}
            summary['requested_start'] = request['params']['start']
            summary['requested_end'] = request['params']['end']
            try:
                response = http.get('https://api.tradier.com/v1/markets/' + request['endpoint'],
                                    params=request['params'], headers={'Authorization': f'Bearer {token}',
                                    'Accept': 'application/json'}, timeout=20, allow_redirects=False)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {'invalid_json': True}
                group, child = ('history', 'day') if request['endpoint'] == 'history' else ('series', 'data')
                rows = rows_at(payload, group, child)
                fields = sorted({k for row in rows if isinstance(row, dict) for k in row})
                required = {'bid', 'ask', 'bidsize', 'asksize'}
                summary.update(http_status=response.status_code, returned_rows=len(rows), fields=fields,
                               bid_ask_size_rows=sum(required.issubset(row) for row in rows if isinstance(row, dict)),
                               has_error_payload=isinstance(payload, dict) and bool(payload.get('errors')))
                records.append({'sequence': len(records), 'observed_at': observed.isoformat(),
                                'request': request, 'http_status': response.status_code, 'payload': payload,
                                'counts_for_trial': False, 'capture_mode': 'historical_availability_probe'})
            except requests.RequestException as error:
                summary.update(error=type(error).__name__, returned_rows=0, bid_ask_size_rows=0)
            summaries.append(summary)
    observed = datetime.now(timezone.utc)
    context = digest({'purpose': 'historical_availability_probe', 'plan_sha256': digest(plan)})
    archive = archive_batch('historical-probe-private', observed.date().isoformat(), context, records, key)
    result = {'status': 'availability_probe_completed', 'observed_at': observed.isoformat(),
              'provider': 'tradier_production', 'checks': summaries, 'archive_context_digest': context,
              'encrypted_archive': str(archive), 'counts_for_trial': False, 'orders_sent': 0,
              'alerts_sent': 0, 'historical_bid_ask_backtest_completed': False,
              'limitation': 'Bars or last trades cannot establish historical executable bids, asks or displayed size.'}
    Path('historical-availability-summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
