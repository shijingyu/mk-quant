"""Database behavior is exercised against the same MySQL engine as production."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading
import time
import hashlib
from types import SimpleNamespace

import pytest

from backend import db
from backend.binance import BinanceError, MySQLOrderJournal


def make_account():
    user_id, account_id = db.uid(), db.uid()
    with db.connect() as con:
        con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id, user_id, "集成测试", None, "test-hash", "client", db.now()))
        con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (account_id,user_id,"集成测试","paper",db.uid(),"momentum","{}",'["BTCUSDT"]',1000,"{}",None,None,None,"local",db.now()))
        con.execute("INSERT INTO instances(account_id,desired_state) VALUES(?,'running')", (account_id,))
    return account_id


def test_innodb_strict_mode_utc_and_rollback(mysql_database):
    with db.connect() as con:
        tables = con.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()").fetchall()
        assert tables and all(row["ENGINE"] == "InnoDB" for row in tables)
        settings = con.execute("SELECT @@session.time_zone tz, @@session.sql_mode mode, @@session.tx_isolation isolation_level, @@session.binlog_row_image row_image").fetchone()
        assert settings["tz"] == "+00:00"
        assert "STRICT_ALL_TABLES" in settings["mode"]
        assert settings["isolation_level"] == "READ-COMMITTED"
        assert settings["row_image"] == "MINIMAL"
    user_id = db.uid()
    with pytest.raises(RuntimeError, match="rollback"):
        with db.connect() as con:
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (user_id,user_id,"测试",None,"hash","client",db.now()))
            raise RuntimeError("rollback")
    with db.connect() as con:
        assert con.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone() is None


def test_minimal_row_images_are_session_scoped_with_primary_keys(mysql_database):
    with db.connect() as con:
        global_before = con.execute("SELECT @@global.binlog_row_image").fetchone()[0]
        tables = {row[0] for row in con.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND CONSTRAINT_TYPE='PRIMARY KEY'").fetchall()}
        assert set(db.TABLES) <= tables
        # A connection's local change must not leak into a subsequent actor,
        # and application initialization must not alter unrelated services.
        con.execute("SET SESSION binlog_row_image = 'FULL'")
    with db.connect() as con:
        assert con.execute("SELECT @@session.binlog_row_image").fetchone()[0] == "MINIMAL"
        assert con.execute("SELECT @@global.binlog_row_image").fetchone()[0] == global_before


def test_competing_leases_have_one_owner_and_expiry_fences_old_owner(mysql_database):
    account_id = make_account()
    barrier = threading.Barrier(6)
    def claim(index):
        barrier.wait()
        return index, db.acquire_lease(account_id, f"worker-{index}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(claim, range(6)))
    winners = [(index, fence) for index, fence in claims if fence is not None]
    assert len(winners) == 1
    index, fence = winners[0]
    owner = f"worker-{index}"
    assert db.acquire_lease(account_id, owner) == fence
    with db.connect() as con:
        con.execute("UPDATE instances SET lease_until=? WHERE account_id=?", (time.time()-1, account_id))
    next_fence = db.acquire_lease(account_id, "replacement")
    assert next_fence > fence
    assert not db.execution_authorized(account_id, owner, fence, opening=True)
    assert db.execution_authorized(account_id, "replacement", next_fence, opening=True)


def test_order_claim_race_and_restart_never_double_submit(mysql_database, monkeypatch):
    journal = MySQLOrderJournal()
    barrier = threading.Barrier(8)
    def claim(_):
        barrier.wait()
        return journal.claim("same-account", "one-intent", {"quantity":"0.01"})[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))
    assert sum(outcomes) == 1
    restarted = MySQLOrderJournal()
    assert restarted.claim("same-account", "one-intent", {"quantity":"0.01"}) == (False, {
        "account":"same-account", "client_id":"one-intent", "payload":'{"quantity":"0.01"}',
        "status":"UNKNOWN", "result":None,
        "updated_at": pytest.approx(time.time(), abs=10),
    })
    with pytest.raises(BinanceError, match="不同交易意图"):
        restarted.claim("same-account", "one-intent", {"quantity":"0.02"})
    monkeypatch.setattr("backend.binance.time", SimpleNamespace(time=lambda:1234.0))
    restarted.record("same-account", "one-intent", "CONFIRMED", {"orderId": 42})
    restarted.record("same-account", "one-intent", "CONFIRMED", {"orderId": 42})


def test_orders_fills_and_ledger_commit_together_with_decimal_precision(mysql_database):
    account_id = make_account()
    order = {"id": 1, "symbol":"BTCUSDT", "side":"buy", "status":"filled", "quantity":0.01, "price":50000.1, "fee":0.2000004, "realized_pnl":0}
    with pytest.raises(RuntimeError):
        with db.connect() as con:
            db.persist_fill(con, account_id, order)
            raise RuntimeError("abort fill transaction")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        first = db.persist_fill(con, account_id, order)
        assert db.persist_fill(con, account_id, order) == first
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?)", (db.uid(),account_id,"fill",f"{account_id}:1",db.dump(order),db.now()))
        db.audit(con, None, "paper.fill", "account", account_id, {"order_id":first})
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        fill = con.execute("SELECT * FROM fills").fetchone()
        assert fill["price"] == Decimal("50000.1")
        assert fill["fee"] == Decimal("0.2000004")
        assert con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0] == 1


def test_same_external_account_cannot_be_bound_twice(mysql_database):
    first, second = make_account(), make_account()
    with db.connect() as con:
        con.execute("UPDATE accounts SET external_uid='actual-exchange-account' WHERE id=?", (first,))
    with pytest.raises(db.IntegrityError):
        with db.connect() as con:
            con.execute("UPDATE accounts SET external_uid='actual-exchange-account' WHERE id=?", (second,))


def test_strategy_version_is_immutable(mysql_database):
    with db.connect() as con:
        row = con.execute("SELECT * FROM strategies WHERE id='momentum'").fetchone()
        manifest = db.parse(row["manifest"])
        version = db.pin_strategy_version(con, row["id"], manifest, row["source"], row["source_hash"])
        assert len(version) == 64
        with pytest.raises(ValueError, match="不可覆盖"):
            db.pin_strategy_version(con, row["id"], {**manifest,"description":"changed without new source"}, row["source"], row["source_hash"])


@pytest.mark.parametrize('guard',['idle','running','queued','custom','untrusted'])
def test_builtin_minute_upgrade_pins_old_version_and_preserves_edits(mysql_database,monkeypatch,guard):
    with db.connect() as con:
        current=dict(con.execute("SELECT * FROM strategies WHERE id='momentum'").fetchone())
    old_source=current['source'].replace('"version": "1.1.0"','"version": "1.0.0"')
    old_manifest=db.parse(current['manifest']);old_manifest['version']='1.0.0'
    old_hash=hashlib.sha256(old_source.encode()).hexdigest()
    monkeypatch.setattr(db,'_MINUTE_PREDECESSOR_HASHES',{'momentum':old_hash})
    if guard=='custom':
        old_source+='\n# administrator customization\n'
        old_hash=hashlib.sha256(old_source.encode()).hexdigest()
    with db.connect() as con:
        con.execute("UPDATE strategies SET source=?,source_hash=?,manifest=? WHERE id='momentum'",(old_source,old_hash,db.dump(old_manifest)))
        if guard=='untrusted':con.execute("UPDATE strategies SET trusted=0 WHERE id='momentum'")
    if guard=='running':make_account()
    if guard=='queued':
        with db.connect() as con:
            con.execute("INSERT INTO datasets VALUES('upgrade-data','旧数据','futures','1m','[\"BTCUSDT\"]','{}',?,'synthetic',?)",('a'*64,db.now()))
            con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at) VALUES('upgrade-job','未完成任务','momentum','upgrade-data','queued','{}','{}',?)",(db.now(),))
    db.init_db()
    with db.connect() as con:
        saved=con.execute("SELECT * FROM strategies WHERE id='momentum'").fetchone()
        old=con.execute("SELECT source FROM strategy_versions WHERE strategy_id='momentum' AND source_hash=?",(old_hash,)).fetchone()
        assert old['source']==old_source
        assert saved['source_hash']==(current['source_hash'] if guard in {'idle','untrusted'} else old_hash)
        if guard=='untrusted':assert saved['trusted']==0
        upgraded=con.execute("SELECT COUNT(*) FROM audit_records WHERE action='strategy.builtin.minute_upgrade'").fetchone()[0]
        assert upgraded==(1 if guard in {'idle','untrusted'} else 0)
