"""Independent workers: python -m backend.worker backtest | runtime | init-demo.

Web shutdown never owns the lifetime of these workers. The backtest coordinator
forks a bounded subprocess per job; runtime isolates each account in a process.
Live order submission is intentionally gated until production acceptance.
"""
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

from . import db


def service(name,owner):
    with db.connect() as con:
        con.execute("INSERT INTO services VALUES(?,?,?,'online') ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat,status='online'",(name,owner,db.now()))


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
    with db.connect() as con:
        row=con.execute("SELECT * FROM experiments WHERE id=?",(id,)).fetchone()
        if not row or row["status"] not in {"queued","running"}:
            return
        strategy=con.execute("SELECT * FROM strategies WHERE id=?",(row["strategy_id"],)).fetchone()
        dataset=con.execute("SELECT * FROM datasets WHERE id=?",(row["dataset_id"],)).fetchone()
        con.execute("UPDATE experiments SET status='running',worker_pid=?,started_at=? WHERE id=?",(os.getpid(),db.now(),id))
    partial={"curve":[],"validation_curve":[],"metrics":{},"validation_metrics":{},"incomplete":True}
    try:
        if not strategy or not strategy["trusted"]:
            raise ValueError("自定义代码未经离线安全审核，禁止执行")
        config=db.parse(row["config"],{})
        manifest=db.parse(strategy["manifest"],{})
        candles=db.parse(dataset["candles"],{})
        ranges=config["ranges_ms"]
        random.seed(config["seed"])
        filters=None
        # Network independence keeps historical experiments reproducible. Rules are
        # supplied with the job when captured; fallback is exposed in assumptions.
        filters=config.get("symbol_filters")
        output={}
        def cancelled():
            with db.connect() as con:
                current=con.execute("SELECT cancel_requested FROM experiments WHERE id=?",(id,)).fetchone()
            return bool(current and current[0])
        for i,(split,start,end) in enumerate((("train",ranges[0],ranges[1]),("validation",ranges[2],ranges[3]))):
            data={s:[c for c in candles[s] if start<=c["timestamp"]<end] for s in config["symbols"]}
            curve_key="curve" if split=="train" else "validation_curve"
            def progress(event):
                partial[curve_key].extend(event.get("equity",[]))
                value=min(99.9,(i+event["processed"]/max(event["total"],1))*50)
                with db.connect() as con:
                    con.execute("UPDATE experiments SET progress=?,result=? WHERE id=?",(value,db.dump(partial),id))
            output[split]=run_backtest(strategy["source"],config["parameters"],data,config["initial_capital"],config["fee_rate"],config["slippage_bps"],market=config["market"],leverage=config.get("leverage",manifest["account"]["leverage_min"]),symbol_filters=filters,progress=progress,cancelled=cancelled)
            partial["metrics" if split=="train" else "validation_metrics"]=ui_metrics(output[split]["metrics"])
        if cancelled():
            raise BacktestCancelled("任务取消")
        result={"metrics":ui_metrics(output["train"]["metrics"]),"validation_metrics":ui_metrics(output["validation"]["metrics"]),"curve":output["train"]["equity"],"validation_curve":output["validation"]["equity"],"orders":[dict(o,split=split) for split,r in output.items() for o in r["orders"]],"decisions":[dict(o,split=split) for split,r in output.items() for o in r["decisions"]],"assumptions":output["train"]["assumptions"],"incomplete":False}
        result["differences"]={k:result["validation_metrics"][k]-v for k,v in result["metrics"].items() if isinstance(v,(int,float)) and isinstance(result["validation_metrics"].get(k),(int,float))}
        with db.connect() as con:
            con.execute("UPDATE experiments SET status='completed',progress=100,result=?,completed_at=?,error=NULL WHERE id=?",(db.dump(result),db.now(),id))
        db.log("debug","回测及验证集完成",strategy_id=row["strategy_id"],details={"experiment_id":id})
    except BacktestCancelled:
        with db.connect() as con:
            con.execute("UPDATE experiments SET status='cancelled',completed_at=?,error='用户取消；部分曲线不是最终结果' WHERE id=?",(db.now(),id))
    except Exception as exc:
        with db.connect() as con:
            con.execute("UPDATE experiments SET status='failed',completed_at=?,error=? WHERE id=?",(db.now(),str(exc)[:2000],id))
        db.log("error","回测失败",strategy_id=row["strategy_id"],details={"experiment_id":id,"error":str(exc)[:2000]})


def _child_limits():
    """Resource ceilings supplement process isolation; they are not a code sandbox."""
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(300,305))
    if sys.platform.startswith("linux"):
        memory=1024*1024*1024
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
        with db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT id FROM experiments WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
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
    while child.poll() is None:
        service("backtest",owner)
        with db.connect() as con:
            cancellation=con.execute("SELECT cancel_requested FROM experiments WHERE id=?",(job_id,)).fetchone()
        if cancellation and cancellation[0]:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
            with db.connect() as con:
                con.execute("UPDATE experiments SET status='cancelled',completed_at=?,error='用户取消；部分结果不是最终统计' WHERE id=? AND status='running'",(db.now(),job_id))
            break
        if time.monotonic()-started>360:
            child.kill();child.wait()
            break
        time.sleep(.5)


def ensure_market_cache():
    with db.connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS market_cache(market TEXT NOT NULL,symbol TEXT NOT NULL,interval TEXT NOT NULL,candles TEXT NOT NULL,filters TEXT NOT NULL,fetched_at REAL NOT NULL,PRIMARY KEY(market,symbol,interval))")


def market_loop(once=False):
    """One shared public feed poller, independent from account strategy processes."""
    from .binance import BinanceClient
    ensure_market_cache()
    clients={}
    while True:
        service("market",str(os.getpid()))
        with db.connect() as con:
            rows=con.execute("SELECT a.symbols,s.manifest FROM accounts a JOIN instances i ON i.account_id=a.id JOIN strategies s ON s.id=a.strategy_id WHERE a.mode='paper' AND a.source!='demo' AND (i.desired_state IN ('running','paused') OR i.observed_state IN ('running','paused','starting','error') OR EXISTS (SELECT 1 FROM commands c WHERE c.account_id=a.id AND c.action='flatten' AND c.status IN ('pending','running')))").fetchall()
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
                with db.connect() as con:
                    con.execute("INSERT INTO market_cache VALUES(?,?,'1m',?,?,?) ON CONFLICT(market,symbol,interval) DO UPDATE SET candles=excluded.candles,filters=excluded.filters,fetched_at=excluded.fetched_at",(market,symbol,db.dump(candles),db.dump(item.get("filters",item)),time.time()))
            except Exception as exc:
                db.log("error","共享行情更新失败",details={"symbol":symbol,"market":market,"exception":type(exc).__name__})
        if once:
            return
        time.sleep(15)


def _restore_ledger(account,manifest,snapshot):
    from .engine import Ledger,Position
    D=lambda x:Decimal(str(x))
    execution=db.parse(account["metrics"],{}).get("execution_model",{})
    fee=execution.get("fee_rate",.001 if manifest["market"]=="spot" else .0004)
    ledger=Ledger(D(account["initial_equity"]),manifest["market"],D(manifest["account"]["leverage_min"]),D(fee))
    if snapshot:
        ledger.balance=D(snapshot["balance"])
        ledger.fees=D(snapshot["fees"])
        ledger.positions={s:Position(D(p["quantity"]),D(p["average_price"]),D(p["entry_fees"])) for s,p in snapshot["positions"].items()}
        ledger.orders=snapshot["orders"]
        ledger.closed_pnls=[D(v) for v in snapshot["closed_pnls"]]
    return ledger


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


def runtime_tick(account_id,owner):
    """One transactionally fenced paper step. Returns True while actor is needed."""
    from .engine import _filters_for,calculate_metrics
    from .strategy import load_strategy,validate_parameters
    ensure_market_cache()
    fence=db.acquire_lease(account_id,owner)
    if fence is None:
        return True
    with db.connect() as con:
        account=con.execute("SELECT * FROM accounts WHERE id=?",(account_id,)).fetchone()
        instance=con.execute("SELECT * FROM instances WHERE account_id=?",(account_id,)).fetchone()
        commands=con.execute("SELECT * FROM commands WHERE account_id=? AND status IN ('pending','running') ORDER BY created_at",(account_id,)).fetchall()
        strategy=con.execute("SELECT * FROM strategies WHERE id=?",(account["strategy_id"],)).fetchone()
        saved=con.execute("SELECT payload FROM ledger WHERE account_id=? AND kind='snapshot' ORDER BY created_at DESC LIMIT 1",(account_id,)).fetchone()
    if account["mode"]=="live" or account["source"]=="demo":
        with db.connect() as con:
            for command in commands:
                con.execute("UPDATE commands SET status='failed',error=?,completed_at=? WHERE id=?",("演示账户或实盘执行闸门关闭；未向交易所下单",db.now(),command["id"]))
            con.execute("UPDATE instances SET observed_state='stopped',desired_state='stopped',error=? WHERE account_id=?",("执行闸门关闭",account_id))
        return False
    manifest=db.parse(strategy["manifest"],{})
    if not strategy["trusted"] or "one_way" not in manifest["account"]["position_modes"]:
        raise ValueError("运行策略未经审核或不支持单向账本")
    if manifest["market"]=="futures" and "cross" not in manifest["account"]["margin_modes"]:
        raise ValueError("当前模拟账本仅实现全仓模型，不能运行仅支持逐仓的策略")
    snapshot=db.parse(saved["payload"],{}) if saved else {}
    ledger=_restore_ledger(account,manifest,snapshot)
    revision={"strategy_id":account["strategy_id"],"source_hash":strategy["source_hash"],"config_revision":instance["config_revision"]}
    if snapshot.get("revision")!=revision:
        snapshot.update(pending=[],strategy_state={},bar_index=0)
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
                if commands:
                    con.execute("UPDATE commands SET status='completed',completed_at=?,result=? WHERE id=?",(db.now(),db.dump({"message":"无持仓，已停止"}),commands[0]["id"]))
                con.execute("UPDATE instances SET observed_state='stopped',last_heartbeat=?,error=NULL WHERE account_id=?",(db.now(),account_id))
            return False
        with db.connect() as con:
            con.execute("UPDATE instances SET observed_state='starting',error='等待新鲜的共享行情，禁止开仓',last_heartbeat=? WHERE account_id=?",(db.now(),account_id))
        return True
    histories={s:db.parse(c["candles"],[]) for s,c in zip(symbols,cached)}
    common=set.intersection(*(set(c["timestamp"] for c in rows) for rows in histories.values()))
    if not common:
        return True
    timestamp=max(common)
    if int(time.time()*1000)-timestamp>150000:
        with db.connect() as con:
            con.execute("UPDATE instances SET observed_state='starting',error='最后一根完整 K 线已过期，禁止开仓',last_heartbeat=? WHERE account_id=? AND owner=?",(db.now(),account_id,owner))
        return True
    bars={s:next(c for c in reversed(rows) if c["timestamp"]==timestamp) for s,rows in histories.items()}
    filters={s:_filters_for(s,{s:db.parse(c["filters"],{})}) for s,c in zip(symbols,cached)}
    pending=snapshot.get("pending",[])
    last=snapshot.get("last_timestamp",0)
    old_order_count=len(ledger.orders)
    if timestamp==last and not commands:
        with db.connect() as con:
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
            reason=ledger.execute(intent,timestamp,filters,Decimal(".0002"),last)
            risk.append({"status":"rejected" if reason else "accepted","reason":reason or "模拟撮合通过"})
    ledger.prices={s:Decimal(str(bar["close"])) for s,bar in bars.items()}
    if mode=="stopped":
        for symbol,position in list(ledger.positions.items()):
            if position.quantity:
                reason=ledger.execute({"symbol":symbol,"side":"sell" if position.quantity>0 else "buy","quantity":float(abs(position.quantity)),"reduce_only":True,"reason":"管理员平仓并停止"},timestamp,filters,Decimal(".0002"),timestamp)
                if reason:
                    raise ValueError(f"模拟平仓失败：{reason}")
        pending=[]
    display={}
    strategy_state=snapshot.get("strategy_state",{})
    if timestamp>last and mode in {"running","paused"}:
        strategy_obj=load_strategy(strategy["source"])
        strategy_obj.__dict__.update(copy.deepcopy(strategy_state))
        def strategy_log(level,message,data=None):
            db.log(level,message,strategy_id=account["strategy_id"],account_id=account_id,details=data)
        context={"params":validate_parameters(manifest,db.parse(account["config"],{})),"candles":copy.deepcopy(bars),"history":{s:[c for c in rows if c["timestamp"]<=timestamp][-manifest["history_window"]:] for s,rows in histories.items()},"positions":ledger.position_snapshot(),"account":ledger.account(),"timestamp":timestamp,"bar_index":snapshot.get("bar_index",0),"log":strategy_log,"emit":lambda **fields:display.update(fields)}
        with strategy_deadline():
            pending=strategy_obj.on_bar(context)
        strategy_state=copy.deepcopy(strategy_obj.__dict__)
        db.dump(strategy_state)  # State is durable JSON; unsupported state fails closed.
        if not isinstance(pending,list) or len(pending)>1000 or any(not isinstance(i,dict) for i in pending):
            raise ValueError("策略必须返回有限的交易意图列表")
        if mode=="paused":
            pending=[i for i in pending if i.get("reduce_only")]
    curve=snapshot.get("curve",[])
    if timestamp>last or mode=="stopped":
        curve.append({"timestamp":timestamp,"equity":float(ledger.equity()),"margin_used":float(ledger.margin()),"unrealized_pnl":float(ledger.unrealized())})
    stats=ui_metrics(calculate_metrics(curve,ledger.orders,account["initial_equity"],closed_pnls=ledger.closed_pnls,account=ledger.account()))
    positions=[dict(v,symbol=s,direction="long" if v["quantity"]>0 else "short") for s,v in ledger.position_snapshot().items()]
    from datetime import datetime, timezone
    current_dt=datetime.fromtimestamp(timestamp/1000,timezone.utc)
    day_start=current_dt.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
    month_start=current_dt.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
    def period_pnl(start):
        baseline=next((p["equity"] for p in reversed(curve) if p["timestamp"]<start),account["initial_equity"])
        return float(ledger.equity())-baseline
    metrics={"today_pnl":period_pnl(day_start),"month_pnl":period_pnl(month_start),"running_days":max(1,int((timestamp-curve[0]["timestamp"])/86400000)+1) if curve else 0,**ledger.account(),**stats,"return_pct":stats["total_return_pct"],"max_drawdown_u":stats["max_drawdown_amount"],"position_count":len(positions),"long_count":sum(p["quantity"]>0 for p in positions),"short_count":sum(p["quantity"]<0 for p in positions),"curve":curve,"positions":positions,"orders":ledger.orders,"display":display,"data_updated_at":db.now(),"risk":risk,"execution_model":{"fee_rate":float(ledger.fee_rate),"slippage_bps":2,"fee_source":"documented_simulation_assumption","interval":"1m","funding_included":False,"liquidation_simulated":False}}
    snapshot={"balance":str(ledger.balance),"fees":str(ledger.fees),"positions":{s:{"quantity":str(p.quantity),"average_price":str(p.average_price),"entry_fees":str(p.entry_fees)} for s,p in ledger.positions.items()},"orders":ledger.orders,"closed_pnls":[str(v) for v in ledger.closed_pnls],"pending":pending,"strategy_state":strategy_state,"revision":revision,"last_timestamp":timestamp,"bar_index":snapshot.get("bar_index",0)+(timestamp>last),"curve":curve}
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        lease=con.execute("SELECT * FROM instances WHERE account_id=?",(account_id,)).fetchone()
        if lease["owner"]!=owner or lease["fence"]!=fence or lease["lease_until"]<=time.time() or lease["desired_state"]!=instance["desired_state"]:
            return True
        for order in ledger.orders[old_order_count:]:
            con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),account_id,"fill",f"{account_id}:{order['id']}",db.dump(order),db.now()))
        if timestamp>last:
            con.execute("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),account_id,"valuation",f"valuation:{account_id}:{timestamp}",db.dump(curve[-1]),db.now()))
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON CONFLICT(reference) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",(db.uid(),account_id,"snapshot",f"snapshot:{account_id}",db.dump(snapshot),db.now()))
        con.execute("UPDATE accounts SET metrics=? WHERE id=?",(db.dump(metrics),account_id))
        con.execute("UPDATE instances SET observed_state=?,last_heartbeat=?,error=NULL WHERE account_id=?",(mode,db.now(),account_id))
        con.execute("INSERT INTO runtime_events(account_id,fence,event,payload,created_at) VALUES(?,?,?,?,?)",(account_id,fence,"paper_tick",db.dump({"timestamp":timestamp,"mode":mode,"risk":risk}),db.now()))
        if commands:
            con.execute("UPDATE commands SET status='completed',completed_at=?,result=? WHERE id=?",(db.now(),db.dump({"observed_state":mode,"fence":fence}),commands[0]["id"]))
    return mode!="stopped" or len(commands)>1


def account_loop(account_id,once=False):
    owner=db.uid()
    while True:
        try:
            keep_running=runtime_tick(account_id,owner)
        except Exception as exc:
            with db.connect() as con:
                con.execute("UPDATE instances SET observed_state='error',desired_state='stopped',error=? WHERE account_id=? AND owner=?",(str(exc)[:2000],account_id,owner))
                con.execute("UPDATE commands SET status='failed',completed_at=?,error=? WHERE account_id=? AND status IN ('pending','running')",(db.now(),str(exc)[:2000],account_id))
            db.log("error","模拟运行进程异常，停止继续开仓",account_id=account_id,details={"error":str(exc)[:2000]})
            return
        if once or not keep_running:
            return
        time.sleep(2)


def runtime_loop(once=False):
    owner=db.uid();children={}
    ensure_market_cache()
    market=None if once else subprocess.Popen([sys.executable,"-m","backend.worker","market"])
    try:
        while True:
            service("runtime",owner)
            for account_id,child in list(children.items()):
                if child.poll() is None:
                    with db.connect() as con:
                        heartbeat=con.execute("SELECT last_heartbeat,lease_until FROM instances WHERE account_id=?",(account_id,)).fetchone()
                        if heartbeat and heartbeat["last_heartbeat"] and heartbeat["lease_until"]<time.time()-15:
                            child.kill();child.wait()
                            con.execute("UPDATE instances SET desired_state='stopped',observed_state='error',error='策略心跳超时，执行进程已隔离停止，需人工检查后恢复' WHERE account_id=?",(account_id,))
                            con.execute("UPDATE commands SET status='failed',completed_at=?,error='策略心跳超时' WHERE account_id=? AND status IN ('pending','running')",(db.now(),account_id))
            with db.connect() as con:
                rows=con.execute("SELECT DISTINCT i.account_id FROM instances i LEFT JOIN commands c ON c.account_id=i.account_id WHERE i.desired_state IN ('running','paused') OR c.status='pending'").fetchall()
            for row in rows:
                id=row["account_id"]
                if id not in children or children[id].poll() is not None:
                    children[id]=subprocess.Popen([sys.executable,"-m","backend.worker","account",id]+(["--once"] if once else []),preexec_fn=_account_limits if os.name=="posix" else None)
            if market and market.poll() is not None:
                market=subprocess.Popen([sys.executable,"-m","backend.worker","market"])
            if once:
                for child in children.values():
                    child.wait(timeout=20)
                return
            time.sleep(2)
    finally:
        # Web restarts never execute this. A supervisor stop deliberately stops actors.
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        if market and market.poll() is None:
            market.terminate()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("backtest","runtime","market","job","account","init-demo","create-admin","trust-strategy"))
    parser.add_argument("id",nargs="?")
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--username", help="管理员用户名；不提供则交互输入")
    parser.add_argument("--generate-password", action="store_true", help="生成高强度密码并仅在创建成功时输出一次 JSON")
    parser.add_argument("--source-hash")
    parser.add_argument("--accept-trusted-code",action="store_true")
    args=parser.parse_args();db.init_db()
    if args.mode in {"runtime","backtest"}:
        signal.signal(signal.SIGTERM,lambda signum,frame:sys.exit(0))
    if args.mode in {"job","account","market"}:
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
        db.log("debug","通过本地 CLI 批准可信策略代码",strategy_id=args.id,details={"source_hash":args.source_hash})
        print("已启用该精确源码版本。进程资源限制不等于恶意代码沙箱；仅运行已审查的可信策略。")
    elif args.mode=="create-admin":
        import getpass
        from .security import hash_password
        import secrets
        import sqlite3
        username=(args.username or input("管理员登录名: ")).strip()
        if not username or len(username)>120:
            parser.error("管理员用户名长度必须为 1–120 字符")
        password=secrets.token_urlsafe(24) if args.generate_password else getpass.getpass("密码（至少 10 位）: ")
        try:
            with db.connect() as con:
                con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",(db.uid(),username,username,None,hash_password(password),"admin",db.now()))
        except (sqlite3.IntegrityError, ValueError) as exc:
            parser.error("管理员账号已存在，未覆盖原密码" if isinstance(exc,sqlite3.IntegrityError) else str(exc))
        db.log("debug","通过本地 CLI 创建管理员",details={"username":username})
        print(db.dump({"username":username,"password":password,"created":True}) if args.generate_password else "管理员已创建")
    elif args.mode=="backtest":backtest_loop(args.once)
    elif args.mode=="runtime":runtime_loop(args.once)
    elif args.mode=="market":market_loop(args.once)
    elif args.mode=="job":run_experiment(args.id)
    elif args.mode=="account":account_loop(args.id,args.once)


if __name__=="__main__":
    main()
