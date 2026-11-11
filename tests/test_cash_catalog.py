import gzip
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from apex_chronos import cash_catalog as cc

NOW = datetime(2026, 11, 11, 13, tzinfo=timezone.utc)
HEADER = 'Root Symbol|Options Closing Type|Options Type|Expiration Date|Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending'


def snapshot(now=NOW):
    rows = [HEADER,
            'SPY|N|C|11/11/2026|600.000|SPY|SPDR|N',
            'SPY|N|P|11/13/2026|599.000|SPY|SPDR|N',
            'SPY1|N|C|11/13/2026|600.000|SPY|ADJUSTED|N',
            'SPY|N|C|11/13/2026|601.000|SPY|PENDING|Y',
            'SPY|N|C|11/20/2026|602.000|SPY|LATER|N',
            'File Creation Time: 111120260700|||||||']
    return cc.parse_nasdaq(rows, now, ['SPY'])


def test_public_directory_keeps_only_actual_active_standard_root_series():
    s = snapshot()
    assert [c['symbol'] for c in s['contracts']] == ['SPY261111C00600000', 'SPY261113P00599000']
    assert s['observed_at'] == NOW.isoformat()
    assert s['publisher_creation_raw'] == '111120260700'
    with pytest.raises(ValueError, match='not dated'):
        snapshot(NOW + timedelta(days=1))
    with pytest.raises(ValueError, match='columns'):
        cc.parse_nasdaq(['wrong header'], NOW, ['SPY'])
    with pytest.raises(ValueError, match='trailer'):
        cc.parse_nasdaq([HEADER, 'SPY|N|C|11/11/2026|600|SPY|SPDR|N'], NOW, ['SPY'])


def test_catalog_cannot_be_used_before_observation_or_after_24_hours(tmp_path):
    s = snapshot()
    path = cc.save_snapshot(tmp_path, s)
    loaded = cc.load_snapshots(tmp_path, date(2026, 11, 11))
    assert len(loaded) == 1 and loaded[0]['_digest']
    assert cc.contracts_at(loaded, 'SPY', NOW - timedelta(seconds=1))[2] == 'missing_causal_catalog'
    assert cc.contracts_at(loaded, 'SPY', NOW + timedelta(hours=24, seconds=1))[2] == 'missing_causal_catalog'
    assert len(cc.contracts_at(loaded, 'SPY', NOW)[0]) == 2
    assert cc.contracts_at(loaded, 'QQQ', NOW)[2] == 'ticker_absent_from_catalog'
    # Changing even valid data without changing the digest filename fails closed.
    s['source'] = 'changed'
    path.write_bytes(gzip.compress(json.dumps(s).encode()))
    with pytest.raises(ValueError, match='digest filename'):
        cc.load_snapshots(tmp_path, date(2026, 11, 11))


def test_catalog_validation_rejects_inconsistent_and_adjusted_contracts():
    s = snapshot()
    s['contracts'].append(dict(s['contracts'][0]))
    with pytest.raises(ValueError, match='duplicate'):
        cc.validate_snapshot(s)
    s = snapshot()
    s['contracts'][0]['multiplier'] = 10
    with pytest.raises(ValueError, match='100-share'):
        cc.validate_snapshot(s)
    with pytest.raises(ValueError, match='timezone'):
        cc.timestamp('2026-11-11T08:00:00')
    with pytest.raises(ValueError, match='strike'):
        cc.option_symbol('SPY', date(2026, 11, 11), 'C', float('nan'))


def test_collector_is_read_only_and_records_observation(tmp_path):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def raise_for_status(self):
            pass
        def iter_lines(self, **_):
            return iter([HEADER, 'SPY|N|C|11/11/2026|600|SPY|SPDR|N',
                         'File Creation Time: 111120260700|||||||'])
    class HTTP:
        def get(self, url, **kwargs):
            assert url == cc.SOURCE_URL and kwargs['stream']
            return Response()
    assert cc.collect(tmp_path, ['SPY'], http=HTTP(), now=NOW).is_file()
    assert cc.collect(tmp_path, ['SPY'], http=HTTP(), now=NOW + timedelta(days=3)) is None
