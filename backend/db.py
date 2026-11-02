"""SQLite WAL control store. No runtime state lives exclusively in memory."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time

ENGINE_VERSION = "1.0.0"


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return secrets.token_hex(16)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse(value, fallback=None):
    return json.loads(value) if value else fallback


@contextmanager
def connect():
    path = Path(os.environ.get("MK_DB_PATH", "data/mk.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,phone TEXT,password TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','client')),created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode IN ('paper','live')),route TEXT UNIQUE NOT NULL,strategy_id TEXT NOT NULL,config TEXT NOT NULL,symbols TEXT NOT NULL,initial_equity REAL NOT NULL,metrics TEXT NOT NULL,credentials TEXT,external_uid TEXT,check_result TEXT,source TEXT NOT NULL DEFAULT 'local',created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS actual_account_once ON accounts(external_uid) WHERE external_uid IS NOT NULL;
        CREATE TABLE IF NOT EXISTS instances(account_id TEXT PRIMARY KEY REFERENCES accounts(id),desired_state TEXT NOT NULL DEFAULT 'stopped',observed_state TEXT NOT NULL DEFAULT 'stopped',owner TEXT,fence INTEGER NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,last_heartbeat TEXT,error TEXT,config_revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),action TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',requested_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,completed_at TEXT,error TEXT,result TEXT);
        CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY,manifest TEXT NOT NULL,source TEXT NOT NULL,source_hash TEXT NOT NULL,trusted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,name TEXT NOT NULL,market TEXT NOT NULL,interval TEXT NOT NULL,symbols TEXT NOT NULL,candles TEXT NOT NULL,sha256 TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,name TEXT NOT NULL,strategy_id TEXT NOT NULL,dataset_id TEXT NOT NULL,status TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,config TEXT NOT NULL,provenance TEXT NOT NULL,result TEXT,error TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,worker_pid INTEGER,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT);
        CREATE TABLE IF NOT EXISTS presets(id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,name TEXT NOT NULL,parameters TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,level TEXT NOT NULL,strategy_id TEXT,account_id TEXT,message TEXT NOT NULL,details TEXT,source TEXT NOT NULL DEFAULT 'system',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),kind TEXT NOT NULL,reference TEXT UNIQUE,payload TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime_events(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,fence INTEGER NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS services(name TEXT PRIMARY KEY,owner TEXT NOT NULL,heartbeat TEXT NOT NULL,status TEXT NOT NULL);
        """)
    try:
        from .strategy import builtin_strategies
        with connect() as con:
            for item in builtin_strategies():
                manifest = {k: v for k, v in item.items() if k != "source"}
                source = item["source"]
                con.execute("INSERT OR IGNORE INTO strategies VALUES(?,?,?,?,1,?)", (item["id"], dump(manifest), source, hashlib.sha256(source.encode()).hexdigest(), now()))
    except ImportError:
        pass


def log(level, message, *, strategy_id=None, account_id=None, details=None, source="system"):
    with connect() as con:
        con.execute("INSERT INTO logs(level,strategy_id,account_id,message,details,source,created_at) VALUES(?,?,?,?,?,?,?)", (level,strategy_id,account_id,message,dump(details) if details else None,source,now()))


def acquire_lease(account_id, owner, ttl=30):
    """BEGIN IMMEDIATE serializes ownership; monotonically increasing fence invalidates predecessors."""
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM instances WHERE account_id=?", (account_id,)).fetchone()
        if not row or (row["lease_until"] > time.time() and row["owner"] != owner):
            return None
        fence = row["fence"] + (row["owner"] != owner or row["lease_until"] <= time.time())
        con.execute("UPDATE instances SET owner=?,fence=?,lease_until=?,last_heartbeat=? WHERE account_id=?", (owner,fence,time.time()+ttl,now(),account_id))
        return fence


def execution_authorized(account_id, owner, fence, *, opening=False):
    with connect() as con:
        row = con.execute("SELECT * FROM instances WHERE account_id=?", (account_id,)).fetchone()
        return bool(row and row["owner"] == owner and row["fence"] == fence and row["lease_until"] > time.time() and (not opening or row["desired_state"] == "running"))


def seed_demo():
    """Opt-in synthetic fixtures. They never represent exchange balances or executed trades."""
    from .security import hash_password
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
                rows.append({"timestamp":base_ts+j*1800000,"open":previous,"high":max(previous,close)*1.002,"low":min(previous,close)*.998,"close":close,"volume":120+j%87})
                previous=close
            candles[symbol]=rows
        serialized=dump(candles)
        con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?)", ("demo-30m","演示行情 · BTC / ETH / SOL","futures","30m",dump(list(candles)),serialized,hashlib.sha256(serialized.encode()).hexdigest(),"synthetic",now()))
        for i in range(3):
            strat=strategies[0]
            config={"strategy_id":strat["id"],"parameters":{},"symbols":["BTCUSDT"],"market":"futures","initial_capital":10000,"fee_rate":.0004,"slippage_bps":2,"train_start":"2026-09-01","train_end":"2026-09-30","validation_start":"2026-10-01","validation_end":"2026-10-30","dataset_id":"demo-30m","seed":42}
            curve=[{"timestamp":base_ts+j*86400000,"equity":round(10000*(1+j*.003+math.sin(j*.7+i)*.012),2)} for j in range(30)]
            metrics={"total_return_pct":18.64-i*3.2,"annualized_return_pct":87.9-i*8,"max_drawdown_pct":5.24+i*.6,"profit_factor":1.86-i*.12,"win_rate":58.2+i*2,"sharpe":1.72-i*.16,"final_equity":11864-i*320,"total_trades":84+i*12,"long_trades":51+i*4,"short_trades":33+i*8,"max_consecutive_losses":4+i,"net_profit":1864-i*320}
            validation={**metrics,"total_return_pct":11.27-i*2,"max_drawdown_pct":6.12+i*.4,"profit_factor":1.59-i*.1,"sharpe":1.41-i*.12,"final_equity":11127-i*200}
            result={"metrics":metrics,"validation_metrics":validation,"curve":curve,"validation_curve":[{**p,"equity":round(10000+(p["equity"]-10000)*.72,2),"timestamp":p["timestamp"]+30*86400000} for p in curve],"orders":[],"decisions":[],"illustrative":True}
            provenance={"strategy_version":parse(strat["manifest"],{}).get("version","1.0.0"),"strategy_hash":strat["source_hash"],"data_hash":hashlib.sha256(serialized.encode()).hexdigest(),"engine_version":ENGINE_VERSION,"seed":42,"source":"demo","environment":"illustrative fixture"}
            con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,progress,config,provenance,result,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"demo-exp-{i+1}",f"趋势参数实验 #{i+1}",strat["id"],"demo-30m","completed",100,dump(config),dump(provenance),dump(result),now(),now()))
        for level,message in [("debug","演示数据已载入：收益与订单均为示例，不代表实际交易"),("debug","控制服务启动；等待独立运行进程连接"),("error","实盘执行闸门关闭，禁止提交交易所订单"),("debug","策略接入协议已加载，上传策略将执行静态契约检查")]:
            con.execute("INSERT INTO logs(level,message,source,created_at) VALUES(?,?,?,?)", (level,message,"demo",now()))
    return True
