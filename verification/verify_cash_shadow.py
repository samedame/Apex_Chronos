"""Offline catalog-aware cash-shadow replay; no alerts, orders or network calls."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from apex_chronos import cash_shadow, config as C, replay
from verification.fixtures import extract


def verify(days, tickers, *, catalog_dir=None):
    reports = []
    with TemporaryDirectory() as temp:
        directory = Path(temp) / 'shadow'
        directory.mkdir()
        for session, day in sorted(days.items()):
            report = cash_shadow.evaluate(session, day, tickers, ledger_dir=temp, catalog_dir=catalog_dir)
            for account in report['accounts'].values():
                if account['settled_cash_end'] < -1e-7 or account['open_positions']:
                    raise ValueError('cash replay failed funding/flatness checks')
                if not math.isclose(account['cumulative_net_pnl'], account['cumulative_gross_pnl']
                                    - account['cumulative_fees'] - account['cumulative_slippage'], abs_tol=1e-6):
                    raise ValueError('cash replay failed cumulative PnL reconciliation')
                for fill in account['fills']:
                    if fill['quantity'] < 1 or fill['quantity'] != int(fill['quantity']):
                        raise ValueError('cash replay contains fractional contracts')
                    if fill['kind'] == 'BUY' and (fill['quantity'] < 2 or fill['quantity'] % 2):
                        raise ValueError('cash replay violated the two-contract/even-lot entry rule')
            (directory / f'{session}.json').write_text(json.dumps({'date': session, 'cash_only': report}, allow_nan=False))
            reports.append(report)
    return {'sessions': len(reports), 'first': reports[0]['date'], 'last': reports[-1]['date'],
            'qualified_candidates': sum(r['coverage']['qualified_p1a_candidates'] for r in reports),
            'catalog_verified_candidates': sum(r['coverage'].get('catalog_verified_candidates', 0) for r in reports),
            'accounts': {key: {k: value[k] for k in ('ending_equity', 'cumulative_net_pnl',
                         'cumulative_fees', 'cumulative_slippage', 'max_drawdown', 'cumulative_funded_trades')}
                         for key, value in reports[-1]['accounts'].items()},
            'limitation': 'Zero trades without causal catalogs establish skip behavior, not historical profitability.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default='verification/fixtures.tar.gz')
    parser.add_argument('--catalog-dir')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    with TemporaryDirectory() as temp:
        source = extract(args.data_dir, temp) if args.data_dir.endswith('.tar.gz') else args.data_dir
        _, days = replay.build_days(source, list(C.TICKERS), grid=True)
        result = verify(days, list(C.TICKERS), catalog_dir=args.catalog_dir)
    text = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
