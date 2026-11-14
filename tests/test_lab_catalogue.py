import os

import pytest

from apex_chronos.lab import catalogue as CAT
from tests.test_lab_integration import D, M, _prior, _session, _trade, world  # noqa: F401  (fixture reuse)
from apex_chronos.lab import shadow_eval as SE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_all_110_items_once_in_order():
    assert [i.n for i in CAT.ITEMS] == list(range(1, 111))


def test_every_item_is_explained():
    for it in CAT.ITEMS:
        assert it.status in ('BUILT', 'DEFER', 'REJECT', 'INFO'), it.n
        if it.status != 'BUILT':
            assert it.note.strip(), f'item {it.n} has no reason or trigger'
            assert not it.where
        else:
            assert it.mode in ('LOG', 'CALC', 'FLAG', 'TEST', 'RULE'), it.n
            assert it.where, it.n


def test_every_built_item_points_at_real_code():
    for it in CAT.ITEMS:
        for ref in it.where:
            if ref.startswith('scripts/'):
                assert os.path.exists(os.path.join(ROOT, ref)), f'item {it.n}: {ref} missing'
            else:
                assert callable(CAT.resolve(ref)), f'item {it.n}: {ref}'


def test_every_cited_column_exists_in_a_real_shadow_row(world):
    state, today, _ = world
    key = f'{D.isoformat()}|ENTRY|AMD|Phase2_X|{M}'
    recs = [{'key': key, 'tag': 'alert', 'role': r, 'b': b, 'a': a, 'age_s': 1.0, 'iv': 0.5, 'd': 0.3}
            for r, b, a in (('short', 1.0, 1.1), ('long', 0.2, 0.25), ('xsp_short', 1.0, 1.1), ('xsp_long', 0.2, 0.25))]
    t = dict(_trade(), ticker='SPY')
    spy = {k: (v.copy() if hasattr(v, 'copy') else v) for k, v in today.items()}
    prior = _prior()
    for p in prior:
        p['tickers']['SPY'] = p['tickers']['AMD']
    everyone = {tk: {k: spy[k] for k in ('O', 'H', 'L', 'C', 'V')} for tk in ('SPY', 'QQQ', 'NVDA', 'AAPL', 'MSFT')}
    row = SE.evaluate_trade(D, t, spy, prior, state_dir=state, iv_model=0.6, ledger_recs=recs, ev_key=key.replace('AMD', 'SPY'), day_trades=[t],
                            all_tickers_today=everyone)
    missing = sorted({c for it in CAT.ITEMS for c in it.cols if c not in row})
    assert not missing, missing


def test_markdown_is_current():
    path = os.path.join(ROOT, 'docs', 'LAB_CATALOGUE.md')
    if os.path.exists(path):
        assert open(path).read() == CAT.to_markdown(), 'regenerate: python -m apex_chronos.lab.catalogue --write docs/LAB_CATALOGUE.md'
