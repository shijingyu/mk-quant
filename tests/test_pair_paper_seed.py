"""Warmup aggregation and transactional guards with no database/network I/O."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import pytest

from backend.strategy import validate_strategy_source
from backend.timeframes import MinuteFrames
from scripts.seed_pair_paper import (
    PAIR, assert_pristine, build_warmup_frames, fetch_recent_minutes,
    seed_account, warmup_requirements,
)


MINUTE = 60_000
START = 1735689600000
END = START + 617 * MINUTE  # A partial current hour is preserved, not fabricated.
SOURCE = (Path(__file__).resolve().parents[1] / "backend/strategies/btc_eth_usdc_ratio.py").read_text()


def manifest():
    return validate_strategy_source(SOURCE)


def bars(symbol, start, end):
    price = 90000. if symbol == "BTCUSDC" else 3000.
    return [dict(timestamp=stamp, open=price, high=price+10, low=price-10,
                 close=price+1, volume=1., close_time=stamp+MINUTE-1)
            for stamp in range(start, end, MINUTE)]


def candles(period="1h", lookback=3, end=END):
    m = manifest()
    m["timeframes"]["primary"] = period
    required = warmup_requirements(m, {"lookback": lookback})
    return m, {symbol: bars(symbol, end-required*MINUTE, end) for symbol in PAIR}


@pytest.mark.parametrize("period,minutes", [("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)])
def test_frames_contain_complete_lookback_and_partial_bucket_without_trading(period, minutes):
    end = START + (4*minutes + 7)*MINUTE
    m, data = candles(period, end=end)
    original = copy.deepcopy(data)
    state = build_warmup_frames(m, {"lookback": 3}, data, end)
    loaded = MinuteFrames.load(json.loads(json.dumps(state)))
    assert loaded.last_timestamp == end-MINUTE
    assert all(len(rows) == 3 for rows in loaded.history[period].values())
    assert loaded.pending[period]["count"] == 7
    assert data == original
    assert not any(key in state for key in ("orders", "positions", "strategy_state"))
    # Continue the exact normal accumulator: the first future completed bucket
    # is a new decision opportunity; seeded historical bars are never replayed.
    for stamp in range(end, end+(minutes-7)*MINUTE, MINUTE):
        ready = loaded.push({symbol: bars(symbol, stamp, stamp+MINUTE)[0] for symbol in PAIR})
    assert ready
    assert all(len(rows) == 4 for rows in loaded.history[period].values())


@pytest.mark.parametrize("problem", ["missing", "duplicate", "misaligned", "negative_volume", "nan", "bad_high", "wrong_close_time"])
def test_invalid_minute_data_is_not_repaired(problem):
    m, data = candles()
    rows = data["ETHUSDC"]
    if problem == "missing":
        rows.pop(8)
    elif problem == "duplicate":
        rows[8] = dict(rows[7])
    elif problem == "misaligned":
        rows[8]["timestamp"] += 1
    elif problem == "negative_volume":
        rows[8]["volume"] = -1
    elif problem == "nan":
        rows[8]["close"] = float("nan")
    elif problem == "bad_high":
        rows[8]["high"] = rows[8]["close"]-1
    else:
        rows[8]["close_time"] += MINUTE
    with pytest.raises(ValueError):
        build_warmup_frames(m, {"lookback": 3}, data, END)


def fresh_records():
    m = manifest()
    m["id"] = "uploaded-uuid"  # app.py replaces the source literal's ID.
    return dict(
        account=dict(id="account", mode="paper", strategy_id=m["id"], config='{"lookback":3}',
                     symbols=json.dumps(sorted(PAIR)), initial_equity=10000.,
                     metrics=json.dumps(dict(equity=10000., positions=[], orders=[], curve=[])),
                     source="local", created_at="2025-01-01T00:00:00Z"),
        instance=dict(account_id="account", desired_state="stopped", observed_state="stopped", owner=None,
                      fence=0, lease_until=0., last_heartbeat=None, error=None, config_revision=1),
        strategy=dict(id=m["id"], manifest=json.dumps(m), source=SOURCE,
                      source_hash=hashlib.sha256(SOURCE.encode()).hexdigest(), trusted=1),
        orders=[], fills=[], ledger=[], commands=[], audits=[])


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return copy.deepcopy(self.row)


class MemoryDatabase:
    def __init__(self):
        self.records = fresh_records()
        self.queries = []
        self.locked = False

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params):
        self.queries.append(sql)
        assert "credentials" not in sql.lower()
        if "FROM instances" in sql:
            return Cursor(self.records["instance"])
        if "FROM accounts" in sql:
            return Cursor(self.records["account"])
        if "FROM strategies" in sql:
            return Cursor(self.records["strategy"])
        for table in ("orders", "fills", "ledger", "commands"):
            if f"FROM {table} " in sql:
                return Cursor({"exists": 1} if self.records[table] else None)
        if sql.startswith("INSERT INTO ledger"):
            assert self.locked and not self.records["ledger"]
            self.records["ledger"].append(json.loads(params[4]))
            return Cursor()
        raise AssertionError(sql)

    def lock_instance(self, con, account_id):
        self.locked = True
        return copy.deepcopy(self.records["instance"])

    def uid(self):
        return "snapshot-id"

    def now(self):
        return "2025-01-01T10:17:00Z"

    dump = staticmethod(json.dumps)

    def audit(self, con, actor, action, kind, account, payload):
        assert self.locked
        self.records["audits"].append((action, payload))


class PublicClient:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    def sync_time(self):
        return 0

    def klines(self, symbol, interval, start, end, **kwargs):
        assert interval == "1m"
        self.calls.append((symbol, start, end))
        if self.hook:
            self.hook()
            self.hook = None
        return bars(symbol, start, end)


def test_seed_uploaded_uuid_snapshot_matches_worker_and_is_once_only():
    from backend.worker import _restore_ledger
    database = MemoryDatabase()
    result = seed_account("account", database=database, client=PublicClient(), clock=lambda: END/1000)
    assert result["applied"] and result["minutes_per_symbol"] == 240
    snapshot = database.records["ledger"][0]
    assert snapshot["revision"]["strategy_id"] == "uploaded-uuid"
    assert snapshot["last_timestamp"] == END-MINUTE and snapshot["bar_index"] == 0
    assert snapshot["strategy_state"] == {} and snapshot["pending"] == [] and snapshot["curve"] == []
    ledger = _restore_ledger(database.records["account"], manifest(), snapshot)
    assert ledger.balance == 10000 and ledger.fees == 0 and ledger.orders == [] and ledger.positions == {}
    assert database.records["instance"]["desired_state"] == "stopped"
    assert len(database.records["audits"]) == 1
    with pytest.raises(ValueError, match="账本"):
        seed_account("account", database=database, client=PublicClient(), clock=lambda: END/1000)
    assert len(database.records["ledger"]) == 1


def test_dry_run_does_not_mutate_account_or_ledger():
    database = MemoryDatabase()
    before = copy.deepcopy(database.records)
    result = seed_account("account", dry_run=True, database=database, client=PublicClient(), clock=lambda: END/1000)
    assert not result["applied"] and database.records == before


@pytest.mark.parametrize("table", ["orders", "fills", "ledger", "commands"])
def test_existing_history_and_pending_commands_rejected_before_download(table):
    database, client = MemoryDatabase(), PublicClient()
    database.records[table].append({"any": "history"})
    with pytest.raises(ValueError):
        seed_account("account", database=database, client=client, clock=lambda: END/1000)
    assert client.calls == []


@pytest.mark.parametrize("change", ["live", "demo", "running", "lease", "positions", "equity"])
def test_non_pristine_account_rejected(change):
    records = fresh_records()
    account, instance = records["account"], records["instance"]
    if change == "live":
        account["mode"] = "live"
    elif change == "demo":
        account["source"] = "demo"
    elif change == "running":
        instance["desired_state"] = "running"
    elif change == "lease":
        instance["lease_until"] = END/1000+1
    else:
        metrics = json.loads(account["metrics"])
        metrics[change] = [{"quantity": 1}] if change == "positions" else 10001
        account["metrics"] = json.dumps(metrics)
    with pytest.raises(ValueError):
        assert_pristine(account, instance, END/1000)


def test_download_race_revision_change_refuses_commit():
    database = MemoryDatabase()
    client = PublicClient(hook=lambda: database.records["instance"].update(config_revision=2))
    with pytest.raises(ValueError, match="已改变"):
        seed_account("account", database=database, client=client, clock=lambda: END/1000)
    assert database.locked and not database.records["ledger"] and not database.records["audits"]


def test_download_catches_up_both_legs_and_keeps_exact_window():
    times = iter([END/1000, (END+MINUTE)/1000, (END+MINUTE)/1000])
    client = PublicClient()
    data, end, offset = fetch_recent_minutes(client, 240, clock=lambda: next(times))
    assert end == END+MINUTE and offset == 0 and len(client.calls) == 4
    state = build_warmup_frames(manifest(), {"lookback": 3}, data, end)
    assert state["last_timestamp"] == END
    assert all(len(rows) == 240 for rows in data.values())
