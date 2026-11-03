"""MySQL / InnoDB control store shared by development, tests and production.

All connections use UTC, strict SQL mode and READ COMMITTED. No in-memory or
file-database fallback is provided: missing MySQL configuration fails closed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import secrets
import time

import pymysql
from pymysql.err import IntegrityError
from .schema import SCHEMA, TABLES

ENGINE_VERSION = "1.3.0"
# Exact bundled predecessors only. Administrator-edited strategies must never
# be replaced merely because they use a builtin id or an older version label.
_MINUTE_PREDECESSOR_HASHES = {
    "momentum": "dbbe401ac11bde6da57c6eb3746b2ad4d79ea9e98459ab0ceeda0da54e40e844",
    "spot_dca": "0836a82552f3107a229be3c29d6fbe93e7e167b7e5c362b5681f8131e58ad732",
    "usdc_hedge_grid": "b512435a3ede06c087a671eaa07abb7533d0a596b4d6badff187c110f2f4f41a",
}
SQL_MODE = "STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ZERO_DATE,NO_ZERO_IN_DATE,NO_ENGINE_SUBSTITUTION"


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return secrets.token_hex(16)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse(value, fallback=None):
    return json.loads(value) if value else fallback


class Row(dict):
    """Mapping rows, with positional reads retained for COUNT and scalar queries."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def fetchone(self):
        row = self._cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _sql(query):
    # Translate parameter markers, not values. Quote legacy column names which
    # are MySQL keywords, without rewriting strings, comments or quoted names.
    parts = re.split(r"('(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"|`[^`]*`)", query)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\b(interval|reference)\b", lambda m: "`" + m.group(0) + "`", parts[index], flags=re.I)
        parts[index] = parts[index].replace("?", "%s")
    return "".join(parts)


class Connection:
    def __init__(self, raw):
        self.raw = raw
        self._cursors = []

    def execute(self, query, params=None):
        cursor = self.raw.cursor()
        self._cursors.append(cursor)
        cursor.execute(_sql(query), params)
        return Cursor(cursor)

    def executemany(self, query, rows):
        cursor = self.raw.cursor()
        self._cursors.append(cursor)
        cursor.executemany(_sql(query), rows)
        return Cursor(cursor)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        for cursor in self._cursors:
            try:
                cursor.close()
            except pymysql.Error:
                pass
        try:
            self.raw.close()
        except pymysql.Error:
            pass


def settings():
    required = ("MK_DB_HOST", "MK_DB_USER", "MK_DB_PASSWORD", "MK_DB_NAME")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("必须配置 MySQL 环境变量：" + ", ".join(missing))
    name = os.environ["MK_DB_NAME"]
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", name):
        raise ValueError("MK_DB_NAME 只能包含字母、数字和下划线")
    host = os.environ["MK_DB_HOST"]
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    mode = os.getenv("MK_DB_SSL_MODE", "disabled" if loopback else "verify_identity")
    if mode == "disabled":
        # Loopback and SSH forwarding already keep this hop local. Operators
        # can explicitly select this for a private container network too.
        tls = {"ssl_disabled": True}
    elif mode == "verify_identity":
        ca = os.environ.get("MK_DB_SSL_CA")
        if not ca:
            raise RuntimeError("非本机 MySQL 连接须配置 MK_DB_SSL_CA 并验证服务器身份；私有容器网络可显式设置 MK_DB_SSL_MODE=disabled")
        tls = {"ssl_disabled": False, "ssl_ca": ca, "ssl_verify_cert": True, "ssl_verify_identity": True}
    else:
        raise ValueError("MK_DB_SSL_MODE 必须是 disabled 或 verify_identity")
    return dict(host=host, port=int(os.getenv("MK_DB_PORT", "3306")),
                user=os.environ["MK_DB_USER"], password=os.environ["MK_DB_PASSWORD"], database=name,
                charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                autocommit=False, connect_timeout=10, read_timeout=30, write_timeout=30, **tls)


@contextmanager
def connect():
    raw = pymysql.connect(**settings())
    con = Connection(raw)
    try:
        con.execute("SET SESSION time_zone = '+00:00'")
        con.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        con.execute("SET SESSION sql_mode = ?", (SQL_MODE,))
        yield con
        con.commit()
    except BaseException:
        try:
            con.rollback()
        except pymysql.Error:
            # Lost-connection rollback must not hide the original failure.
            pass
        raise
    finally:
        con.close()


def init_schema():
    with connect() as con:
        for statement in SCHEMA:
            con.execute(statement)
        con.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?) ON DUPLICATE KEY UPDATE version=VALUES(version)", ("mysql-001", now()))
        con.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?) ON DUPLICATE KEY UPDATE version=VALUES(version)", ("mysql-002-download-jobs", now()))
        # MySQL 5.7 lacks ADD COLUMN IF NOT EXISTS. Serialize concurrent role
        # startup and inspect the current columns before the additive migration.
        lock_name = "mk-schema-" + hashlib.sha256(os.environ["MK_DB_NAME"].encode()).hexdigest()[:40]
        if con.execute("SELECT GET_LOCK(?,30)", (lock_name,)).fetchone()[0] != 1:
            raise RuntimeError("数据库结构更新锁等待超时")
        try:
            columns = {row["COLUMN_NAME"] for row in con.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='data_download_jobs'").fetchall()}
            for column, definition in (("waiting_reason", "TEXT NULL"), ("retry_at", "DOUBLE NULL")):
                if column not in columns:
                    con.execute(f"ALTER TABLE data_download_jobs ADD COLUMN `{column}` {definition}")
            con.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?) ON DUPLICATE KEY UPDATE version=VALUES(version)", ("mysql-003-download-waits", now()))
        finally:
            con.execute("SELECT RELEASE_LOCK(?)", (lock_name,))


def init_db():
    init_schema()
    from .strategy import builtin_strategies
    with connect() as con:
        for item in builtin_strategies():
            manifest = {k: v for k, v in item.items() if k != "source"}
            source = item["source"]
            con.execute("INSERT INTO strategies VALUES(?,?,?,?,1,?) ON DUPLICATE KEY UPDATE id=VALUES(id)",
                        (item["id"], dump(manifest), source, hashlib.sha256(source.encode()).hexdigest(), now()))
            saved = con.execute("SELECT * FROM strategies WHERE id=? FOR UPDATE", (item["id"],)).fetchone()
            pin_strategy_version(con, saved["id"], parse(saved["manifest"]), saved["source"], saved["source_hash"])
            if saved["source_hash"] == _MINUTE_PREDECESSOR_HASHES.get(item["id"]) and saved["source_hash"] != hashlib.sha256(source.encode()).hexdigest():
                states = con.execute("SELECT i.desired_state,i.observed_state FROM accounts a JOIN instances i ON i.account_id=a.id WHERE a.strategy_id=? FOR UPDATE", (item["id"],)).fetchall()
                unfinished = con.execute("SELECT id FROM experiments WHERE strategy_id=? AND status IN ('queued','running') FOR UPDATE", (item["id"],)).fetchall()
                if not unfinished and all(row["desired_state"] == row["observed_state"] == "stopped" for row in states):
                    digest = hashlib.sha256(source.encode()).hexdigest()
                    pin_strategy_version(con,item["id"],manifest,source,digest)
                    con.execute("UPDATE strategies SET manifest=?,source=?,source_hash=? WHERE id=? AND source_hash=?", (dump(manifest),source,digest,item["id"],saved["source_hash"]))
                    audit(con,None,"strategy.builtin.minute_upgrade","strategy",item["id"],{"previous_source_hash":saved["source_hash"],"source_hash":digest,"version":manifest["version"]})


def pin_strategy_version(con, strategy_id, manifest, source, source_hash):
    actual_hash = hashlib.sha256(source.encode()).hexdigest()
    if actual_hash != source_hash:
        raise ValueError("策略源码与版本哈希不一致")
    version_id = hashlib.sha256((strategy_id + ":" + source_hash).encode()).hexdigest()
    encoded = dump(manifest)
    con.execute("INSERT INTO strategy_versions(id,strategy_id,version,manifest,source,source_hash,created_at) VALUES(?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=VALUES(id)",
                (version_id, strategy_id, str(manifest.get("version", "")), encoded, source, source_hash, now()))
    saved = con.execute("SELECT manifest,source FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
    if saved["source"] != source or parse(saved["manifest"]) != manifest:
        raise ValueError("同一策略版本不可覆盖，必须保存为新版本")
    return version_id


def audit(con, actor_id, action, resource_type, resource_id, payload):
    return con.execute("INSERT INTO audit_records(actor_id,action,resource_type,resource_id,payload,created_at) VALUES(?,?,?,?,?,?)",
                       (actor_id, action, resource_type, resource_id, dump(payload or {}), now())).lastrowid


def persist_fill(con, account_id, order, created_at=None):
    """Store each paper fill and its order atomically with the account ledger.

    IDs are account-scoped and deterministic, making historical backfill and
    recovery idempotent. The original exchange/engine record remains intact.
    """
    at = created_at or now()
    original_id = str(order["id"])
    identity = hashlib.sha256((account_id + ":" + original_id).encode()).hexdigest()
    encoded = dump(order)
    con.execute("INSERT INTO orders(id,account_id,external_id,client_order_id,symbol,side,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=VALUES(id)",
                (identity, account_id, str(order.get("orderId", original_id)), str(order.get("clientOrderId", original_id)), order["symbol"], order["side"], order.get("status", "filled"), encoded, at, at))
    saved = con.execute("SELECT payload FROM orders WHERE id=?", (identity,)).fetchone()
    if parse(saved["payload"]) != order:
        raise ValueError("订单 ID 与已持久化的成交不一致")
    con.execute("INSERT INTO fills(id,account_id,order_id,external_id,symbol,quantity,price,fee,realized_pnl,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=VALUES(id)",
                (identity, account_id, identity, str(order.get("tradeId", original_id)), order["symbol"], Decimal(str(order["quantity"])), Decimal(str(order["price"])), Decimal(str(order.get("fee", 0))), Decimal(str(order.get("realized_pnl", 0))), encoded, at))
    return identity


def persist_maker_activity(con, account_id, orders, fills):
    """Persist order lifecycles and only actual simulated fills in caller's fence.

    Maker order IDs and fill IDs occupy separate namespaces from the original
    market executor. The complete client ID, lot and bracket stay in payload;
    long client IDs use a deterministic indexed representation, never truncation.
    """
    at = now()
    def order_identity(client_id, generation):
        return hashlib.sha256((account_id + ":maker-order:" + generation + ":" + str(client_id)).encode()).hexdigest()
    for order in orders:
        client_id = str(order["client_order_id"])
        generation=str(order.get("execution_id","legacy"))
        identity = order_identity(client_id,generation)
        prefix="maker:"+generation+":"
        indexed_id = prefix + (client_id if len(prefix)+len(client_id)<=128 else hashlib.sha256(client_id.encode()).hexdigest())
        saved = con.execute("SELECT payload FROM orders WHERE id=?", (identity,)).fetchone()
        if saved:
            previous = parse(saved["payload"])
            if any(previous.get(key) != order.get(key) for key in ("fingerprint", "client_order_id", "symbol", "side", "position_side", "lot_id", "execution_id")):
                raise ValueError("Maker 委托身份与已持久化的订单不一致")
        created = datetime.fromtimestamp(order["submitted_at"] / 1000, timezone.utc).isoformat()
        con.execute("INSERT INTO orders(id,account_id,external_id,client_order_id,symbol,side,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status),payload=VALUES(payload),updated_at=VALUES(updated_at)",
                    (identity, account_id, None, indexed_id, str(order.get("symbol") or ""), str(order.get("side") or ""), order["status"], dump(order), created, at))
    for fill in fills:
        original_id = str(fill["id"])
        identity = hashlib.sha256((account_id + ":maker-fill:" + original_id).encode()).hexdigest()
        encoded = dump(fill)
        created = datetime.fromtimestamp(fill["timestamp"] / 1000, timezone.utc).isoformat()
        con.execute("INSERT INTO fills(id,account_id,order_id,external_id,symbol,quantity,price,fee,realized_pnl,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=VALUES(id)",
                    (identity, account_id, order_identity(fill["client_order_id"],str(fill.get("execution_id","legacy"))), "maker:" + original_id, fill["symbol"], Decimal(str(fill["quantity"])), Decimal(str(fill["price"])), Decimal(str(fill.get("fee", 0))), Decimal(str(fill.get("realized_pnl", 0))), encoded, created))
        saved = con.execute("SELECT payload FROM fills WHERE id=?", (identity,)).fetchone()
        if parse(saved["payload"]) != fill:
            raise ValueError("Maker 成交身份与已持久化的成交不一致")
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=id",
                    (identity, account_id, "fill", f"maker-fill:{account_id}:{original_id}", encoded, created))


def log(level, message, *, strategy_id=None, account_id=None, details=None, source="system"):
    with connect() as con:
        con.execute("INSERT INTO logs(level,strategy_id,account_id,message,details,source,created_at) VALUES(?,?,?,?,?,?,?)", (level,strategy_id,account_id,message,dump(details) if details else None,source,now()))


def lock_instance(con, account_id):
    return con.execute("SELECT * FROM instances WHERE account_id=? FOR UPDATE", (account_id,)).fetchone()


def acquire_lease(account_id, owner, ttl=30):
    """InnoDB row lock serializes owners; expired leases advance the fencing token."""
    with connect() as con:
        row = lock_instance(con, account_id)
        instant = time.time()
        if not row or (row["lease_until"] > instant and row["owner"] != owner):
            return None
        fence = row["fence"] + int(row["owner"] != owner or row["lease_until"] <= instant)
        con.execute("UPDATE instances SET owner=?,fence=?,lease_until=?,last_heartbeat=? WHERE account_id=?", (owner,fence,instant+ttl,now(),account_id))
        return fence


def execution_authorized(account_id, owner, fence, *, opening=False):
    with connect() as con:
        row = con.execute("SELECT * FROM instances WHERE account_id=?", (account_id,)).fetchone()
        return bool(row and row["owner"] == owner and row["fence"] == fence and row["lease_until"] > time.time() and (not opening or row["desired_state"] == "running"))


def seed_demo():
    """Opt-in synthetic fixtures. They never represent exchange balances or executed trades."""
    from .security import hash_password
    from .storage import write_candles, store_result
    init_db()
    with connect() as con:
        if con.execute("SELECT id FROM users WHERE username='admin'").fetchone():
            return False
        password = os.getenv("MK_DEMO_PASSWORD", "")
        if len(password) < 10:
            raise ValueError("首次初始化演示数据须设置至少 10 位 MK_DEMO_PASSWORD，或运行 init-demo --generate-password")
        admin_id = uid()
        con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (admin_id,"admin","系统管理员",None,hash_password(password),"admin",now()))
        strategies = con.execute("SELECT * FROM strategies").fetchall()
        if not strategies:
            return
        base_ts = 1788220800000
        names = ["陈宇 · 趋势组合", "林夏 · 均值回归", "赵铭 · 多币种", "王珂 · 稳健增长", "许言 · 研究账户"]
        for i, name in enumerate(names):
            person, account_id = uid(), uid()
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (person,f"demo{i+1}",name.split(" · ")[0],f"1380000000{i}",hash_password(password),"client",now()))
            strategy = strategies[i % len(strategies)]
            manifest = parse(strategy["manifest"], {})
            initial = [25000,18000,50000,30000,10000][i]
            curve = [{"timestamp":base_ts+j*86400000,"equity":round(initial*(1+j*.0018+math.sin(j*.65+i)*.012-math.sin(j*.19)*.016),2)} for j in range(60)]
            equity = curve[-1]["equity"]
            unrealized = [246.8,-132.4,815.6,398.2,-54.1][i]
            metrics = {"equity":equity,"unrealized_pnl":unrealized,"realized_pnl":round(equity-initial-unrealized,2),"margin_used":round(equity*.19,2),"return_pct":round((equity/initial-1)*100,2),"win_rate":61.3-i*2.1,"profit_factor":1.81-i*.09,"sharpe":1.62-i*.12,"max_drawdown_pct":4.2+i*.4,"max_drawdown_u":round(initial*(.042+i*.004),2),"position_count":2 if i<3 else 1,"long_count":1,"short_count":1 if i<3 else 0,"today_pnl":round(initial*.0031,2),"month_pnl":round(initial*.032,2),"running_days":60,"curve":curve,"orders":[],"positions":[],"data_updated_at":now(),"illustrative":True}
            if manifest.get("market")=="spot":
                metrics["short_count"]=0
                metrics["long_count"]=metrics["position_count"]
            params = manifest.get("parameters", {}).get("properties", {})
            config = {k:v.get("default") if isinstance(v,dict) else v for k,v in params.items()} if isinstance(params,dict) else {}
            con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (account_id,person,name,"paper" if i!=3 else "live",uid(),strategy["id"],dump(config),dump(["BTCUSDT","ETHUSDT"] if i==2 else ["BTCUSDT"]),initial,dump(metrics),None,None,None,"demo",now()))
            con.execute("INSERT INTO instances(account_id) VALUES(?)", (account_id,))
        candles = {}
        for symbol,base in [("BTCUSDT",62000),("ETHUSDT",3200),("SOLUSDC",145)]:
            rows=[]
            previous=base
            for j in range(2880):
                close=base*(1+j*.000035+.035*math.sin(j*.027)+.016*math.sin(j*.119))
                rows.append({"timestamp":base_ts+j*60000,"open":previous,"high":max(previous,close)*1.002,"low":min(previous,close)*.998,"close":close,"volume":120+j%87})
                previous=close
            candles[symbol]=rows
        serialized=dump(candles)
        con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?)", ("demo-1m","演示行情 · BTC / ETH / SOL","futures","1m",dump(list(candles)),dump(write_candles(candles)),hashlib.sha256(serialized.encode()).hexdigest(),"synthetic",now()))
        for i in range(3):
            strat=strategies[0]
            config={"strategy_id":strat["id"],"parameters":{},"symbols":["BTCUSDT"],"market":"futures","initial_capital":10000,"fee_rate":.0004,"slippage_bps":2,"train_start":"2026-09-01","train_end":"2026-09-30","validation_start":"2026-10-01","validation_end":"2026-10-30","dataset_id":"demo-1m","seed":42}
            curve=[{"timestamp":base_ts+j*86400000,"equity":round(10000*(1+j*.003+math.sin(j*.7+i)*.012),2)} for j in range(30)]
            metrics={"total_return_pct":18.64-i*3.2,"annualized_return_pct":87.9-i*8,"max_drawdown_pct":5.24+i*.6,"profit_factor":1.86-i*.12,"win_rate":58.2+i*2,"sharpe":1.72-i*.16,"final_equity":11864-i*320,"total_trades":84+i*12,"long_trades":51+i*4,"short_trades":33+i*8,"max_consecutive_losses":4+i,"net_profit":1864-i*320}
            validation={**metrics,"total_return_pct":11.27-i*2,"max_drawdown_pct":6.12+i*.4,"profit_factor":1.59-i*.1,"sharpe":1.41-i*.12,"final_equity":11127-i*200}
            result={"metrics":metrics,"validation_metrics":validation,"curve":curve,"validation_curve":[{**p,"equity":round(10000+(p["equity"]-10000)*.72,2),"timestamp":p["timestamp"]+30*86400000} for p in curve],"orders":[],"decisions":[],"illustrative":True}
            provenance={"strategy_version":parse(strat["manifest"],{}).get("version","1.0.0"),"strategy_hash":strat["source_hash"],"data_hash":hashlib.sha256(serialized.encode()).hexdigest(),"engine_version":ENGINE_VERSION,"seed":42,"source":"demo","environment":"illustrative fixture"}
            con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,progress,config,provenance,result,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"demo-exp-{i+1}",f"趋势参数实验 #{i+1}",strat["id"],"demo-1m","completed",100,dump(config),dump(provenance),dump(store_result(result)),now(),now()))
        for level,message in [("debug","演示数据已载入：收益与订单均为示例，不代表实际交易"),("debug","控制服务启动；等待独立运行进程连接"),("error","实盘执行闸门关闭，禁止提交交易所订单"),("debug","策略接入协议已加载，上传策略将执行静态契约检查")]:
            con.execute("INSERT INTO logs(level,message,source,created_at) VALUES(?,?,?,?)", (level,message,"demo",now()))
    return True
