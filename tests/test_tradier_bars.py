"""Opt-in Tradier 1-minute bars: off by default, completed bars only, Yahoo as the fallback. Offline."""
import os
from datetime import datetime
from unittest import mock

import pandas as pd
import pytest

from apex_chronos import data, tradier
from apex_chronos.calendar_nyse import NY

KEY = {'TRADIER_API_KEY': 'SECRET-KEY-123', 'TRADIER_BASE_URL': '', 'CHRONOS_TRADIER_BARS': '1'}
NOW = datetime(2026, 10, 15, 9, 40, 30, tzinfo=NY)          # the 09:40 bar is still forming


def rows(n_minutes):
    return [{'time': f'2026-10-15T09:{30 + m:02d}:00', 'open': 100 + m, 'high': 101 + m, 'low': 99 + m, 'close': 100.5 + m,
             'volume': 1000} for m in range(n_minutes)]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ('TRADIER_API_KEY', 'TRADIER_TOKEN', 'TRADIER_BASE_URL', 'CHRONOS_TRADIER_BARS'):
        monkeypatch.delenv(k, raising=False)


def test_off_by_default_even_with_a_key(monkeypatch):
    monkeypatch.setenv('TRADIER_API_KEY', 'k')
    assert not data.tradier_bars_enabled()
    monkeypatch.setenv('CHRONOS_TRADIER_BARS', '1')
    assert data.tradier_bars_enabled()


def test_the_switch_without_a_key_stays_off(monkeypatch):
    monkeypatch.setenv('CHRONOS_TRADIER_BARS', '1')
    assert not data.tradier_bars_enabled()


def test_forming_bar_is_dropped_and_the_frame_matches_the_yahoo_shape():
    with mock.patch.object(tradier, 'timesales', return_value=rows(11)):        # 09:30 .. 09:40
        df = data.tradier_frame('SPY', now=NOW)
    assert list(df.columns) == ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    assert len(df) == 10 and df['timestamp'].iloc[-1] == pd.Timestamp('2026-10-15 09:39:00', tz=NY)
    sb = data.to_grid(df, NOW.date(), 'SPY', 9)
    assert sb.ok and len(sb.C) == 10 and sb.holes == [] and sb.last_real == 9


def test_fetch_uses_tradier_for_equities_when_enabled(monkeypatch):
    for k, v in KEY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(data, 'datetime', mock.Mock(now=lambda tz=None: NOW))
    with mock.patch.object(tradier, 'timesales', return_value=rows(11)), \
            mock.patch.object(data, '_get', side_effect=AssertionError('Yahoo must not be called')):
        df = data.fetch_1m_today(None, 'SPY')
    assert len(df) == 10


def test_vix_always_stays_on_yahoo(monkeypatch):
    for k, v in KEY.items():
        monkeypatch.setenv(k, v)
    with mock.patch.object(tradier, 'timesales', side_effect=AssertionError('no Tradier for the index')), \
            mock.patch.object(data, '_get', return_value={'chart': {'result': [{'timestamp': []}]}}):
        assert data.fetch_1m_today(None, '^VIX').empty


@pytest.mark.parametrize('failure', [tradier.TradierError('HTTP 401'), RuntimeError('boom'), None])
def test_any_tradier_problem_falls_back_to_yahoo(monkeypatch, failure, capsys):
    for k, v in KEY.items():
        monkeypatch.setenv(k, v)
    yahoo = {'chart': {'result': [{'timestamp': [1760535000], 'indicators': {'quote': [
        {'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5], 'volume': [10]}]}}]}}
    kw = {'side_effect': failure} if failure else {'return_value': []}
    with mock.patch.object(tradier, 'timesales', **kw), mock.patch.object(data, '_get', return_value=yahoo) as g:
        df = data.fetch_1m_today(None, 'SPY')
    g.assert_called_once()
    assert len(df) == 1
    assert 'SECRET-KEY-123' not in capsys.readouterr().out


def test_workflows_pass_the_key_and_the_switch():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ('chronos-session.yml', 'chronos-eod.yml'):
        with open(os.path.join(root, '.github', 'workflows', name)) as f:
            text = f.read()
        for needle in ('TRADIER_API_KEY: ${{ secrets.TRADIER_API_KEY }}', 'TRADIER_BASE_URL: ${{ secrets.TRADIER_BASE_URL }}',
                       'CHRONOS_TRADIER_BARS: ${{ vars.CHRONOS_TRADIER_BARS }}'):
            assert needle in text, (name, needle)
