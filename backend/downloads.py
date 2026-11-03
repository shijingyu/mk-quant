"""Durable public-candle jobs, claimed by the isolated research worker.

The API only enqueues work. Database leases fence progress and publication, and
one nullable unique active slot bounds concurrency even across API processes.
"""
from datetime import datetime, timezone
import hashlib
import math
import os
import re
import signal
import threading
import time
from pymysql.err import OperationalError

from . import db, storage

LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
ACTIVE_SLOT = "dataset-download"


class DownloadConflict(ValueError):
    def __init__(self, message, job):
        super().__init__(message)
        self.job = job


class DownloadLeaseLost(RuntimeError):
    pass


class DownloadTimeout(RuntimeError):
    """Stall deadline must bypass the exchange transport's retry types."""
    pass


def stall_seconds():
    # Existing deployments may retain the old setting; it now bounds silence,
    # never the duration of an actively downloading or rate-limited task.
    return max(30,min(int(os.getenv("MK_DOWNLOAD_STALL_SECONDS",os.getenv("MK_DOWNLOAD_TIMEOUT_SECONDS","900"))),3600))


def _timestamp(value):
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("时间戳必须为有限数字")
        return int(value if value > 1e11 else value * 1000)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    except (ValueError, TypeError) as exc:
        raise ValueError("时间格式必须为 ISO 日期或 Unix 时间戳") from exc


def normalize_config(body):
    market = body.get("market", "futures")
    market = "futures" if market == "perpetual" else market
    if not isinstance(market,str) or market not in {"spot", "futures"}:
        raise ValueError("不支持该市场")
    symbols = body.get("symbols", [])
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10 or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{4,32}", s) or not s.endswith(("USDT", "USDC")) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("单次选择 1–10 个不重复的 USDT / USDC 交易对")
    interval = body.get("interval", "1m")
    if interval != "1m":
        raise ValueError("仅支持下载 1 分钟 K 线；其他策略周期由完整 1 分钟行情合成")
    start = _timestamp(body.get("start", body.get("start_date")))
    end = min(_timestamp(body.get("end", body.get("end_date"))), int(time.time() * 1000))
    if start < 0 or start >= end:
        raise ValueError("下载起始日期必须早于结束日期，且区间须包含已发生的行情")
    name = str(body.get("name") or "币安公开 K 线").strip()
    if not name or len(name) > 255:
        raise ValueError("数据集名称需要 1–255 个字符")
    return {"name":name,"market":market,"interval":interval,"symbols":sorted(symbols),"start":start,"end":end}


def public_job(row):
    keys = ("id","status","progress","current_symbol","downloaded_bars","completed_symbols","total_symbols","attempts","dataset_id","error","waiting_reason","retry_at","created_at","started_at","updated_at","completed_at")
    return {**{key:row[key] for key in keys},"config":db.parse(row["config"],{})}


def list_jobs():
    with db.connect() as con:
        rows = con.execute("SELECT * FROM data_download_jobs ORDER BY (active_slot IS NOT NULL) DESC,created_at DESC,id DESC LIMIT 50").fetchall()
    return [public_job(row) for row in rows]


def get_job(identity):
    with db.connect() as con:
        row = con.execute("SELECT * FROM data_download_jobs WHERE id=?", (identity,)).fetchone()
    return public_job(row) if row else None


def temporary_prefix(identity, owner):
    return f"download-{identity}-{hashlib.sha256(owner.encode()).hexdigest()[:16]}-"


def cleanup_temporary(identity, owner=None):
    """Remove only task-owned incomplete files after a child is confirmed dead."""
    if not re.fullmatch(r"[a-f0-9]{32}",identity):
        return
    root=storage.artifact_root()
    directory=root/"candles"
    if not directory.is_dir() or root not in directory.resolve().parents:
        return
    prefix=temporary_prefix(identity,owner) if owner else f"download-{identity}-"
    for path in directory.glob(".writing-"+prefix+"*.parquet"):
        if owner is None:
            match=re.fullmatch(r"\.writing-download-[a-f0-9]{32}-[a-f0-9]{16}-(\d+)-.+\.parquet",path.name)
            if not match:
                continue
            try:
                os.kill(int(match[1]),0)
                continue
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
        path.unlink(missing_ok=True)


def enqueue(body, actor_id):
    config = normalize_config(body)
    identity = db.uid()
    request_key = body.get("request_key")
    if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_key)):
        raise ValueError("request_key 必须为 1–128 位字母、数字、短横线或下划线")
    # Hash the requested end, not the moving now-cutoff. Retrying a request with
    # a future end must resolve to the original immutable download window.
    requested = {key:config[key] for key in ("market","interval","symbols","start","end")}
    requested["end"] = _timestamp(body.get("end",body.get("end_date")))
    fingerprint = hashlib.sha256(db.dump(requested).encode()).hexdigest()
    # A uniqueness race is resolved by reading the winner in a new transaction.
    for attempt in range(3):
        try:
            with db.connect() as con:
                previous = con.execute("SELECT * FROM data_download_jobs WHERE request_key=? FOR UPDATE", (request_key,)).fetchone() if request_key else None
                if previous:
                    if previous["request_hash"] != fingerprint or previous["requested_by"] != actor_id:
                        raise DownloadConflict("该请求编号已用于另一次下载，请使用新的请求编号", public_job(previous))
                    return {**public_job(previous),"reused":True}
                active = con.execute("SELECT * FROM data_download_jobs WHERE active_slot=? FOR UPDATE", (ACTIVE_SLOT,)).fetchone()
                if active:
                    if active["request_hash"] == fingerprint:
                        return {**public_job(active),"reused":True}
                    raise DownloadConflict("已有后台下载任务，请等待完成后再提交其他下载", public_job(active))
                at = db.now()
                con.execute("INSERT INTO data_download_jobs(id,request_key,request_hash,requested_by,config,status,active_slot,total_symbols,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                            (identity,request_key,fingerprint,actor_id,db.dump(config),ACTIVE_SLOT,len(config["symbols"]),at,at))
                db.audit(con,actor_id,"dataset.download.enqueue","data_download",identity,{"config":config})
                row = con.execute("SELECT * FROM data_download_jobs WHERE id=?", (identity,)).fetchone()
                return {**public_job(row),"reused":False}
        except db.IntegrityError:
            if attempt == 2:
                raise
        except OperationalError as exc:
            if exc.args[0] not in {1205,1213} or attempt == 2:
                raise
            time.sleep(.02 * (attempt+1))


def claim(owner):
    """Return (claimed row, busy) and recover expired work with a fresh owner."""
    with db.connect() as con:
        row = con.execute("SELECT * FROM data_download_jobs WHERE active_slot=? FOR UPDATE", (ACTIVE_SLOT,)).fetchone()
        if not row:
            return None, False
        if row["status"] == "running" and row["lease_until"] > time.time():
            return None, True
        cleanup_temporary(row["id"])
        if row["attempts"] >= MAX_ATTEMPTS:
            con.execute("UPDATE data_download_jobs SET status='failed',active_slot=NULL,lease_until=0,waiting_reason=NULL,retry_at=NULL,error=?,completed_at=?,updated_at=? WHERE id=?",
                        ("下载工作进程多次中断，已保留失败记录；可重新提交",db.now(),db.now(),row["id"]))
            return None, False
        token = owner + ":" + db.uid()
        con.execute("UPDATE data_download_jobs SET status='running',owner=?,lease_until=?,worker_pid=NULL,attempts=attempts+1,progress=0,downloaded_bars=0,completed_symbols=0,current_symbol=NULL,error=NULL,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                    (token,time.time()+LEASE_SECONDS,db.now(),db.now(),row["id"]))
        row = con.execute("SELECT * FROM data_download_jobs WHERE id=?", (row["id"],)).fetchone()
        db.audit(con,row["requested_by"],"dataset.download.claim","data_download",row["id"],{"attempt":row["attempts"]})
        return dict(row), True


def progress(identity, owner, *, completed_symbols, total_symbols, downloaded_bars, current_symbol, fraction, waiting_reason=None, retry_at=None):
    with db.connect() as con:
        result = con.execute("UPDATE data_download_jobs SET progress=?,downloaded_bars=?,completed_symbols=?,current_symbol=?,waiting_reason=?,retry_at=?,lease_until=?,updated_at=? WHERE id=? AND owner=? AND status='running' AND lease_until>?",
                             (min(99.5,100*(completed_symbols+max(0,min(fraction,1)))/total_symbols),downloaded_bars,completed_symbols,current_symbol,waiting_reason,retry_at,time.time()+LEASE_SECONDS,db.now(),identity,owner,time.time()))
        if not result.rowcount:
            raise DownloadLeaseLost("下载任务租约已失效，旧执行者禁止继续发布结果")


def fail(identity, owner, message):
    with db.connect() as con:
        row = con.execute("SELECT * FROM data_download_jobs WHERE id=? FOR UPDATE", (identity,)).fetchone()
        if not row or row["owner"] != owner or row["status"] != "running" or row["lease_until"] <= time.time():
            return
        con.execute("UPDATE data_download_jobs SET status='failed',active_slot=NULL,lease_until=0,waiting_reason=NULL,retry_at=NULL,error=?,updated_at=?,completed_at=? WHERE id=?", (message[:2000],db.now(),db.now(),identity))
        db.audit(con,row["requested_by"],"dataset.download.failed","data_download",identity,{"error":message[:2000]})


def run(identity, owner):
    from fastapi import HTTPException
    from .app import publish_dataset_artifact
    from .binance import BinanceClient, BinanceError
    with db.connect() as con:
        row = con.execute("SELECT * FROM data_download_jobs WHERE id=? FOR UPDATE", (identity,)).fetchone()
        if not row or row["owner"] != owner or row["status"] != "running" or row["lease_until"] <= time.time():
            return
        con.execute("UPDATE data_download_jobs SET worker_pid=?,updated_at=? WHERE id=?", (os.getpid(),db.now(),identity))
    config = db.parse(row["config"])
    state = {"completed_symbols":0,"total_symbols":len(config["symbols"]),"downloaded_bars":0,
             "current_symbol":None,"fraction":0,"waiting_reason":row.get("waiting_reason"),"retry_at":row.get("retry_at")}
    previous_signals = {}
    alarm_active = hasattr(signal,"SIGALRM") and threading.current_thread() is threading.main_thread()
    last_update = [0.0]

    def heartbeat(force=False):
        # Reset even while validating/hashing a large file. Waiting callbacks
        # prove that the exchange cooldown is advancing, not a stuck request.
        if alarm_active:
            signal.alarm(stall_seconds())
        if force or time.monotonic()-last_update[0] >= 10:
            progress(identity,owner,**state)
            last_update[0] = time.monotonic()

    def waiting(event):
        remaining = max(0,float(event.get("seconds",0)))
        reason=event.get("reason") or "等待行情接口恢复"
        reason={"rate_limit":"币安限流冷却，等待恢复","request_weight":"行情接口请求权重接近上限，等待窗口恢复","request_spacing":"按行情接口频率要求等待","ip_ban":"币安 IP 限制冷却，等待恢复","network_retry":"网络连接暂时失败，等待重试","server_retry":"币安服务暂时异常，等待重试"}.get(reason,reason)
        state["waiting_reason"] = str(reason)[:2000] if remaining>=1 else None
        state["retry_at"] = float(event["retry_at"])/1000 if remaining and event.get("retry_at") else None
        heartbeat(force=True)

    if alarm_active:
        def timed_out(signum,frame):
            raise DownloadTimeout("下载任务长时间没有进展，已停止；请检查网络或工作进程后重试")
        previous_signals[signal.SIGALRM] = signal.signal(signal.SIGALRM,timed_out)
        previous_signals[signal.SIGTERM] = signal.signal(signal.SIGTERM,timed_out)
        signal.alarm(stall_seconds())
    try:
        if config.get("interval") != "1m":
            raise ValueError("旧下载任务不是 1 分钟周期，请重新提交 1 分钟行情下载")
        # A replacement process must respect an already persisted IP cooldown
        # before sending even exchangeInfo. Claim does not erase these fields.
        resume_at = float(state["retry_at"] or 0)
        while resume_at > time.time():
            remaining = resume_at-time.time()
            waiting({"seconds":remaining,"reason":state["waiting_reason"] or "恢复上次行情接口冷却等待","retry_at":resume_at*1000})
            time.sleep(min(15,remaining))
        state.update(waiting_reason=None,retry_at=None)
        heartbeat(force=True)
        client = BinanceClient(market=config["market"])
        with storage.CandleStreamWriter(config["symbols"],heartbeat=heartbeat,temporary_prefix=temporary_prefix(identity,owner)+str(os.getpid())+"-") as writer:
            for index, symbol in enumerate(config["symbols"]):
                state.update(completed_symbols=index,current_symbol=symbol,fraction=0)
                heartbeat(force=True)
                for page in client.iter_klines(symbol,config["interval"],config["start"],config["end"],progress=lambda event:heartbeat(),on_wait=waiting):
                    writer.append(symbol,page)
                    state.update(downloaded_bars=writer.count,waiting_reason=None,retry_at=None)
                    if page:
                        state["fraction"] = (page[-1]["timestamp"]+60000-config["start"])/max(1,config["end"]-config["start"])
                    heartbeat(force=True)
                state.update(completed_symbols=index+1,fraction=0)
                heartbeat(force=True)
            with storage.lifecycle_lock():
                artifact = writer.finish()
                heartbeat(force=True)
                publish_dataset_artifact(config,"binance_public",artifact,row["requested_by"],download_id=identity,download_owner=owner)
    except DownloadLeaseLost:
        return
    except Exception as exc:
        if isinstance(exc,OSError) and exc.errno in {28,122}:
            message = "磁盘空间不足，下载已停止且未发布数据集；请清理磁盘后重试"
        else:
            message = str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,(BinanceError,ValueError,DownloadTimeout)) else "下载处理失败（"+type(exc).__name__+"），请检查工作进程日志"
        fail(identity,owner,message)
    finally:
        if alarm_active:
            signal.alarm(0)
            for signum, previous in previous_signals.items():
                signal.signal(signum,previous)
