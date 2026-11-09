"""Independent workers: python -m backend.worker backtest | runtime | init-demo.

Web shutdown never owns the lifetime of these workers. The backtest coordinator
forks a bounded subprocess per job; runtime isolates each account in a process.
Live order submission is intentionally gated until production acceptance.
"""
from .account_settings import configured_leverage
from .strategy import resolve_position_mode
import argparse
import copy
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import random
import signal
from contextlib import contextmanager
import subprocess
import sys
import time
import traceback

from . import db, storage, downloads


# Only transport/availability and transaction-conflict failures are retried.
# Authentication, schema, disk-full and strategy failures remain explicit errors.
_TRANSIENT_DB_CODES={1040,1042,1047,1053,1158,1159,1160,1161,1205,1213,1927,2002,2003,2006,2013}


def transient_database_error(exc):
    from pymysql.err import OperationalError, InterfaceError
    code=exc.args[0] if exc.args else None
    return (isinstance(exc,OperationalError) and code in _TRANSIENT_DB_CODES
            or isinstance(exc,InterfaceError) and code in {0,2006,2013})


def _database_retry_wait(role,exc,failures):
    delay=min(15,2**min(failures-1,4))
    # Database logging cannot be used while that database is unavailable.
    # Deliberately omit the exception message, which may contain SQL/config.
    print(f"{role}: temporary database failure ({type(exc).__name__}:{exc.args[0] if exc.args else 'unknown'}); retry in {delay}s",file=sys.stderr,flush=True)
    time.sleep(delay)


def retry_database(operation,role,*,once=False):
    """Retry by reloading durable state, never by replaying a failed SQL call.

    A lost COMMIT acknowledgement is ambiguous. Callers must reconstruct work
    from MySQL on each attempt; account ticks reacquire their fence and reload
    the snapshot/order cursor before calculating or persisting any new action.
    """
    failures=0
    while True:
        try:
            return operation()
        except Exception as exc:
            if once or not transient_database_error(exc):
                raise
            failures+=1
            _database_retry_wait(role,exc,failures)


def service(name,owner):
    with db.connect() as con:
        con.execute("INSERT INTO services VALUES(?,?,?,'online') ON DUPLICATE KEY UPDATE owner=VALUES(owner),heartbeat=VALUES(heartbeat),status='online'",(name,owner,db.now()))


def ui_metrics(m):
    d=dict(m)
    for old,new in (("total_return","total_return_pct"),("annual_return","annualized_return_pct"),("max_drawdown","max_drawdown_pct")):
        d[new]=m.get(old)*100 if m.get(old) is not None else None
    if m.get("win_rate") is not None:
        d["win_rate"]=m["win_rate"]*100
    d.update(total_trades=m.get("total_orders",0),long_trades=m.get("long_orders",0),short_trades=m.get("short_orders",0),net_profit=m.get("final_equity",0)-m.get("initial_equity",0))
    return d


def run_experiment(id):
    from .engine import run_backtest,BacktestCancelled
    from .research import ResearchSpace, CandleWindow, RecordView, ExtremaPreview
    with db.connect() as con:
        row=con.execute("SELECT * FROM experiments WHERE id=? FOR UPDATE",(id,)).fetchone()
        if not row or row["status"] not in {"queued","running"}:
            return
        if row["status"]=="running" and row["worker_pid"] not in {None,os.getpid()}:
            return
        if row["cancel_requested"]:
            con.execute("UPDATE experiments SET status='cancelled',completed_at=? WHERE id=?",(db.now(),id))
            return
        strategy=con.execute("SELECT * FROM strategies WHERE id=?",(row["strategy_id"],)).fetchone()
        config=db.parse(row["config"],{})
        provenance=db.parse(row["provenance"],{})
        version=con.execute("SELECT * FROM strategy_versions WHERE id=?",(config.get("strategy_version_id",provenance.get("strategy_version_id","")),)).fetchone()
        if not version:
            version=con.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND source_hash=?",(row["strategy_id"],provenance.get("strategy_hash",""))).fetchone()
        if version and strategy:
            strategy={**dict(version),"trusted":strategy["trusted"]}
        dataset=con.execute("SELECT * FROM datasets WHERE id=?",(row["dataset_id"],)).fetchone()
        con.execute("UPDATE experiments SET status='running',worker_pid=?,started_at=? WHERE id=?",(os.getpid(),db.now(),id))
    space = data = published = None
    committed=False
    partial={"curve":[],"validation_curve":[],"metrics":{},"validation_metrics":{},"incomplete":True,"symbol_results":{}}
    try:
        if not strategy or not strategy["trusted"]:
            raise ValueError("自定义代码未经离线安全审核，禁止执行")
        if not version or strategy["source_hash"]!=provenance.get("strategy_hash"):
            raise ValueError("实验固定的策略版本缺失或不匹配，拒绝使用变化后的源码")
        manifest=db.parse(strategy["manifest"],{})
        if dataset["interval"]!="1m" or config.get("base_interval")!="1m":
            raise ValueError("该任务使用旧高周期行情；请以 1 分钟基础数据重新创建回测，历史结果仍可查看")
        partial["symbol_results"]={symbol:{"curve":[],"validation_curve":[],"metrics":{},"validation_metrics":{},
            "allocation":{"method":"equal_initial_capital","initial_capital":config["initial_capital"]/len(config["symbols"])}}
            for symbol in config["symbols"]}
        ranges=config["ranges_ms"]
        random.seed(config["seed"])
        filters=None
        # Network independence keeps historical experiments reproducible. Rules are
        # supplied with the job when captured; fallback is exposed in assumptions.
        filters=config.get("symbol_filters")
        space=ResearchSpace(storage.artifact_root())
        output={}
        cancellation_checked=0.0
        cancellation_value=False
        last_publish=0.0
        accumulators={}
        def cancelled():
            nonlocal cancellation_checked,cancellation_value
            now=time.monotonic()
            if now-cancellation_checked<1:
                return cancellation_value
            cancellation_checked=now
            with db.connect() as con:
                current=con.execute("SELECT cancel_requested FROM experiments WHERE id=?",(id,)).fetchone()
            cancellation_value=not current or bool(current[0])
            return cancellation_value
        for i,(split,start,end) in enumerate((("train",ranges[0],ranges[1]),("validation",ranges[2],ranges[3]))):
            data=CandleWindow(config.get("data_selection",dataset["candles"]),config["symbols"],start,end)
            curve_key="curve" if split=="train" else "validation_curve"
            def progress(event):
                nonlocal last_publish
                groups={"":event.get("equity",[]), **event.get("symbol_equity",{})}
                for symbol,points in groups.items():
                    accumulator=accumulators.setdefault((curve_key,symbol),ExtremaPreview(event["total"]))
                    accumulator.extend(points)
                if time.monotonic()-last_publish<2 and event.get("status")!="completed":
                    return
                last_publish=time.monotonic()
                partial[curve_key]=accumulators[(curve_key,"")].points()
                for symbol in config["symbols"]:
                    partial["symbol_results"][symbol][curve_key]=accumulators[(curve_key,symbol)].points()
                value=min(99.9,(i+event["processed"]/max(event["total"],1))*50)
                partial["phase"]=split
                partial["execution_progress"]={"split":split,"processed":event["processed"],"total":event["total"],"updated_at":db.now()}
                preview=storage.result_preview(partial)
                with db.connect() as con:
                    con.execute("UPDATE experiments SET progress=?,result=? WHERE id=? AND status='running'",(value,db.dump(preview),id))
            output[split]=run_backtest(strategy["source"],config["parameters"],data,config["initial_capital"],config["fee_rate"],config["slippage_bps"],market=config["market"],leverage=config.get("leverage",manifest["account"]["leverage_min"]),symbol_filters=filters,progress=progress,cancelled=cancelled,base_interval="1m",research_space=space,position_mode=config.get("position_mode"))
            data.close()
            data=None
            partial["metrics" if split=="train" else "validation_metrics"]=ui_metrics(output[split]["metrics"])
            for symbol,value in output[split]["symbol_results"].items():
                partial["symbol_results"][symbol]["metrics" if split=="train" else "validation_metrics"]=ui_metrics(value["metrics"])
        if cancelled():
            raise BacktestCancelled("任务取消")
        result={"metrics":ui_metrics(output["train"]["metrics"]),"validation_metrics":ui_metrics(output["validation"]["metrics"]),"curve":output["train"]["equity"],"validation_curve":output["validation"]["equity"],"orders":RecordView([(split,r["orders"]) for split,r in output.items()],transform=lambda row,split:dict(row,split=split)),"decisions":RecordView([(split,r["decisions"]) for split,r in output.items()],transform=lambda row,split:dict(row,split=split)),"assumptions":output["train"]["assumptions"],"incomplete":False}
        result["symbol_results"]={}
        for symbol in config["symbols"]:
            train,validation=output["train"]["symbol_results"][symbol],output["validation"]["symbol_results"][symbol]
            item={"metrics":ui_metrics(train["metrics"]),"validation_metrics":ui_metrics(validation["metrics"]),
                  "curve":train["equity"],"validation_curve":validation["equity"],
                  "allocation":train.get("allocation",{"method":"equal_initial_capital","initial_capital":train["metrics"]["initial_equity"]}),
                  "account_summaries":{"train":train["account"],"validation":validation["account"]}}
            item["differences"]={key:item["validation_metrics"][key]-value for key,value in item["metrics"].items()
                                 if isinstance(value,(int,float)) and isinstance(item["validation_metrics"].get(key),(int,float))}
            result["symbol_results"][symbol]=item
        if manifest.get("execution",{}).get("model")=="maker_grid_v1":
            result.update(open_orders=[dict(order,split=split) for split,value in output.items() for order in value["open_orders"]],
                          lots=[dict(lot,split=split) for split,value in output.items() for lot in value["lots"]],
                          account_summaries={split:value["account"] for split,value in output.items()})
        result["differences"]={k:result["validation_metrics"][k]-v for k,v in result["metrics"].items() if isinstance(v,(int,float)) and isinstance(result["validation_metrics"].get(k),(int,float))}
        def publish_heartbeat():
            nonlocal last_publish
            if cancelled():
                raise BacktestCancelled("发布时取消")
            if time.monotonic()-last_publish>=2:
                with db.connect() as con:
                    con.execute("UPDATE experiments SET result=JSON_SET(result,'$.publication_heartbeat',?,'$.phase','publishing') WHERE id=? AND status='running'",(db.now(),id))
                last_publish=time.monotonic()
        with storage.lifecycle_lock():
            published={"artifacts":{}}
            stored_result=storage.store_result(result,heartbeat=publish_heartbeat,published=published["artifacts"])
            published=stored_result
            with db.connect() as con:
                current=con.execute("SELECT cancel_requested,status FROM experiments WHERE id=? FOR UPDATE",(id,)).fetchone()
                if not current or current["cancel_requested"] or current["status"] != "running":
                    raise BacktestCancelled("任务取消")
                con.execute("UPDATE experiments SET status='completed',progress=100,result=?,completed_at=?,error=NULL WHERE id=?",(db.dump(stored_result),db.now(),id))
                db.audit(con,None,"experiment.complete","experiment",id,{"strategy_version_id":version["id"],"artifact_counts":{key:manifest["count"] for key,manifest in stored_result["artifacts"].items()}})
        committed=True
        db.log("debug","回测及验证集完成",strategy_id=row["strategy_id"],details={"experiment_id":id})
    except BacktestCancelled:
        with db.connect() as con:
            con.execute("UPDATE experiments SET status='cancelled',completed_at=?,error='用户取消；部分曲线不是最终结果' WHERE id=?",(db.now(),id))
    except Exception as exc:
        memory_failure=isinstance(exc,MemoryError) or "memory" in str(exc).lower() or "malloc" in str(exc).lower()
        if memory_failure:
            # Free the failing engine/read stack before allocating a DB error report.
            exc.__traceback__=None
            if data is not None:
                data.close()
                data=None
            if "output" in locals():
                output.clear()
            partial.clear()
        message=("回测超过单任务内存预算，请缩短回测区间、减少交易对或由管理员提高独立回测资源预算" if memory_failure else str(exc) or type(exc).__name__)
        with db.connect() as con:
            con.execute("UPDATE experiments SET status='failed',completed_at=?,error=? WHERE id=?",(db.now(),message[:2000],id))
        db.log("error","回测失败",strategy_id=row["strategy_id"],details={"experiment_id":id,"error":message[:2000]})
    finally:
        if data is not None:
            data.close()
        if space is not None:
            space.close()
        if published and not committed:
            from . import artifact_cleanup
            with db.connect() as con:
                cleanup_id=artifact_cleanup.enqueue(con,None,list(published["artifacts"].values()))
            artifact_cleanup.drain(cleanup_id)


def _child_limits():
    """Resource ceilings supplement process isolation; they are not a code sandbox."""
    import resource
    cpu=max(300,int(os.getenv("MK_BACKTEST_CPU_SECONDS","43200")))
    resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu+5))
    if sys.platform.startswith("linux"):
        memory=max(256,int(os.getenv("MK_BACKTEST_ADDRESS_SPACE_MB","768")))*1024*1024
        inherited=resource.getrlimit(resource.RLIMIT_AS)[1]
        if inherited!=resource.RLIM_INFINITY:
            memory=min(memory,inherited)
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    try:
        os.nice(10)
    except PermissionError:
        # Some macOS app sandboxes forbid even lowering our own priority.
        # CPU/wall-clock limits still apply; deployment applies cgroup quotas.
        pass


def backtest_loop(once=False):
    return retry_database(lambda:_backtest_loop(once),"backtest",once=once)


def _backtest_loop(once=False):
    owner=db.uid()
    while True:
        service("backtest",owner)
        with db.connect() as con:
            interrupted=con.execute("SELECT id,worker_pid,started_at FROM experiments WHERE status='running'").fetchall()
            for job in interrupted:
                alive=False
                if job["worker_pid"]:
                    try:
                        os.kill(job["worker_pid"],0)
                        alive=True
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        alive=True
                if not alive and job["started_at"]:
                    from datetime import datetime
                    if time.time()-datetime.fromisoformat(job["started_at"]).timestamp()>60:
                        con.execute("UPDATE experiments SET status='failed',error='原回测工作进程已退出，完整统计未完成；可重跑',completed_at=? WHERE id=? AND status='running'",(db.now(),job["id"]))
            still_running=con.execute("SELECT id FROM experiments WHERE status='running' LIMIT 1").fetchone()
        # A predecessor coordinator can die while its isolated research child
        # survives. Wait for that child (including the short pid-registration
        # window) before allocating resources to either kind of queued job.
        if still_running:
            if once:
                return
            time.sleep(.8)
            continue
        download,busy=downloads.claim(owner)
        if download:
            _run_download_child(download,owner)
        if busy:
            if once:
                return
            time.sleep(.8)
            continue
        with db.connect() as con:
            row=con.execute("SELECT id FROM experiments WHERE status='queued' AND cancel_requested=0 ORDER BY created_at,id LIMIT 1 FOR UPDATE").fetchone()
            if row:
                con.execute("UPDATE experiments SET status='running',started_at=? WHERE id=? AND status='queued'",(db.now(),row["id"]))
        if row:
            env={k:v for k,v in os.environ.items() if k not in {"MK_MASTER_KEY","MK_ENABLE_LIVE"}}
            try:
                child=subprocess.Popen([sys.executable,"-m","backend.worker","job",row["id"]],env=env,preexec_fn=_child_limits if os.name=="posix" else None)
            except (OSError,subprocess.SubprocessError) as exc:
                with db.connect() as con:
                    con.execute("UPDATE experiments SET status='failed',error=?,completed_at=? WHERE id=?",(f"无法启动独立工作进程：{type(exc).__name__}",db.now(),row["id"]))
                if once:
                    return
                continue
            started=time.monotonic()
            try:
                _monitor_job(child,row["id"],owner,started)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill();child.wait()
            with db.connect() as con:
                con.execute("UPDATE experiments SET status='failed',error='回测工作进程退出或超时；可重跑',completed_at=? WHERE id=? AND status='running'",(db.now(),row["id"]))
        if once:
            return
        time.sleep(.8)


def _monitor_job(child,job_id,owner,started):
    last_progress=None
    last_advance=time.monotonic()
    while child.poll() is None:
        service("backtest",owner)
        with db.connect() as con:
            cancellation=con.execute("SELECT cancel_requested,progress,status,JSON_EXTRACT(result,'$.publication_heartbeat') AS publication_heartbeat FROM experiments WHERE id=?",(job_id,)).fetchone()
        if cancellation and cancellation[0]:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
            with db.connect() as con:
                con.execute("UPDATE experiments SET status='cancelled',completed_at=?,error='用户取消；部分结果不是最终统计' WHERE id=? AND status='running'",(db.now(),job_id))
            break
        value=(cancellation["progress"],cancellation["status"],cancellation["publication_heartbeat"]) if cancellation else None
        if value!=last_progress:
            last_progress,last_advance=value,time.monotonic()
        stalled=time.monotonic()-last_advance>max(60,int(os.getenv("MK_BACKTEST_STALL_SECONDS","900")))
        deadline=time.monotonic()-started>max(360,int(os.getenv("MK_BACKTEST_WALL_SECONDS","86400")))
        if stalled or deadline:
            child.kill();child.wait()
            with db.connect() as con:
                con.execute("UPDATE experiments SET status='failed',completed_at=?,error=? WHERE id=? AND status='running'",(db.now(),"回测长时间无进展" if stalled else "回测超过总运行时长预算",job_id))
            break
        time.sleep(.5)


def _download_limits():
    # Large historical downloads have no total CPU/wall deadline. They remain
    # isolated, low priority and memory bounded; their watchdog detects stalls.
    try:
        os.nice(10)
    except PermissionError:
        pass
    if sys.platform.startswith("linux"):
        import resource
        memory=max(256,min(int(os.getenv("MK_DOWNLOAD_MEMORY_MB","768")),2048))*1024*1024
        inherited=resource.getrlimit(resource.RLIMIT_AS)[1]
        if inherited!=resource.RLIM_INFINITY:
            memory=min(memory,inherited)
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))


def _run_download_child(job,coordinator):
    """Research CPU/memory budgets are never shared with the runtime workers."""
    env={k:v for k,v in os.environ.items() if k not in {"MK_MASTER_KEY","MK_ENABLE_LIVE"}}
    env.update(OMP_NUM_THREADS="1",OPENBLAS_NUM_THREADS="1",MKL_NUM_THREADS="1",NUMEXPR_NUM_THREADS="1")
    try:
        child=subprocess.Popen([sys.executable,"-m","backend.worker","download-job",job["id"],"--download-owner",job["owner"]],env=env,preexec_fn=_download_limits if os.name=="posix" else None)
    except (OSError,subprocess.SubprocessError) as exc:
        downloads.fail(job["id"],job["owner"],f"无法启动独立下载工作进程：{type(exc).__name__}")
        return
    last_activity=time.monotonic()
    last_update=job.get("updated_at")
    error="下载工作进程退出，可能触发资源上限；请检查工作进程日志后重试"
    try:
        while child.poll() is None:
            service("backtest",coordinator)
            with db.connect() as con:
                current=con.execute("SELECT status,owner,lease_until,updated_at FROM data_download_jobs WHERE id=?",(job["id"],)).fetchone()
                if not current or current["status"]!="running" or current["owner"]!=job["owner"] or current["lease_until"]<=time.time():
                    break
                if current["updated_at"]!=last_update:
                    last_update=current["updated_at"]
                    last_activity=time.monotonic()
                if time.monotonic()-last_activity>downloads.stall_seconds():
                    error="下载任务长时间没有进展，已停止；请检查网络或工作进程后重试"
                    break
                # A page request may temporarily have no progress. Only this
                # coordinator's live subprocess can receive a lease extension.
                if current["lease_until"]<time.time()+downloads.LEASE_SECONDS/2:
                    con.execute("UPDATE data_download_jobs SET lease_until=? WHERE id=? AND status='running' AND owner=? AND lease_until>?",(time.time()+downloads.LEASE_SECONDS,job["id"],job["owner"],time.time()))
            time.sleep(.5)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
        downloads.cleanup_temporary(job["id"],job["owner"])
    downloads.fail(job["id"],job["owner"],error)


def ensure_market_cache():
    # DDL belongs to the shared versioned InnoDB schema, never actor ticks.
    return None


def persist_market_cache(market, symbol, candles, filters, fetched_at):
    """Confirm feed freshness without rewriting unchanged candle windows.

    Statement binlogs record the complete SQL payload; ROW/MIXED with FULL
    images also repeat unchanged TEXT fields. Use minimal row images on this
    connection and compare before choosing the write so repeated successful
    polls only log the small freshness update. Compare
    the full contents: exchanges can correct bars or symbol filters without
    changing the last candle timestamp.
    """
    encoded_candles, encoded_filters = db.dump(candles), db.dump(filters)
    with db.connect() as con:
        # Scoped to this short-lived feed connection, never disables binlogs
        # or changes other applications. market_cache has a primary key.
        con.execute("SET SESSION binlog_row_image='MINIMAL'")
        previous = con.execute(
            "SELECT candles,filters FROM market_cache WHERE market=? AND symbol=? AND interval='1m' FOR UPDATE",
            (market, symbol),
        ).fetchone()
        if previous and previous["candles"] == encoded_candles and previous["filters"] == encoded_filters:
            con.execute("UPDATE market_cache SET fetched_at=? WHERE market=? AND symbol=? AND interval='1m'",
                        (fetched_at, market, symbol))
            return False
        con.execute("INSERT INTO market_cache VALUES(?,?,'1m',?,?,?) ON DUPLICATE KEY UPDATE candles=VALUES(candles),filters=VALUES(filters),fetched_at=VALUES(fetched_at)",
                    (market, symbol, encoded_candles, encoded_filters, fetched_at))
    return True


def market_loop(once=False):
    return retry_database(lambda:_market_loop(once),"market",once=once)


def _market_loop(once=False):
    """One shared public feed poller, independent from account strategy processes."""
    from .binance import BinanceClient
    ensure_market_cache()
    clients={}
    while True:
        service("market",str(os.getpid()))
        with db.connect() as con:
            rows=con.execute("SELECT a.symbols,s.manifest FROM accounts a JOIN instances i ON i.account_id=a.id JOIN strategies s ON s.id=a.strategy_id WHERE a.mode='paper' AND a.source!='demo' AND (i.desired_state IN ('running','paused') OR i.observed_state IN ('running','paused','starting','error','flattening') OR EXISTS (SELECT 1 FROM commands c WHERE c.account_id=a.id AND c.action='flatten' AND c.status IN ('pending','running')))").fetchall()
        wanted=set()
        for row in rows:
            manifest=db.parse(row["manifest"],{})
            for symbol in db.parse(row["symbols"],[]):
                wanted.add((manifest["market"],symbol))
        for market,symbol in wanted:
            try:
                client=clients.setdefault(market,BinanceClient(market=market))
                info=client.exchange_info()
                item=next(s for s in info["symbols"] if s["symbol"]==symbol)
                end=int(time.time()//60)*60000
                candles=client.klines(symbol,"1m",end-1000*60000,end)
                candles=[c for c in candles if c["timestamp"]<end]
                persist_market_cache(market, symbol, candles, item.get("filters",item), time.time())
            except Exception as exc:
                db.log("error","共享行情更新失败",details={"symbol":symbol,"market":market,"exception":type(exc).__name__})
        if once:
            return
        time.sleep(15)


def _paper_execution_costs(manifest, metrics=None):
    """Resolve market-paper costs; preserve legacy defaults and honor zero."""
    from .strategy import validate_paper_costs
    declared=manifest.get("execution",{})
    if "paper_costs" in declared:
        if declared.get("model")!="market_next_open_v1":
            raise ValueError("paper_costs 仅适用于市价持续模拟模型")
        return {**validate_paper_costs(declared["paper_costs"]),"fee_source":"strategy_manifest_paper_costs"}
    previous=(metrics or {}).get("execution_model",{})
    default_fee=.001 if manifest["market"]=="spot" else .0004
    fee=previous.get("fee_rate",default_fee) if previous.get("model")!="maker_grid_v1" else default_fee
    return {**validate_paper_costs({"fee_rate":fee,"slippage_bps":2}),"fee_source":"documented_simulation_assumption"}


def _restore_ledger(account,manifest,snapshot):
    from .engine import Ledger,Position
    D=lambda x:Decimal(str(x))
    fee=_paper_execution_costs(manifest,db.parse(account["metrics"],{}))["fee_rate"]
    ledger=Ledger(D(account["initial_equity"]),manifest["market"],D(configured_leverage(account,manifest)),D(fee),position_mode=resolve_position_mode(manifest))
    if snapshot:
        ledger.balance=D(snapshot["balance"])
        ledger.fees=D(snapshot["fees"])
        ledger.positions={s:Position(D(p["quantity"]),D(p["average_price"]),D(p["entry_fees"])) for s,p in snapshot["positions"].items()}
        ledger.orders=snapshot["orders"]
        ledger.closed_pnls=[D(v) for v in snapshot["closed_pnls"]]
    return ledger


def _period_starts(timestamp):
    """Business-day/month cutoffs use Beijing time; stored timestamps stay UTC."""
    from datetime import datetime, timedelta, timezone
    current=datetime.fromtimestamp(timestamp/1000,timezone(timedelta(hours=8)))
    day=current.replace(hour=0,minute=0,second=0,microsecond=0)
    return day.timestamp()*1000,day.replace(day=1).timestamp()*1000


def _convert_flat_snapshot(account,manifest,snapshot,target):
    """Move a flat financial ledger between executors without resetting money."""
    from .engine import Ledger, MakerOrderBook
    source=snapshot.get("model","market_next_open_v1") if snapshot else target
    source_mode = snapshot.get("book",{}).get("ledger",{}).get("position_mode","hedge") if source=="maker_grid_v1" else snapshot.get("position_mode","one_way")
    target_mode = resolve_position_mode(manifest)
    if not snapshot or (source==target and source_mode==target_mode):
        return snapshot,None
    if source=="maker_grid_v1":
        book=MakerOrderBook.load(snapshot["book"])
        if book.has_open_positions() or book.open_orders() or any(position.quantity for position in book.ledger.positions.values()):
            raise ValueError("Maker 持仓或挂单未清空，拒绝切换执行模型")
        previous=book.ledger
        financial={"balance":str(previous.balance),"fees":str(previous.fees),"orders":copy.deepcopy(previous.orders),"closed_pnls":[str(value) for value in previous.closed_pnls]}
    else:
        if any(Decimal(str(position["quantity"])) for position in snapshot.get("positions",{}).values()) or snapshot.get("pending"):
            raise ValueError("原执行模型仍有持仓或待执行意图，拒绝切换执行模型")
        financial={key:copy.deepcopy(snapshot[key]) for key in ("balance","fees","orders","closed_pnls")}
    common={"model":target,"curve":copy.deepcopy(snapshot.get("curve",[])),"last_timestamp":snapshot.get("last_timestamp",0),"strategy_state":{},"bar_index":0,"position_mode":target_mode}
    if target=="maker_grid_v1":
        ledger=Ledger(account["initial_equity"],"futures",configured_leverage(account,manifest),Decimal(".0002"),position_mode=resolve_position_mode(manifest))
        ledger.balance=Decimal(financial["balance"]);ledger.fees=Decimal(financial["fees"])
        ledger.orders=financial["orders"];ledger.closed_pnls=[Decimal(str(value)) for value in financial["closed_pnls"]]
        converted={**common,"book":MakerOrderBook(ledger).dump()}
    else:
        converted={**common,**financial,"positions":{},"pending":[]}
    return converted,snapshot


def _archive_executor(con,account_id,snapshot,target):
    if snapshot is None:
        return
    encoded=db.dump(snapshot);digest=hashlib.sha256(encoded.encode()).hexdigest()
    con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=id",(db.uid(),account_id,"executor_archive",f"executor:{account_id}:{digest}",encoded,db.now()))
    db.audit(con,None,"instance.model_change","account",account_id,{"from":snapshot.get("model","market_next_open_v1"),"to":target,"archived_sha256":digest})


def _account_limits():
    """Per-strategy memory ceilings; no cumulative CPU cap on long-lived actors."""
    if sys.platform.startswith("linux"):
        import resource
        memory=int(os.getenv("MK_ACCOUNT_MEMORY_MB","384"))*1024*1024
        resource.setrlimit(resource.RLIMIT_AS,(memory,memory))


@contextmanager
def strategy_deadline(seconds=10):
    """Abort a stuck trusted strategy call without waiting for lease expiry."""
    if not hasattr(signal,"SIGALRM"):
        yield
        return
    def expired(signum,frame):
        raise TimeoutError("策略单次决策超过 10 秒，已停止该账户运行")
    previous=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous)


def _runtime_frames(manifest, symbols, histories, timestamp, saved=None):
    """Warm up only indicators; never replay historical orders on a live actor.

    Completed higher-period history survives the 1000-minute shared cache
    window. Missing minutes invalidate incomplete buckets rather than creating
    a synthetic continuous candle. Only the newest minute may trigger a call.
    """
    from .timeframes import MinuteFrames, normalize_timeframes
    frames=MinuteFrames.load(saved) if saved else MinuteFrames(symbols,manifest.get("timeframes"),manifest["history_window"])
    if frames.symbols!=sorted(symbols) or frames.timeframes!=normalize_timeframes(manifest.get("timeframes")) or frames.history_window!=manifest["history_window"]:
        frames=MinuteFrames(symbols,manifest.get("timeframes"),manifest["history_window"])
    indexed={symbol:{bar["timestamp"]:bar for bar in histories[symbol] if bar["timestamp"]<=timestamp} for symbol in symbols}
    common=sorted(set.intersection(*(set(rows) for rows in indexed.values())))
    ready=False
    for stamp in common:
        if frames.last_timestamp is not None and stamp<=frames.last_timestamp:
            continue
        closed=frames.push({symbol:indexed[symbol][stamp] for symbol in symbols},allow_gap=True)
        ready=closed and stamp==timestamp
    return frames,ready


def maker_runtime_tick(account, instance, commands, strategy, snapshot, fence, current_lease):
    """Advance a persisted Maker book; every mutation commits under one fence.

    Missing candles never fill orders. Pausing cancels entries only; flattening
    remains an in-progress command until its post-only exits really fill.
    """
    from .engine import Ledger, MakerOrderBook, calculate_metrics
    from .strategy import load_strategy, validate_parameters
    account_id=account["id"]
    manifest=db.parse(strategy["manifest"],{})
    if not strategy["trusted"] or manifest["market"]!="futures" or "cross" not in manifest["account"]["margin_modes"]:
        raise ValueError("Maker 模拟执行需要可信合约策略和全仓模型")
    snapshot,archived=_convert_flat_snapshot(account,manifest,snapshot,"maker_grid_v1")
    revision={"strategy_id":account["strategy_id"],"source_hash":strategy["source_hash"],"config_revision":instance["config_revision"]}
    command=commands[0] if commands else None
    now_ms=int(time.time()*1000)
    symbols=db.parse(account["symbols"],[])
    with db.connect() as con:
        cached=[con.execute("SELECT * FROM market_cache WHERE market='futures' AND symbol=? AND interval='1m'",(symbol,)).fetchone() for symbol in symbols]
    fresh=bool(cached and all(row and time.time()-row["fetched_at"]<90 for row in cached))
    histories={symbol:db.parse(row["candles"],[]) for symbol,row in zip(symbols,cached) if row}
    common=set.intersection(*(set(bar["timestamp"] for bar in rows) for rows in histories.values())) if len(histories)==len(symbols) and histories else set()
    timestamp=max(common) if common else None
    closed_before=int(time.time()//60)*60000
    fresh=bool(fresh and timestamp is not None and timestamp<closed_before and 0<now_ms-timestamp<=150000)
    market_state={"timestamp":timestamp,"fresh":fresh}
    unchanged_command=not command or (command["status"]=="running" and snapshot.get("last_command_id")==command["id"])
    if archived is None and snapshot.get("revision")==revision and snapshot.get("market_state")==market_state and snapshot.get("desired_state")==instance["desired_state"] and snapshot.get("observed_state")==instance["observed_state"] and unchanged_command and instance["observed_state"]!="stopped":
        # Most actor polls have no new candle. Do not reconstruct or reserialize
        # the complete order history merely to confirm the same heartbeat.
        with db.connect() as con:
            if current_lease(con):
                con.execute("UPDATE instances SET last_heartbeat=? WHERE account_id=?",(db.now(),account_id))
        return True
    if snapshot:
        book=MakerOrderBook.load(snapshot["book"])
    else:
        book=MakerOrderBook(Ledger(account["initial_equity"],"futures",configured_leverage(account,manifest),Decimal(".0002"),position_mode=resolve_position_mode(manifest)))
    ledger=book.ledger
    previous_orders={key:db.dump(value) for key,value in book.orders.items()}
    fill_start=len(ledger.orders); event_start=len(book.events)
    revision_changed=snapshot.get("revision")!=revision
    execution_id=snapshot.get("execution_id") or db.uid()
    strategy_state=copy.deepcopy(snapshot.get("strategy_state",{}))
    strategy_error=snapshot.get("strategy_error")
    previous_strategy_error=strategy_error
    strategy_cursor=snapshot.get("strategy_event_cursor",0)
    bar_index=snapshot.get("bar_index",0)
    frame_state=snapshot.get("frames")
    if revision_changed:
        if snapshot and (book.has_open_positions() or book.open_orders()):
            raise ValueError("Maker 持仓或挂单未清空，不能用新配置覆盖运行状态")
        strategy_state={};strategy_error=None;strategy_cursor=len(book.events);bar_index=0;frame_state=None
        ledger.leverage=Decimal(str(configured_leverage(account,manifest)))
        ledger.fee_rate=Decimal(".0002")
        if snapshot.get("revision"):
            archived=archived or snapshot
        book=MakerOrderBook(ledger,book.symbol_filters,maker_fee_rate=Decimal(".0002"))
        previous_orders={};event_start=0;strategy_cursor=0;execution_id=db.uid()
    mode=instance["desired_state"]
    if command:
        mode={"pause":"paused","flatten":"stopped","resume":"running"}[command["action"]]
        if command["action"]=="resume":
            strategy_error=None;book.flattening=False
        elif command["action"]=="flatten":
            book.flattening=True
    if mode!="running" or strategy_error:
        book.cancel_entries(now_ms,"停止新增风险，保留已有 lot 的退出单")
    # A stopped actor with remaining positions still manages protection. A
    # failed strategy is never interpreted as an authorization for market exits.
    if mode=="stopped" and book.has_open_positions() and not book.flattening:
        mode="paused"
        strategy_error=strategy_error or "账户已停止新增风险，但仍有持仓；继续处理已有保护单"
    last=snapshot.get("last_timestamp",0)
    new_bar=bool(fresh and timestamp>last)
    risk=[];buffered_logs=[];display=copy.deepcopy(snapshot.get("display",{}))
    data_error=None
    if not fresh:
        book.cancel_entries(now_ms,"共享行情缺失或过期，撤销开仓挂单；保留退出单")
        data_error="等待完整且新鲜的多币种 K 线；未新增挂单或模拟成交，现有 lot 仍保留"
    elif timestamp<last:
        data_error="行情时间早于已持久化账本，拒绝倒序撮合"
        fresh=False;new_bar=False
        book.cancel_entries(now_ms,data_error)
    bars={symbol:next(bar for bar in reversed(rows) if bar["timestamp"]==timestamp) for symbol,rows in histories.items()} if fresh else {}
    if fresh:
        book.symbol_filters={symbol:db.parse(row["filters"],{}) for symbol,row in zip(symbols,cached)}
    if new_bar:
        if last and timestamp-last>60000:
            book.cancel_entries(timestamp,"行情断档，过期开仓挂单作废")
            risk.append({"status":"warning","reason":"行情断档：缺失期间的成交先后与止损触发无法重建，仅处理当前完整 K 线上的保护单"})
        book.process_bar(bars)
    primary_ready=False
    frame_context={}
    if new_bar:
        frames,primary_ready=_runtime_frames(manifest,symbols,histories,timestamp,frame_state)
        frame_state=frames.dump()
        if primary_ready:
            frame_context=frames.context()

    decision_called=False
    references={symbol:bar["close"] for symbol,bar in bars.items()}
    if book.flattening:
        if fresh:
            book.request_flatten(timestamp,references)
        mode="stopped"
    elif new_bar and primary_ready and mode=="running" and not strategy_error:
        try:
            strategy_obj=load_strategy(strategy["source"])
            strategy_obj.__dict__.update(copy.deepcopy(strategy_state))
            def strategy_log(level,message,data=None):
                if len(buffered_logs)<1000:
                    buffered_logs.append(("error" if level=="error" else "debug",str(message)[:4000],data))
            context={**frame_context,"params":validate_parameters(manifest,db.parse(account["config"],{})),
                     "positions":ledger.position_snapshot(),"account":ledger.account(),"timestamp":timestamp,"bar_index":bar_index,
                     "open_orders":book.open_orders(),"lots":book.lot_snapshot(),"execution_events":copy.deepcopy(book.events[strategy_cursor:]),
                     "log":strategy_log,"emit":lambda **fields:display.update(fields)}
            strategy_cursor=len(book.events)
            with strategy_deadline():
                decision_called=True
                intents=strategy_obj.on_bar(context)
            next_state=copy.deepcopy(strategy_obj.__dict__);db.dump(next_state)
            if not isinstance(intents,list) or len(intents)>1000 or any(not isinstance(intent,dict) for intent in intents):
                raise ValueError("策略必须返回有限的委托意图列表")
            submitted=book.submit_many(intents,timestamp,references)
            rejected=[{"status":"rejected","order_id":result.get("id",result.get("client_order_id")),"reason":str(result.get("reason","委托未通过风险或精度检查"))[:1000]} for result in submitted if result.get("status")=="REJECTED"]
            if rejected:
                risk.extend(rejected[:20])
                buffered_logs.append(("error","Maker 委托未被接受；未记作成交",{"rejected_count":len(rejected),"orders":rejected[:20]}))
            strategy_state=next_state
        except Exception as exc:
            strategy_error=str(exc)[:2000]
            book.cancel_entries(timestamp,"策略异常：停止开仓，已有保护单继续运行")
            mode="paused"
            buffered_logs.append(("error","Maker 策略异常；已有 lot 和保护单继续管理",{"error":strategy_error}))
    lots=book.lot_snapshot()
    if not book.flattening and any(lot.get("protection_status") in {"stop_rejected","unprotected"} for lot in lots):
        strategy_error=strategy_error or "部分 lot 的止损被拒绝或保护不完整；已暂停新开仓，无市价回退"
        book.cancel_entries(timestamp or now_ms,"已有 lot 保护异常，暂停新增风险")
        mode="paused"
    positions=[dict(value,direction="long" if value["position_side"]=="LONG" else "short") for value in ledger.position_snapshot().values()]
    open_orders=book.open_orders()
    stopped=mode=="stopped" and not book.has_open_positions() and not open_orders
    observed="stopped" if stopped else "flattening" if book.flattening else "error" if strategy_error else "starting" if not fresh else mode
    error=strategy_error or (None if stopped else data_error)
    curve=list(snapshot.get("curve",[]))
    if new_bar:
        point={"timestamp":timestamp,"equity":float(ledger.equity()),"margin_used":float(ledger.margin()),"unrealized_pnl":float(ledger.unrealized()),"pending_margin":float(ledger.pending_margin)}
        curve.append(point)
    stats=ui_metrics(calculate_metrics(curve,ledger.orders,account["initial_equity"],closed_pnls=ledger.closed_pnls,account=ledger.account()))
    metric_time=timestamp if fresh else last or now_ms
    day_start,month_start=_period_starts(metric_time)
    def period_pnl(start):
        baseline=next((point["equity"] for point in reversed(curve) if point["timestamp"]<start),account["initial_equity"])
        return float(ledger.equity())-baseline
    previous_metrics=db.parse(account["metrics"],{})
    metrics={**ledger.account(),**stats,"today_pnl":period_pnl(day_start),"month_pnl":period_pnl(month_start),
             "running_days":max(1,int((metric_time-curve[0]["timestamp"])/86400000)+1) if curve else 0,
             "return_pct":stats["total_return_pct"],"max_drawdown_u":stats["max_drawdown_amount"],"position_count":len(lots),
             "long_count":sum(lot["position_side"]=="LONG" for lot in lots),"short_count":sum(lot["position_side"]=="SHORT" for lot in lots),
             "curve":curve,"positions":positions,"orders":ledger.orders,"lots":lots,"open_orders":open_orders,"open_order_count":len(open_orders),
             "execution_events":copy.deepcopy(book.events[-100:]),"display":display,"risk":risk,"strategy_error":strategy_error,
             "data_updated_at":db.now() if fresh else previous_metrics.get("data_updated_at"),
             "execution_model":{"model":"maker_grid_v1","fee_rate":float(book.maker_fee_rate),"maker_fee_rate":float(book.maker_fee_rate),
                 "slippage_bps":0,"fee_source":"documented_simulation_assumption","interval":"1m","timeframes":manifest.get("timeframes",{"primary":"1m","additional":[]}),"position_mode":ledger.position_mode,
                 "funding_included":False,"liquidation_simulated":False,"liquidity":"simulated_maker","order_book_queue_simulated":False}}
    snapshot={"model":"maker_grid_v1","book":book.dump(),"strategy_state":strategy_state,"strategy_error":strategy_error,
              "strategy_event_cursor":strategy_cursor,"revision":revision,"last_timestamp":timestamp if new_bar else last,
              "bar_index":bar_index+int(decision_called),"frames":frame_state,"curve":curve,"display":display,"market_state":market_state,
              "execution_id":execution_id,"last_command_id":command["id"] if command else None,"observed_state":observed,
              "desired_state":"paused" if strategy_error and not book.flattening else instance["desired_state"]}
    changed_orders=[dict(order,execution_id=execution_id) for key,order in book.orders.items() if previous_orders.get(key)!=db.dump(order)]
    new_fills=[dict(fill,execution_id=execution_id) for fill in ledger.orders[fill_start:]]
    command_status=None
    if command:
        command_status="running" if (command["action"]=="flatten" and not stopped) or (command["action"]=="resume" and not fresh) else "failed" if command["action"]=="resume" and strategy_error else "completed"
    with db.connect() as con:
        if not current_lease(con):
            return True
        _archive_executor(con,account_id,archived,"maker_grid_v1")
        db.persist_maker_activity(con,account_id,changed_orders,new_fills)
        if new_bar:
            con.execute("INSERT IGNORE INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),account_id,"valuation",f"valuation:{account_id}:{timestamp}",db.dump(curve[-1]),db.now()))
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload),created_at=VALUES(created_at)",(db.uid(),account_id,"snapshot",f"snapshot:{account_id}",db.dump(snapshot),db.now()))
        con.execute("UPDATE accounts SET metrics=? WHERE id=?",(db.dump(metrics),account_id))
        con.execute("UPDATE instances SET observed_state=?,last_heartbeat=?,error=? WHERE account_id=?",(observed,db.now(),error,account_id))
        if strategy_error and not book.flattening:
            con.execute("UPDATE instances SET desired_state='paused' WHERE account_id=?",(account_id,))
        if book.events[event_start:] or new_bar or command:
            con.execute("INSERT INTO runtime_events(account_id,fence,event,payload,created_at) VALUES(?,?,?,?,?)",(account_id,fence,"maker_tick",db.dump({"timestamp":timestamp,"mode":observed,"events":book.events[event_start:],"risk":risk}),db.now()))
        for level,message,details in buffered_logs:
            con.execute("INSERT INTO logs(level,strategy_id,account_id,message,details,source,created_at) VALUES(?,?,?,?,?,'strategy',?)",(level,account["strategy_id"],account_id,message,db.dump(details),db.now()))
        if strategy_error and strategy_error!=previous_strategy_error:
            db.audit(con,None,"instance.maker_risk","account",account_id,{"error":strategy_error,"fence":fence,"open_lots":len(lots)})
        if command:
            result={"observed_state":observed,"fence":fence,"open_lots":len(lots),"open_orders":len(open_orders),"model":"maker_grid_v1"}
            con.execute("UPDATE commands SET status=?,completed_at=?,error=?,result=? WHERE id=?",(command_status,None if command_status=="running" else db.now(),strategy_error if command_status=="failed" else None,db.dump(result),command["id"]))
            if command["status"]!=command_status:
                db.audit(con,None,"command."+command_status,"command",command["id"],{"account_id":account_id,**result})
    return not stopped or len(commands)>1


def runtime_tick(account_id,owner):
    """One transactionally fenced paper step. Returns True while actor is needed."""
    from .engine import _filters_for,calculate_metrics
    from .strategy import load_strategy,validate_parameters
    ensure_market_cache()
    fence=db.acquire_lease(account_id,owner)
    if fence is None:
        return True
    with db.connect() as con:
        account=con.execute("SELECT a.*,e.leverage AS configured_leverage FROM accounts a LEFT JOIN account_execution_settings e ON e.account_id=a.id WHERE a.id=?",(account_id,)).fetchone()
        instance=con.execute("SELECT * FROM instances WHERE account_id=?",(account_id,)).fetchone()
        commands=con.execute("SELECT * FROM commands WHERE account_id=? AND status IN ('pending','running') ORDER BY created_at",(account_id,)).fetchall()
        strategy=con.execute("SELECT * FROM strategies WHERE id=?",(account["strategy_id"],)).fetchone()
        saved=con.execute("SELECT payload FROM ledger WHERE account_id=? AND kind='snapshot' ORDER BY created_at DESC LIMIT 1",(account_id,)).fetchone()
    def current_lease(con):
        lease=db.lock_instance(con,account_id)
        return bool(lease and lease["owner"]==owner and lease["fence"]==fence
                    and lease["lease_until"]>time.time()
                    and lease["desired_state"]==instance["desired_state"]
                    and lease["config_revision"]==instance["config_revision"])
    if account["mode"]=="live" or account["source"]=="demo":
        with db.connect() as con:
            if not current_lease(con):
                return True
            for command in commands:
                con.execute("UPDATE commands SET status='failed',error=?,completed_at=? WHERE id=?",("演示账户或实盘执行闸门关闭；未向交易所下单",db.now(),command["id"]))
                db.audit(con,None,"command.fail","command",command["id"],{"account_id":account_id,"reason":"execution_gate_closed","fence":fence})
            con.execute("UPDATE instances SET observed_state='stopped',desired_state='stopped',error=? WHERE account_id=?",("执行闸门关闭",account_id))
        return False
    manifest=db.parse(strategy["manifest"],{})
    if manifest.get("execution",{}).get("model")=="maker_grid_v1":
        return maker_runtime_tick(account,instance,commands,strategy,db.parse(saved["payload"],{}) if saved else {},fence,current_lease)
    if not strategy["trusted"]:
        raise ValueError("运行策略未经审核")
    if manifest["market"]=="futures" and "cross" not in manifest["account"]["margin_modes"]:
        raise ValueError("当前模拟账本仅实现全仓模型，不能运行仅支持逐仓的策略")
    snapshot,archived=_convert_flat_snapshot(account,manifest,db.parse(saved["payload"],{}) if saved else {},"market_next_open_v1")
    ledger=_restore_ledger(account,manifest,snapshot)
    paper_costs=_paper_execution_costs(manifest,db.parse(account["metrics"],{}))
    paper_slippage=Decimal(str(paper_costs["slippage_bps"]))/Decimal("10000")
    revision={"strategy_id":account["strategy_id"],"source_hash":strategy["source_hash"],"config_revision":instance["config_revision"]}
    if snapshot.get("revision")!=revision:
        snapshot.update(pending=[],strategy_state={},bar_index=0,frames=None)
    symbols=db.parse(account["symbols"],[])
    mode=instance["desired_state"]
    # The durable command order defines transitions; a stale frontend cannot erase them.
    if commands:
        mode={"pause":"paused","flatten":"stopped","resume":"running"}[commands[0]["action"]]
    with db.connect() as con:
        cached=[con.execute("SELECT * FROM market_cache WHERE market=? AND symbol=? AND interval='1m'",(manifest["market"],s)).fetchone() for s in symbols]
    fresh=bool(cached and all(c and time.time()-c["fetched_at"]<90 for c in cached))
    if not fresh:
        if mode=="stopped" and not any(p.quantity for p in ledger.positions.values()):
            with db.connect() as con:
                if not current_lease(con):
                    return True
                if commands:
                    con.execute("UPDATE commands SET status='completed',completed_at=?,result=? WHERE id=?",(db.now(),db.dump({"message":"无持仓，已停止"}),commands[0]["id"]))
                    db.audit(con,None,"command.complete","command",commands[0]["id"],{"account_id":account_id,"observed_state":"stopped","fence":fence})
                con.execute("UPDATE instances SET observed_state='stopped',last_heartbeat=?,error=NULL WHERE account_id=?",(db.now(),account_id))
            return False
        with db.connect() as con:
            if not current_lease(con):
                return True
            con.execute("UPDATE instances SET observed_state='starting',error='等待新鲜的共享行情，禁止开仓',last_heartbeat=? WHERE account_id=?",(db.now(),account_id))
        return True
    histories={s:db.parse(c["candles"],[]) for s,c in zip(symbols,cached)}
    common=set.intersection(*(set(c["timestamp"] for c in rows) for rows in histories.values()))
    if not common:
        return True
    timestamp=max(common)
    if int(time.time()*1000)-timestamp>150000 or timestamp>=int(time.time()//60)*60000 or timestamp<snapshot.get("last_timestamp",0):
        with db.connect() as con:
            if not current_lease(con):
                return True
            con.execute("UPDATE instances SET observed_state='starting',error='最后一根完整 K 线已过期，禁止开仓',last_heartbeat=? WHERE account_id=? AND owner=?",(db.now(),account_id,owner))
        return True
    bars={s:next(c for c in reversed(rows) if c["timestamp"]==timestamp) for s,rows in histories.items()}
    filters={s:_filters_for(s,{s:db.parse(c["filters"],{})}) for s,c in zip(symbols,cached)}
    pending=snapshot.get("pending",[])
    last=snapshot.get("last_timestamp",0)
    old_order_count=len(ledger.orders)
    if timestamp==last and not commands and archived is None:
        with db.connect() as con:
            if not current_lease(con):
                return True
            con.execute("UPDATE instances SET last_heartbeat=?,observed_state=?,error=NULL WHERE account_id=? AND owner=? AND fence=?",(db.now(),mode,account_id,owner,fence))
        return mode!="stopped"
    risk=[]
    if timestamp>last and last:
        ledger.prices={s:Decimal(str(bar["open"])) for s,bar in bars.items()}
        for intent in pending:
            if (mode!="running" or instance["desired_state"]!="running") and not intent.get("reduce_only"):
                risk.append({"status":"rejected","reason":"账户已停止开仓"});continue
            if timestamp-last>60000:
                risk.append({"status":"rejected","reason":"行情断档，过期交易意图作废"});continue
            reason=ledger.execute(intent,timestamp,filters,paper_slippage,last,execution_bar=bars.get(intent.get("symbol")))
            risk.append({"status":"rejected" if reason else "accepted","reason":reason or "模拟撮合通过"})
        # A primary-period signal is one-shot. Waiting for the next higher
        # candle must not replay it at every intervening minute.
        pending=[]
    ledger.prices={s:Decimal(str(bar["close"])) for s,bar in bars.items()}
    if mode=="stopped":
        for key,position in list(ledger.positions.items()):
            symbol=key.split(":")[0]
            if position.quantity:
                reason=ledger.execute({"symbol":symbol,"position_side":"LONG" if position.quantity>0 else "SHORT","side":"sell" if position.quantity>0 else "buy","quantity":float(abs(position.quantity)),"reduce_only":True,"reason":"管理员平仓并停止"},timestamp,filters,paper_slippage,timestamp,execution_bar=bars[symbol])
                if reason:
                    raise ValueError(f"模拟平仓失败：{reason}")
        pending=[]
    display=copy.deepcopy(db.parse(account["metrics"],{}).get("display",{}))
    strategy_state=snapshot.get("strategy_state",{})
    frame_state=snapshot.get("frames")
    primary_ready=False
    frame_context={}
    if timestamp>last:
        frames,primary_ready=_runtime_frames(manifest,symbols,histories,timestamp,frame_state)
        frame_state=frames.dump()
        if primary_ready:
            frame_context=frames.context()

    decision_called=False
    if timestamp>last and primary_ready and mode in {"running","paused"}:
        strategy_obj=load_strategy(strategy["source"])
        strategy_obj.__dict__.update(copy.deepcopy(strategy_state))
        def strategy_log(level,message,data=None):
            db.log(level,message,strategy_id=account["strategy_id"],account_id=account_id,details=data)
        context={**frame_context,"params":validate_parameters(manifest,db.parse(account["config"],{})),"positions":ledger.position_snapshot(),"account":ledger.account(),"timestamp":timestamp,"bar_index":snapshot.get("bar_index",0),"log":strategy_log,"emit":lambda **fields:display.update(fields)}
        with strategy_deadline():
            decision_called=True
            pending=strategy_obj.on_bar(context)
        strategy_state=copy.deepcopy(strategy_obj.__dict__)
        db.dump(strategy_state)  # State is durable JSON; unsupported state fails closed.
        if not isinstance(pending,list) or len(pending)>1000 or any(not isinstance(i,dict) for i in pending):
            raise ValueError("策略必须返回有限的交易意图列表")
        if mode=="paused":
            pending=[i for i in pending if i.get("reduce_only")]
    curve=snapshot.get("curve",[])
    if timestamp>last or (mode=="stopped" and len(ledger.orders)>old_order_count):
        curve.append({"timestamp":timestamp,"equity":float(ledger.equity()),"margin_used":float(ledger.margin()),"unrealized_pnl":float(ledger.unrealized())})
    stats=ui_metrics(calculate_metrics(curve,ledger.orders,account["initial_equity"],closed_pnls=ledger.closed_pnls,account=ledger.account()))
    positions=[dict(v,direction="long" if v["quantity"]>0 else "short") for s,v in ledger.position_snapshot().items()]
    day_start,month_start=_period_starts(timestamp)
    def period_pnl(start):
        baseline=next((p["equity"] for p in reversed(curve) if p["timestamp"]<start),account["initial_equity"])
        return float(ledger.equity())-baseline
    metrics={"today_pnl":period_pnl(day_start),"month_pnl":period_pnl(month_start),"running_days":max(1,int((timestamp-curve[0]["timestamp"])/86400000)+1) if curve else 0,**ledger.account(),**stats,"return_pct":stats["total_return_pct"],"max_drawdown_u":stats["max_drawdown_amount"],"position_count":len(positions),"long_count":sum(p["quantity"]>0 for p in positions),"short_count":sum(p["quantity"]<0 for p in positions),"curve":curve,"positions":positions,"orders":ledger.orders,"display":display,"data_updated_at":db.now(),"risk":risk,"execution_model":{"model":"market_next_open_v1","position_mode":ledger.position_mode,"fee_rate":float(ledger.fee_rate),"slippage_bps":paper_costs["slippage_bps"],"fee_source":paper_costs["fee_source"],"interval":"1m","funding_included":False,"liquidation_simulated":False}}
    metrics["execution_model"]["timeframes"]=manifest.get("timeframes",{"primary":"1m","additional":[]})
    snapshot={"model":"market_next_open_v1","position_mode":ledger.position_mode,"balance":str(ledger.balance),"fees":str(ledger.fees),"positions":{s:{"quantity":str(p.quantity),"average_price":str(p.average_price),"entry_fees":str(p.entry_fees)} for s,p in ledger.positions.items()},"orders":ledger.orders,"closed_pnls":[str(v) for v in ledger.closed_pnls],"pending":pending,"strategy_state":strategy_state,"revision":revision,"last_timestamp":timestamp,"bar_index":snapshot.get("bar_index",0)+int(decision_called),"frames":frame_state,"curve":curve}
    with db.connect() as con:
        if not current_lease(con):
            return True
        _archive_executor(con,account_id,archived,"market_next_open_v1")
        if decision_called:
            con.execute("INSERT INTO logs(level,strategy_id,account_id,message,details,source,created_at) VALUES(?,?,?,?,?,?,?)",("debug",account["strategy_id"],account_id,(str(display.get("stage") or "策略决策")+"："+str(display.get("reason") or "已处理完整主周期 K 线"))[:4000],db.dump({"timestamp":timestamp,"display":display,"intent_count":len(pending),"risk":risk}),"runtime",db.now()))
        for order in ledger.orders[old_order_count:]:
            db.persist_fill(con,account_id,order)
            con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),account_id,"fill",f"{account_id}:{order['id']}",db.dump(order),db.now()))
        if timestamp>last:
            con.execute("INSERT IGNORE INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),account_id,"valuation",f"valuation:{account_id}:{timestamp}",db.dump(curve[-1]),db.now()))
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload),created_at=VALUES(created_at)",(db.uid(),account_id,"snapshot",f"snapshot:{account_id}",db.dump(snapshot),db.now()))
        con.execute("UPDATE accounts SET metrics=? WHERE id=?",(db.dump(metrics),account_id))
        con.execute("UPDATE instances SET observed_state=?,last_heartbeat=?,error=NULL WHERE account_id=?",(mode,db.now(),account_id))
        con.execute("INSERT INTO runtime_events(account_id,fence,event,payload,created_at) VALUES(?,?,?,?,?)",(account_id,fence,"paper_tick",db.dump({"timestamp":timestamp,"mode":mode,"risk":risk}),db.now()))
        if commands:
            con.execute("UPDATE commands SET status='completed',completed_at=?,result=? WHERE id=?",(db.now(),db.dump({"observed_state":mode,"fence":fence}),commands[0]["id"]))
            db.audit(con,None,"command.complete","command",commands[0]["id"],{"account_id":account_id,"observed_state":mode,"fence":fence})
    return mode!="stopped" or len(commands)>1


def account_loop(account_id,once=False):
    owner=db.uid()
    while True:
        try:
            keep_running=retry_database(lambda:runtime_tick(account_id,owner),f"account:{account_id}",once=once)
        except Exception as exc:
            message=str(exc)[:2000]
            failure_fence=None
            def record_failure():
                nonlocal failure_fence
                with db.connect() as con:
                    lease=db.lock_instance(con,account_id)
                    # The actor may have lost its lease while MySQL was down.
                    # Stop only if this is still its ownership epoch; never stop
                    # a replacement actor. An expired lease with the SAME owner
                    # cannot be used to execute, but can safely fail closed here.
                    if not lease or lease["owner"]!=owner:
                        return
                    if failure_fence is None:
                        failure_fence=lease["fence"]
                    if lease["fence"]!=failure_fence:
                        return
                    # A committed failure whose acknowledgement was lost must
                    # not create duplicate audit/log entries on reconnect.
                    if lease["desired_state"]=="stopped" and lease["observed_state"]=="error" and lease["error"]==message:
                        return
                    con.execute("UPDATE instances SET observed_state='error',desired_state='stopped',error=? WHERE account_id=? AND owner=? AND fence=?",(message,account_id,owner,failure_fence))
                    con.execute("UPDATE commands SET status='failed',completed_at=?,error=? WHERE account_id=? AND status IN ('pending','running')",(db.now(),message,account_id))
                    db.audit(con,None,"instance.fail","account",account_id,{"error":message,"fence":failure_fence})
                    con.execute("INSERT INTO logs(level,account_id,message,details,source,created_at) VALUES('error',?,?,?,?,?)",(account_id,"模拟运行进程异常，停止继续开仓",db.dump({"error":message}),"runtime",db.now()))
            retry_database(record_failure,f"account-failure:{account_id}",once=once)
            return
        if once or not keep_running:
            return
        time.sleep(2)


def account_heartbeat_expired(instance, started_at, *, now=None, monotonic_now=None):
    """Require both stale heartbeat and expired lease, after startup grace."""
    from datetime import datetime
    now=time.time() if now is None else now
    monotonic_now=time.monotonic() if monotonic_now is None else monotonic_now
    if monotonic_now-started_at<45 or instance["lease_until"]>=now-15:
        return False
    if instance.get("last_heartbeat"):
        try:
            if now-datetime.fromisoformat(instance["last_heartbeat"]).timestamp()<=45:
                return False
        except (TypeError,ValueError):
            pass
    return True


def stop_unresponsive_account(account_id, child, started_at):
    # Serialize the timeout decision with executor renewal and warmup commit;
    # an unlocked stale SELECT must never kill a freshly renewed executor.
    with db.connect() as con:
        instance=db.lock_instance(con,account_id)
        if not instance or child.poll() is not None or not account_heartbeat_expired(instance,started_at):
            return False
        child.kill();child.wait(timeout=5)
        message="策略心跳超时，执行进程已隔离停止，需人工检查后恢复"
        con.execute("UPDATE instances SET desired_state='stopped',observed_state='error',owner=NULL,fence=fence+1,lease_until=0,error=? WHERE account_id=?",(message,account_id))
        con.execute("UPDATE commands SET status='failed',completed_at=?,error='策略心跳超时' WHERE account_id=? AND status IN ('pending','running')",(db.now(),account_id))
        details={"last_heartbeat":instance["last_heartbeat"],"lease_until":instance["lease_until"],"fence":instance["fence"]}
        db.audit(con,None,"instance.heartbeat_timeout","account",account_id,details)
        con.execute("INSERT INTO logs(level,account_id,message,details,source,created_at) VALUES('error',?,?,?,?,?)",(account_id,message,db.dump(details),"runtime",db.now()))
    return True


def runtime_loop(once=False):
    owner=db.uid();children={};started_at={}
    ensure_market_cache()
    market=None if once else subprocess.Popen([sys.executable,"-m","backend.worker","market"])
    warmer=None if once else subprocess.Popen([sys.executable,"-m","backend.worker","warmup"],preexec_fn=_account_limits if os.name=="posix" else None)
    try:
        database_failures=0
        while True:
            try:
                service("runtime",owner)
                if database_failures:
                    # Database recovery is not evidence that an actor is hung.
                    # Give actors time to reconnect/reload before the watchdog runs.
                    for account_id in started_at:
                        started_at[account_id]=time.monotonic()
                    database_failures=0
                for account_id,child in list(children.items()):
                    if child.poll() is None:
                        stop_unresponsive_account(account_id,child,started_at[account_id])
                with db.connect() as con:
                    rows=con.execute("SELECT DISTINCT i.account_id FROM instances i JOIN accounts a ON a.id=i.account_id LEFT JOIN commands c ON c.account_id=i.account_id WHERE i.desired_state IN ('running','paused') OR i.observed_state='flattening' OR c.status IN ('pending','running') OR (JSON_UNQUOTE(JSON_EXTRACT(a.metrics,'$.execution_model.model'))='maker_grid_v1' AND (COALESCE(JSON_EXTRACT(a.metrics,'$.position_count'),0)>0 OR COALESCE(JSON_EXTRACT(a.metrics,'$.open_order_count'),0)>0))").fetchall()
                for row in rows:
                    id=row["account_id"]
                    if id not in children or children[id].poll() is not None:
                        children[id]=subprocess.Popen([sys.executable,"-m","backend.worker","account",id]+(["--once"] if once else []),preexec_fn=_account_limits if os.name=="posix" else None)
                        started_at[id]=time.monotonic()
                if market and market.poll() is not None:
                    market=subprocess.Popen([sys.executable,"-m","backend.worker","market"])
                if warmer and warmer.poll() is not None:
                    warmer=subprocess.Popen([sys.executable,"-m","backend.worker","warmup"],preexec_fn=_account_limits if os.name=="posix" else None)
                if once:
                    for child in children.values():
                        child.wait(timeout=20)
                    return
                time.sleep(2)
            except Exception as exc:
                if once or not transient_database_error(exc):
                    raise
                database_failures+=1
                _database_retry_wait("runtime",exc,database_failures)
    finally:
        # Web restarts never execute this. A supervisor stop deliberately stops actors.
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        if market and market.poll() is None:
            market.terminate()
        if warmer and warmer.poll() is None:
            warmer.terminate()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("backtest","runtime","market","warmup","job","download-job","account","init-demo","create-admin","trust-strategy"))
    parser.add_argument("id",nargs="?")
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--username", help="管理员用户名；不提供则交互输入")
    parser.add_argument("--generate-password", action="store_true", help="生成高强度密码并仅在创建成功时输出一次 JSON")
    parser.add_argument("--source-hash")
    parser.add_argument("--download-owner")
    parser.add_argument("--accept-trusted-code",action="store_true")
    args=parser.parse_args()
    retry_database(db.init_db,"startup:"+args.mode,once=args.once or args.mode not in {"runtime","backtest","market","warmup","account"})
    if args.mode in {"runtime","backtest"}:
        signal.signal(signal.SIGTERM,lambda signum,frame:sys.exit(0))
    if args.mode in {"job","download-job","account","market","warmup"}:
        os.environ.pop("MK_MASTER_KEY",None)
        os.environ.pop("MK_ENABLE_LIVE",None)
    if args.mode=="init-demo":
        import secrets
        if args.generate_password:
            os.environ["MK_DEMO_PASSWORD"] = secrets.token_urlsafe(24)
        try:
            created = db.seed_demo()
        except ValueError as exc:
            parser.error(str(exc))
        if created and args.generate_password:
            print(db.dump({"username":"admin","password":os.environ["MK_DEMO_PASSWORD"],"created":True,"demo":True}))
        else:
            print("演示数据已初始化，使用 MK_DEMO_PASSWORD 登录" if created else "管理员已存在，保留原密码和数据")
    elif args.mode=="trust-strategy":
        if not args.id or not args.source_hash or not args.accept_trusted_code:
            parser.error("必须提供策略 ID、--source-hash SHA256 和 --accept-trusted-code；仅批准已人工审查的可信 Python 源码")
        with db.connect() as con:
            row=con.execute("SELECT * FROM strategies WHERE id=?",(args.id,)).fetchone()
            if not row or row["source_hash"]!=args.source_hash or hashlib.sha256(row["source"].encode()).hexdigest()!=args.source_hash:
                parser.error("策略不存在或源码哈希不匹配，未启用")
            con.execute("UPDATE strategies SET trusted=1 WHERE id=?",(args.id,))
            db.audit(con,None,"strategy.trust","strategy",args.id,{"source_hash":args.source_hash})
        db.log("debug","通过本地 CLI 批准可信策略代码",strategy_id=args.id,details={"source_hash":args.source_hash})
        print("已启用该精确源码版本。进程资源限制不等于恶意代码沙箱；仅运行已审查的可信策略。")
    elif args.mode=="create-admin":
        import getpass
        from .security import hash_password
        import secrets
        username=(args.username or input("管理员登录名: ")).strip()
        if not username or len(username)>120:
            parser.error("管理员用户名长度必须为 1–120 字符")
        password=secrets.token_urlsafe(24) if args.generate_password else getpass.getpass("密码（至少 10 位）: ")
        try:
            with db.connect() as con:
                user_id=db.uid()
                con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",(user_id,username,username,None,hash_password(password),"admin",db.now()))
                db.audit(con,None,"user.create_admin","user",user_id,{})
        except (db.IntegrityError, ValueError) as exc:
            parser.error("管理员账号已存在，未覆盖原密码" if isinstance(exc,db.IntegrityError) else str(exc))
        db.log("debug","通过本地 CLI 创建管理员",details={"username":username})
        print(db.dump({"username":username,"password":password,"created":True}) if args.generate_password else "管理员已创建")
    elif args.mode=="backtest":backtest_loop(args.once)
    elif args.mode=="runtime":runtime_loop(args.once)
    elif args.mode=="market":market_loop(args.once)
    elif args.mode=="warmup":
        if hasattr(os,"nice"):os.nice(10)
        from .warmup import service_loop
        service_loop(args.once)
    elif args.mode=="job":run_experiment(args.id)
    elif args.mode=="download-job":
        if not args.id or not args.download_owner:
            parser.error("download-job 必须提供任务编号与 --download-owner")
        downloads.run(args.id,args.download_owner)
    elif args.mode=="account":account_loop(args.id,args.once)


if __name__=="__main__":
    main()
