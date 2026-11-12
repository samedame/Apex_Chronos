"""Read-only OPRA quote adapter and private, authenticated quote archives.

No order endpoints, broker-account endpoints, alerts or trading SDKs are used.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests

from .cash_catalog import timestamp

ALPACA_URL = 'https://data.alpaca.markets/v1beta1/options/quotes/latest'
TRADIER_URL = 'https://api.tradier.com/v1/markets/quotes'
PROVIDER = 'alpaca_opra'
SYMBOL = re.compile(r'[A-Z]{1,6}\d{6}[CP]\d{8}')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    market_at: str
    observed_at: str
    bid_exchange: str
    ask_exchange: str
    condition: str
    provider: str = PROVIDER
    feed: str = 'opra'
    bid_market_at: str | None = None
    ask_market_at: str | None = None
    size_unit: str = 'contracts'

    def __post_init__(self):
        if not SYMBOL.fullmatch(self.symbol):
            raise ValueError('unsupported option symbol in quote')
        if not all(finite_number(x) and x >= 0 for x in (self.bid, self.ask)):
            raise ValueError('non-finite or negative option price')
        if any(type(x) is not int or x < 0 for x in (self.bid_size, self.ask_size)):
            raise ValueError('quote sizes must be nonnegative whole contracts')
        timestamp(self.market_at)
        timestamp(self.observed_at)
        if self.bid_market_at:
            timestamp(self.bid_market_at)
        if self.ask_market_at:
            timestamp(self.ask_market_at)

    @property
    def identity(self):
        # Re-polling the same market quote must not replenish its displayed size.
        row = asdict(self)
        row.pop('observed_at')
        return digest(row)

    def rejection(self, now, *, max_age_seconds=15, max_spread_fraction=.20):
        sides = [timestamp(v) for v in (self.bid_market_at or self.market_at, self.ask_market_at or self.market_at)]
        market, observed = min(sides), timestamp(self.observed_at)
        if self.provider not in (PROVIDER, 'tradier_realtime') or self.feed != 'opra':
            return 'unverified_or_substitute_feed'
        if max(sides) > observed or observed > now:
            return 'future_quote'
        if (now - market).total_seconds() > max_age_seconds or (now - observed).total_seconds() > max_age_seconds:
            return 'stale_quote'
        if self.size_unit != 'contracts':
            return 'unverified_size_units'
        allowed_condition = self.condition == 'A' if self.provider == PROVIDER else self.condition == 'not_supplied'
        if not allowed_condition or not self.bid_exchange or not self.ask_exchange:
            return 'unsupported_quote_condition'
        if self.ask < self.bid:
            return 'crossed_quote'
        if self.bid <= 0 or self.ask <= 0:
            return 'zero_sided_quote'
        if (self.ask - self.bid) / self.ask > max_spread_fraction:
            return 'spread_too_wide'
        return None

    def side_identity(self, side):
        if side == 'buy':
            values = [self.ask, self.ask_size, self.ask_exchange, self.ask_market_at or self.market_at]
        else:
            values = [self.bid, self.bid_size, self.bid_exchange, self.bid_market_at or self.market_at]
        return digest([self.provider, self.feed, self.symbol, side, *values])

    def public_metadata(self):
        return {'provider': self.provider, 'feed': self.feed,
                'market_at': self.market_at, 'observed_at': self.observed_at}


class AlpacaOPRA:
    """Explicit OPRA requests; never falls back to the free indicative feed."""
    def __init__(self, key, secret, http=None, clock=None):
        if not key or not secret:
            raise ValueError('OPRA data credentials are not configured')
        self._headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}
        self.http = http or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('APCA_API_KEY_ID'), os.environ.get('APCA_API_SECRET_KEY'))

    def latest(self, symbols):
        symbols = sorted(set(symbols))
        if not symbols:
            return []
        if any(not SYMBOL.fullmatch(s) for s in symbols) or len(symbols) > 100:
            raise ValueError('OPRA requests require at most 100 valid option symbols')
        # Do not include error bodies, request objects or headers in logs.
        try:
            response = self.http.get(ALPACA_URL, headers=self._headers,
                                     params={'symbols': ','.join(symbols), 'feed': 'opra'},
                                     timeout=10, allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('OPRA quote request failed') from None
        if response.status_code != 200:
            raise RuntimeError(f'OPRA quote request returned HTTP {response.status_code}')
        try:
            payload = response.json()
            if payload.get('feed', 'opra') != 'opra':
                raise ValueError('substitute feed')
            raw = payload['quotes']
            if not isinstance(raw, dict) or set(raw) - set(symbols):
                raise ValueError('unexpected quote symbols')
            observed = self.clock().astimezone(timezone.utc).isoformat()
            return [Quote(symbol=symbol, bid=q['bp'], ask=q['ap'], bid_size=q['bs'], ask_size=q['as'],
                          market_at=q['t'], observed_at=observed, bid_exchange=q.get('bx', ''),
                          ask_exchange=q.get('ax', ''), condition=q.get('c', ''))
                    for symbol, q in raw.items()]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('OPRA response failed quote validation') from None


class TradierOPRA:
    """Uses only the production consolidated-quote GET endpoint, never sandbox."""
    def __init__(self, token, http=None, clock=None, *, sizes_confirmed=False):
        if not token:
            raise ValueError('Tradier quote credentials are not configured')
        self._headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        self.http = http or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sizes_confirmed = sizes_confirmed

    def raw(self, symbols):
        symbols = sorted(set(symbols))
        if not symbols:
            return []
        if len(symbols) > 100 or any(not SYMBOL.fullmatch(s) for s in symbols):
            raise ValueError('Tradier requests require at most 100 valid option symbols')
        try:
            response = self.http.get(TRADIER_URL, headers=self._headers,
                                     params={'symbols': ','.join(symbols), 'includeLotSize': 'true'},
                                     timeout=10, allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('Tradier quote request failed') from None
        if response.status_code != 200:
            raise RuntimeError(f'Tradier quote request returned HTTP {response.status_code}')
        try:
            rows = response.json()['quotes']['quote']
            rows = [rows] if isinstance(rows, dict) else rows
            if not isinstance(rows, list) or any(q['symbol'] not in symbols for q in rows):
                raise ValueError
            return rows
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('Tradier response failed quote validation') from None

    def latest(self, symbols):
        rows = self.raw(symbols)
        observed = self.clock().astimezone(timezone.utc).isoformat()
        result = []
        for q in rows:
            try:
                if q['type'] != 'option' or q['contract_size'] != 100:
                    raise ValueError
                bid_at = datetime.fromtimestamp(q['bid_date'] / 1000, timezone.utc)
                ask_at = datetime.fromtimestamp(q['ask_date'] / 1000, timezone.utc)
                # Size units are explicitly confirmed in the frozen spec, or
                # by the provider reporting a one-contract lot. No x100 guess.
                unit = 'contracts' if self.sizes_confirmed or q.get('lot_size') == 1 else 'unverified'
                result.append(Quote(symbol=q['symbol'], bid=q['bid'], ask=q['ask'],
                                    bid_size=q['bidsize'], ask_size=q['asksize'],
                                    market_at=min(bid_at, ask_at).isoformat(), observed_at=observed,
                                    bid_market_at=bid_at.isoformat(), ask_market_at=ask_at.isoformat(),
                                    bid_exchange=q.get('bidexch', ''), ask_exchange=q.get('askexch', ''),
                                    condition='not_supplied', provider='tradier_realtime', feed='opra', size_unit=unit))
            except (KeyError, TypeError, ValueError, OverflowError):
                raise RuntimeError('Tradier option fields failed validation') from None
        return result


def cipher(key):
    from cryptography.fernet import Fernet
    try:
        if not isinstance(key, str) or len(base64.urlsafe_b64decode(key.encode())) != 32:
            raise ValueError
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise ValueError('quote archive key must be a Fernet key kept in a secret') from None


def archive_batch(directory, session, trial_digest, records, key):
    if not records:
        return None
    # Plaintext exists only in memory. Only encrypted archives may be committed.
    payload = {'schema_version': 1, 'session': session, 'trial_digest': trial_digest,
               'records': records}
    encoded = cipher(key).encrypt(gzip.compress(canonical(payload), mtime=0))
    path = Path(directory) / session / f'{hashlib.sha256(encoded).hexdigest()}.enc'
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(encoded)
            temp.close()
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)
    return path


def load_archive(directory, session, trial_digest, key):
    records = {}
    decrypt = cipher(key)
    for path in sorted((Path(directory) / session).glob('*.enc')):
        encoded = path.read_bytes()
        if path.stem != hashlib.sha256(encoded).hexdigest():
            raise ValueError('encrypted quote archive digest mismatch')
        try:
            payload = json.loads(gzip.decompress(decrypt.decrypt(encoded)))
        except Exception:
            raise ValueError('quote archive authentication/decryption failed') from None
        if payload.get('schema_version') != 1 or payload.get('session') != session or payload.get('trial_digest') != trial_digest:
            raise ValueError('quote archive belongs to another session or trial')
        for record in payload['records']:
            identity = digest(record)
            records[identity] = record
    return sorted(records.values(), key=lambda r: (timestamp(r['observed_at']), r['sequence']))
