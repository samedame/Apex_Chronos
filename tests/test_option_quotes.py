import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from apex_chronos import option_quotes as oq

NOW = datetime(2026, 11, 11, 14, 36, tzinfo=timezone.utc)
SYMBOL = 'SPY261111C00700000'


def quote(now=NOW, **kwargs):
    row = dict(symbol=SYMBOL, bid=.99, ask=1.0, bid_size=20, ask_size=20,
               market_at=now.isoformat(), observed_at=now.isoformat(),
               bid_exchange='C', ask_exchange='C', condition='A')
    return oq.Quote(**{**row, **kwargs})


def test_quote_records_prices_sizes_timestamps_and_feed_without_inventing_values():
    q = quote()
    assert q.rejection(NOW) is None
    assert asdict(q)['bid_size'] == 20 and q.feed == 'opra'
    assert replace(q, observed_at=(NOW + timedelta(seconds=1)).isoformat()).identity == q.identity
    assert replace(q, market_at=(NOW + timedelta(seconds=1)).isoformat()).identity != q.identity
    for bad in (float('nan'), float('inf'), -1):
        with pytest.raises(ValueError):
            quote(bid=bad)
    with pytest.raises(ValueError, match='whole contracts'):
        quote(ask_size=1.5)


@pytest.mark.parametrize('changes,reason', [
    ({'feed': 'indicative'}, 'unverified_or_substitute_feed'),
    ({'provider': 'unknown'}, 'unverified_or_substitute_feed'),
    ({'market_at': (NOW + timedelta(seconds=1)).isoformat()}, 'future_quote'),
    ({'market_at': (NOW - timedelta(seconds=16)).isoformat()}, 'stale_quote'),
    ({'condition': 'unknown'}, 'unsupported_quote_condition'),
    ({'bid': 1.01}, 'crossed_quote'),
    ({'bid': 0}, 'zero_sided_quote'),
    ({'bid': .5}, 'spread_too_wide'),
])
def test_unusable_quotes_never_become_validated_fills(changes, reason):
    assert quote(**changes).rejection(NOW) == reason


def test_provider_uses_only_explicit_opra_get_requests_and_never_falls_back():
    class Response:
        status_code = 200
        def json(self):
            return {'quotes': {SYMBOL: {'bp': .99, 'ap': 1., 'bs': 20, 'as': 20,
                    't': NOW.isoformat(), 'bx': 'C', 'ax': 'C', 'c': 'A'}}}
    class HTTP:
        def get(self, url, **kwargs):
            assert url == oq.ALPACA_URL
            assert kwargs['params'] == {'symbols': SYMBOL, 'feed': 'opra'}
            assert kwargs['allow_redirects'] is False
            assert kwargs['headers']['APCA-API-KEY-ID'] == 'test-only-key'
            return Response()
    provider = oq.AlpacaOPRA('test-only-key', 'test-only-secret', http=HTTP(), clock=lambda: NOW)
    assert provider.latest([SYMBOL])[0] == quote()
    Response.status_code = 403
    with pytest.raises(RuntimeError, match='HTTP 403') as error:
        provider.latest([SYMBOL])
    assert 'secret' not in str(error.value) and 'test-only-key' not in str(error.value)
    assert provider.latest([]) == []


def test_encrypted_archives_authenticate_and_do_not_publish_plaintext(tmp_path):
    key = Fernet.generate_key().decode()
    records = [{'sequence': 0, 'observed_at': NOW.isoformat(), 'quotes': [asdict(quote())]}]
    path = oq.archive_batch(tmp_path, '2026-11-11', 'trial-digest', records, key)
    assert SYMBOL.encode() not in path.read_bytes() and b'ask_size' not in path.read_bytes()
    assert oq.load_archive(tmp_path, '2026-11-11', 'trial-digest', key) == records
    # Overlapping encrypted checkpoints do not double-count the same capture.
    oq.archive_batch(tmp_path, '2026-11-11', 'trial-digest', records, key)
    assert oq.load_archive(tmp_path, '2026-11-11', 'trial-digest', key) == records
    with pytest.raises(ValueError, match='authentication'):
        oq.load_archive(tmp_path, '2026-11-11', 'trial-digest', Fernet.generate_key().decode())
    with pytest.raises(ValueError, match='another session or trial'):
        oq.load_archive(tmp_path, '2026-11-11', 'changed', key)
    path.write_bytes(path.read_bytes()[:-1] + b'x')
    with pytest.raises(ValueError, match='digest mismatch'):
        oq.load_archive(tmp_path, '2026-11-11', 'trial-digest', key)


def test_archive_without_key_or_with_naive_time_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        oq.archive_batch(tmp_path, '2026-11-11', 'x', [{'observed_at': NOW.isoformat()}], '')
    with pytest.raises(ValueError, match='timezone'):
        quote(market_at='2026-11-11T14:36:00')


def test_tradier_reads_both_side_times_and_keeps_unknown_size_units_unqualified():
    class Response:
        status_code = 200
        def json(self):
            return {'quotes': {'quote': {'symbol': SYMBOL, 'type': 'option', 'contract_size': 100,
                    'bid': .99, 'ask': 1., 'bidsize': 20, 'asksize': 30,
                    'bid_date': int((NOW-timedelta(seconds=4)).timestamp()*1000),
                    'ask_date': int(NOW.timestamp()*1000), 'bidexch': 'C', 'askexch': 'C'}}}
    class HTTP:
        def get(self, url, **kwargs):
            assert url == oq.TRADIER_URL and 'sandbox' not in url
            assert kwargs['headers']['Authorization'] == 'Bearer test-only-token'
            assert kwargs['params']['includeLotSize'] == 'true'
            assert kwargs['allow_redirects'] is False
            return Response()
    provider = oq.TradierOPRA('test-only-token', http=HTTP(), clock=lambda: NOW)
    q = provider.latest([SYMBOL])[0]
    assert q.provider == 'tradier_realtime' and q.condition == 'not_supplied'
    assert q.bid_market_at != q.ask_market_at
    assert q.rejection(NOW) == 'unverified_size_units'
    provider.sizes_confirmed = True
    q = provider.latest([SYMBOL])[0]
    assert q.rejection(NOW) is None
    assert q.rejection(NOW + timedelta(seconds=12)) == 'stale_quote'  # Older bid side governs.


def test_ask_side_update_cannot_replenish_bid_side_capacity():
    q = quote(bid_market_at=NOW.isoformat(), ask_market_at=NOW.isoformat())
    updated = replace(q, ask=1.01, ask_market_at=(NOW+timedelta(seconds=1)).isoformat(),
                      observed_at=(NOW+timedelta(seconds=1)).isoformat())
    assert q.identity != updated.identity
    assert q.side_identity('sell') == updated.side_identity('sell')
    assert q.side_identity('buy') != updated.side_identity('buy')
