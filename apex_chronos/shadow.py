"""End-of-day comparisons of the existing scheduler and independent shadow policies."""
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
    """Fit on prior sessions only, then compare five policies on the same final bars."""
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
    filtered, _ = run_session(date, day, tickers, engine_kwargs={'excluded_modules': {'P1C', 'P3'}})
    p1a_settings = {'p1a_max_rvol': 1.0, 'p1a_max_retrace': 1.0}
    p1a, _ = run_session(date, day, tickers, engine_kwargs=p1a_settings)
    combined, _ = run_session(date, day, tickers, engine_kwargs={
        **p1a_settings, 'excluded_modules': {'P1C', 'P3'},
    })

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
            'mean_model_roi': mean(t['roi'] for t in closed.values()) if closed else None,
        }

    current, alternative = outcomes(baseline), outcomes(shadow)
    filtered_outcomes = outcomes(filtered)
    p1a_outcomes = outcomes(p1a)
    combined_outcomes = outcomes(combined)
    current_summary, shadow_summary = summary(current), summary(alternative)
    filtered_summary = summary(filtered_outcomes)
    p1a_summary = summary(p1a_outcomes)
    combined_summary = summary(combined_outcomes)
    current_entries = current[0].keys() | current[1].keys()
    shadow_entries = alternative[0].keys() | alternative[1].keys()
    filtered_entries = filtered_outcomes[0].keys() | filtered_outcomes[1].keys()
    p1a_entries = p1a_outcomes[0].keys() | p1a_outcomes[1].keys()
    combined_entries = combined_outcomes[0].keys() | combined_outcomes[1].keys()
    # Relaxing the retrace limit can introduce candidates absent from the baseline.
    candidate_rows = {key(row): row for row in baseline.candidate_log}
    for row in p1a.candidate_log:
        candidate_rows.setdefault(key(row), row)
    candidates = [
        {
            **row,
            'expected_model_roi': expected[row['ticker'], row['module']],
            'training_trades_for_ticker_setup': len(ticker_history[row['ticker'], row['module']]),
            'current': decision(current, key(row)),
            'shadow': decision(alternative, key(row)),
            'skip_p1c_p3': decision(filtered_outcomes, key(row)),
            'p1a_rvol1_retrace1': decision(p1a_outcomes, key(row)),
            'combined_p1a_skip_p1c_p3': decision(combined_outcomes, key(row)),
        }
        for row in candidate_rows.values()
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
        'skip_p1c_p3': {
            **filtered_summary,
            'excluded_modules': ['P1C', 'P3'],
            'current_only': len(current_entries - filtered_entries),
            'shadow_only': len(filtered_entries - current_entries),
            'delta_sum_model_roi': (
                None if current_summary['unclosed'] or filtered_summary['unclosed']
                else filtered_summary['sum_model_roi'] - current_summary['sum_model_roi']
            ),
        },
        'p1a_rvol1_retrace1': {
            **p1a_summary,
            'entry_filters': p1a_settings,
            'current_only': len(current_entries - p1a_entries),
            'shadow_only': len(p1a_entries - current_entries),
            'delta_sum_model_roi': (
                None if current_summary['unclosed'] or p1a_summary['unclosed']
                else p1a_summary['sum_model_roi'] - current_summary['sum_model_roi']
            ),
        },
        'combined_p1a_skip_p1c_p3': {
            **combined_summary,
            'entry_filters': p1a_settings,
            'excluded_modules': ['P1C', 'P3'],
            'current_only': len(current_entries - combined_entries),
            'shadow_only': len(combined_entries - current_entries),
            'delta_sum_model_roi': (
                None if current_summary['unclosed'] or combined_summary['unclosed']
                else combined_summary['sum_model_roi'] - current_summary['sum_model_roi']
            ),
        },
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
