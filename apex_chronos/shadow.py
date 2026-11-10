"""End-of-day comparison of the existing scheduler and a setup-return ranking."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile

from . import config as C
from .replay import run_session

SETUP_PRIOR_TRADES = 40
TICKER_PRIOR_TRADES = 20


def compare(date, day, tickers, baseline, ledger):
    """Fit on prior sessions only, then compare two policies on the same final bars."""
    history = defaultdict(list)
    ticker_history = defaultdict(list)
    for row in ledger:
        if row['date'] < date:
            roi = float(row['roi'])
            history[row['module']].append(roi)
            ticker_history[row['ticker'], row['module']].append(roi)

    prior = [roi for returns in history.values() for roi in returns]
    pooled = mean(prior) if prior else 0.0
    expected = {}
    counts = {}
    for spec in C.MODULES.values():
        returns = history[spec.name]
        counts[spec.name] = len(returns)
        setup_roi = (sum(returns) + SETUP_PRIOR_TRADES * pooled) / (len(returns) + SETUP_PRIOR_TRADES)
        for ticker in tickers:
            group = ticker, spec.name
            ticker_returns = ticker_history[group]
            expected[group] = (
                (sum(ticker_returns) + TICKER_PRIOR_TRADES * setup_roi) /
                (len(ticker_returns) + TICKER_PRIOR_TRADES)
            )

    shadow, _ = run_session(date, day, tickers, engine_kwargs={'expected_returns': expected})

    def key(row):
        return row['ticker'], row['module'], row['m_entry']

    def outcomes(engine):
        closed = {key(row): row for row in engine.trades}
        opened = {(pos.ticker, pos.spec.name, pos.m_entry): pos for pos in engine.open}
        return closed, opened

    def decision(outcome, candidate):
        closed, opened = outcome
        if candidate in closed:
            trade = closed[candidate]
            return {k: trade[k] for k in ('slot', 'm_exit', 'roi', 'exit_reason')}
        if candidate in opened:
            return {'slot': opened[candidate].slot, 'm_exit': None, 'roi': None, 'exit_reason': None}
        return None

    def summary(outcome):
        closed, opened = outcome
        return {
            'selected': len(closed) + len(opened),
            'unclosed': len(opened),
            'sum_model_roi': sum(t['roi'] for t in closed.values()),
        }

    current, alternative = outcomes(baseline), outcomes(shadow)
    current_summary, shadow_summary = summary(current), summary(alternative)
    current_entries = current[0].keys() | current[1].keys()
    shadow_entries = alternative[0].keys() | alternative[1].keys()
    candidates = [
        {
            **row,
            'expected_model_roi': expected[row['ticker'], row['module']],
            'training_trades_for_ticker_setup': len(ticker_history[row['ticker'], row['module']]),
            'current': decision(current, key(row)),
            'shadow': decision(alternative, key(row)),
        }
        for row in baseline.candidate_log
    ]
    return {
        'date': date,
        'method': 'prior_forward_ticker_setup_mean_shrunk_v1',
        'outcome_basis': 'final_bar_model_roi_before_fees',
        'training_trades': len(prior),
        'training_trades_by_module': counts,
        'setup_prior_weight': SETUP_PRIOR_TRADES,
        'ticker_prior_weight': TICKER_PRIOR_TRADES,
        'current': current_summary,
        'shadow': shadow_summary,
        'current_only': len(current_entries - shadow_entries),
        'shadow_only': len(shadow_entries - current_entries),
        'delta_sum_model_roi': (
            None if current_summary['unclosed'] or shadow_summary['unclosed']
            else shadow_summary['sum_model_roi'] - current_summary['sum_model_roi']
        ),
        'candidates': candidates,
    }


def save_report(ledger_dir, report):
    directory = Path(ledger_dir) / 'shadow'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['date']}.json"
    with NamedTemporaryFile(mode='w', dir=directory, delete=False) as temp:
        tmp = Path(temp.name)
        try:
            json.dump(report, temp, indent=2, allow_nan=False)
            temp.write('\n')
            temp.close()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
