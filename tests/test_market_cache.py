"""Public feed freshness must not rewrite unchanged windows into MySQL binlogs."""
import copy

import pytest

from backend import db
from backend.worker import persist_market_cache


def read_cache():
    with db.connect() as con:
        return con.execute("SELECT * FROM market_cache WHERE market='futures' AND symbol='BTCUSDC' AND interval='1m'").fetchone()


def test_identical_successful_poll_only_updates_freshness(mysql_database, monkeypatch):
    candles = [{"timestamp": 60000, "open": 100, "high": 102, "low": 99, "close": 101}]
    filters = [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}]
    assert persist_market_cache('futures', 'BTCUSDC', candles, filters, 100)
    writes = []
    original = db.Connection.execute

    def capture(self, query, params=None):
        if query.startswith(('UPDATE market_cache', 'INSERT INTO market_cache')):
            writes.append((query, params))
        return original(self, query, params)

    monkeypatch.setattr(db.Connection, 'execute', capture)
    assert not persist_market_cache('futures', 'BTCUSDC', candles, filters, 115)
    row = read_cache()
    assert row['fetched_at'] == 115
    assert db.parse(row['candles']) == candles and db.parse(row['filters']) == filters
    assert len(writes) == 1
    assert 'SET fetched_at=?' in writes[0][0]
    assert writes[0][1] == (115, 'futures', 'BTCUSDC')


@pytest.mark.parametrize('changed', ['bar', 'filter', 'next_minute'])
def test_corrections_and_new_minutes_are_persisted(mysql_database, changed):
    candles = [{"timestamp": 60000, "close": 101}]
    filters = [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}]
    persist_market_cache('futures', 'BTCUSDC', candles, filters, 100)
    candles, filters = copy.deepcopy(candles), copy.deepcopy(filters)
    if changed == 'bar':
        candles[0]['close'] = 102
    elif changed == 'filter':
        filters[0]['tickSize'] = '0.01'
    else:
        candles.append({"timestamp": 120000, "close": 103})
    assert persist_market_cache('futures', 'BTCUSDC', candles, filters, 130)
    row = read_cache()
    assert db.parse(row['candles']) == candles
    assert db.parse(row['filters']) == filters
    assert row['fetched_at'] == 130


def test_failed_write_does_not_advance_freshness(mysql_database, monkeypatch):
    persist_market_cache('futures', 'BTCUSDC', [], {}, 100)
    original = db.Connection.execute

    def fail(self, query, params=None):
        if query.startswith('UPDATE market_cache'):
            raise OSError('simulated write failure')
        return original(self, query, params)

    monkeypatch.setattr(db.Connection, 'execute', fail)
    with pytest.raises(OSError):
        persist_market_cache('futures', 'BTCUSDC', [], {}, 115)
    assert read_cache()['fetched_at'] == 100
