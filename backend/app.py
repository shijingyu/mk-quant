"""Authenticated control plane. Trading and backtesting run in independent workers."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import csv
import hashlib
import io
import itertools
import json
import math
import os
import platform
import secrets
import time
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from . import db, storage
from .security import decrypt_secret, encrypt_secret, hash_password, token_hash, verify_password


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    if os.getenv("MK_DEMO", "0") == "1":
        db.seed_demo()
    yield


app = FastAPI(title="MK Quant 控制服务", version="0.1.0", lifespan=lifespan)
COOKIE = "mk_session"


@app.exception_handler(storage.ArtifactError)
async def artifact_error_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail":str(exc)})


def exchange_error(exc):
    from .binance import BinanceError
    return str(exc) if isinstance(exc,BinanceError) else type(exc).__name__


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def user(request: Request):
    token = request.cookies.get(COOKIE, "")
    with db.connect() as con:
        row = con.execute("SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?", (token_hash(token),time.time())).fetchone()
    if not row:
        raise HTTPException(401, "请先登录")
    if request.method not in {"GET","HEAD","OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("X-MK-CSRF", ""), row["csrf"]):
            raise HTTPException(403, "CSRF 校验失败，请刷新登录状态")
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/")}
        allowed.update(v.strip() for v in os.getenv("MK_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if v.strip())
        if origin and origin.rstrip("/") not in allowed:
            raise HTTPException(403, "请求来源未获授权")
    return dict(row)


def admin(current=Depends(user)):
    if current["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return current


def public_user(current):
    with db.connect() as con:
        account = con.execute("SELECT route FROM accounts WHERE user_id=?",(current["id"],)).fetchone()
    return {k:current[k] for k in ("id","username","name","role")} | {"route":account["route"] if account else None}


@app.get("/healthz")
def health():
    with db.connect() as con:
        con.execute("SELECT 1").fetchone()
    return {"status":"ok","service":"control","engine_version":db.ENGINE_VERSION,"database":"mysql","table_engine":"InnoDB","artifact_storage":"parquet"}


@app.post("/api/login")
def login(body: dict, response: Response, request: Request):
    # Bounded, persisted per-identity attempt window avoids an unbounded in-memory throttle.
    username = str(body.get("username", ""))[:120]
    with db.connect() as con:
        recent = con.execute("SELECT COUNT(*) FROM logs WHERE level='auth_failure' AND details=? AND created_at>?", (db.dump({"username":username}),datetime.fromtimestamp(time.time()-300,timezone.utc).isoformat())).fetchone()[0]
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if recent >= 10:
        raise HTTPException(429, "登录失败次数过多，请 5 分钟后重试")
    if not row or not verify_password(str(body.get("password", "")), row["password"]):
        db.log("auth_failure","登录失败",details={"username":username})
        raise HTTPException(401, "账号或密码错误")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db.connect() as con:
        con.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        con.execute("INSERT INTO sessions VALUES(?,?,?,?)",(token_hash(token),row["id"],csrf,time.time()+12*3600))
        db.audit(con,row["id"],"auth.login","user",row["id"],{})
    response.set_cookie(COOKIE,token,httponly=True,samesite="strict",secure=os.getenv("MK_COOKIE_SECURE","0")=="1",max_age=12*3600,path="/")
    return {"current_user":public_user(dict(row)),"csrf_token":csrf}


@app.post("/api/logout")
def logout(request: Request, response: Response, current=Depends(user)):
    with db.connect() as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get(COOKIE,"")),))
        db.audit(con,current["id"],"auth.logout","user",current["id"],{})
    response.delete_cookie(COOKIE,path="/")
    return {"ok":True}


@app.get("/api/me")
def me(current=Depends(user)):
    return {"current_user":public_user(current),"csrf_token":current["csrf"]}


def strategy_row(strategy_id):
    with db.connect() as con:
        row=con.execute("SELECT * FROM strategies WHERE id=?",(strategy_id,)).fetchone()
    if not row:
        raise HTTPException(404,"策略不存在")
    return dict(row)


def list_strategy_data():
    with db.connect() as con:
        rows=con.execute("SELECT * FROM strategies ORDER BY created_at").fetchall()
    return [{**db.parse(r["manifest"],{}),"id":r["id"],"source":r["source"],"source_hash":r["source_hash"],"trusted":bool(r["trusted"]),"review_status":"admin_trusted" if r["trusted"] else "disabled"} for r in rows]


def account_data(row, *, include_private=False):
    d=dict(row)
    metrics=db.parse(d.pop("metrics"),{})
    d["config"]=db.parse(d["config"],{})
    d["symbols"]=db.parse(d["symbols"],[])
    d["has_credentials"]=bool(d.pop("credentials",None))
    d.pop("external_uid",None)
    d["check_result"]=db.parse(d.get("check_result"),None)
    with db.connect() as con:
        instance=con.execute("SELECT * FROM instances WHERE account_id=?",(d["id"],)).fetchone()
        strategy=con.execute("SELECT manifest FROM strategies WHERE id=?",(d["strategy_id"],)).fetchone()
    d.update(metrics)
    d["strategy_name"]=db.parse(strategy["manifest"],{}).get("name",d["strategy_id"]) if strategy else d["strategy_id"]
    d["strategy_version"]=db.parse(strategy["manifest"],{}).get("version","") if strategy else ""
    d["manifest_execution"]=db.parse(strategy["manifest"],{}).get("execution",{}) if strategy else {}
    d["execution_policy"]=d["manifest_execution"].get("model","market_next_open_v1")
    d["equity"]=metrics.get("equity",d["initial_equity"])
    for key in ("unrealized_pnl","realized_pnl","margin_used","return_pct","win_rate","profit_factor","max_drawdown_pct","max_drawdown_u","position_count","long_count","short_count","today_pnl","month_pnl","running_days"):
        d.setdefault(key,0)
    for key in ("curve","orders","positions","open_orders","lots"):
        d.setdefault(key,[])
    if instance:
        d["status"]=instance["observed_state"]
        d["desired_state"]=instance["desired_state"]
        d["last_heartbeat"]=instance["last_heartbeat"]
        d["runtime_error"]=instance["error"]
        d["config_revision"]=instance["config_revision"]
        d["stale"]=instance["observed_state"] in {"running","paused","starting","flattening"} and instance["lease_until"] <= time.time()
        if d["stale"]:
            d["status"]="unknown"
    else:
        d.update(status="stopped",desired_state="stopped",stale=False,last_heartbeat=None)
    d["data_source"]="illustrative" if d["source"]=="demo" else "exchange" if d["mode"]=="live" else "paper"
    if d["mode"]=="live" and d["source"]!="demo":
        d["data_source"]="unverified"
        d["initial_equity"]=None
        d["equity"]=None
        d["margin_used"]=None
        d["unrealized_pnl"]=None
        d["realized_pnl"]=None
        d["return_pct"]=None
        for key in ("today_pnl","month_pnl","win_rate","profit_factor","max_drawdown_pct","max_drawdown_u"):
            d[key]=None
        d["stale"]=True
    return d


def all_accounts(current):
    with db.connect() as con:
        rows=con.execute("SELECT a.*,u.phone FROM accounts a JOIN users u ON u.id=a.user_id"+("" if current["role"]=="admin" else " WHERE a.user_id=?")+" ORDER BY a.created_at",() if current["role"]=="admin" else (current["id"],)).fetchall()
    return [account_data(row) for row in rows]


def experiment_data(row,detail=False):
    d=dict(row)
    config=db.parse(d.pop("config"),{})
    result=storage.load_result(d.pop("result"), detail=detail)
    d.update(config)
    d["provenance"]=db.parse(d["provenance"],{})
    d.update(result)
    with db.connect() as con:
        strategy=con.execute("SELECT manifest FROM strategies WHERE id=?",(d["strategy_id"],)).fetchone()
    d["strategy_name"]=db.parse(strategy["manifest"],{}).get("name",d["strategy_id"]) if strategy else d["strategy_id"]
    d.setdefault("parameters",{})
    d.setdefault("metrics",{})
    d.setdefault("validation_metrics",{})
    d.setdefault("curve",[])
    d.setdefault("validation_curve",[])
    d["incomplete"]=d["status"]!="completed"
    if not detail:
        d.pop("decisions",None)
        d.pop("orders",None)
    return d


def list_experiments():
    with db.connect() as con:
        rows=con.execute("SELECT * FROM experiments ORDER BY created_at DESC LIMIT 200").fetchall()
    return [experiment_data(r) for r in rows]


def list_datasets():
    with db.connect() as con:
        rows=con.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    result=[]
    for row in rows:
        d=dict(row)
        metadata=storage.candle_metadata(d.pop("candles"))
        d.update(symbols=db.parse(d["symbols"],[]),**metadata)
        result.append(d)
    return result


def logs_data():
    with db.connect() as con:
        rows=con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 300").fetchall()
    return [{**dict(r),"details":db.parse(r["details"],None)} for r in rows]


def commands_data():
    with db.connect() as con:
        return [dict(r) | {"result":db.parse(r["result"],None)} for r in con.execute("SELECT * FROM commands ORDER BY created_at DESC LIMIT 100").fetchall()]


def services_data():
    with db.connect() as con:
        rows={r["name"]:dict(r) for r in con.execute("SELECT * FROM services").fetchall()}
    result=[{"name":"control","status":"online","heartbeat":db.now(),"label":"控制服务"}]
    for name,label in (("backtest","回测工作进程"),("runtime","交易运行进程"),("market","共享行情进程")):
        item=rows.get(name,{"name":name,"status":"offline","heartbeat":None})
        if item["heartbeat"] and datetime.fromisoformat(item["heartbeat"]).timestamp()<time.time()-45:
            item["status"]="stale"
        result.append(item | {"label":label})
    return result


def overview_data(accounts):
    known=[a for a in accounts if a.get("equity") is not None]
    total=sum(a["equity"] for a in known)
    initial=sum(a["initial_equity"] for a in known)
    timelines={a["id"]:sorted(a.get("curve",[]),key=lambda p:p.get("timestamp",p.get("time",0))) for a in known}
    timestamps=sorted({p.get("timestamp",p.get("time")) for points in timelines.values() for p in points})
    indexes={a["id"]:0 for a in known}; values={a["id"]:a["initial_equity"] for a in known}; curve=[]
    for timestamp in timestamps:
        for account in known:
            key=account["id"]; points=timelines[key]
            while indexes[key]<len(points) and points[indexes[key]].get("timestamp",points[indexes[key]].get("time"))<=timestamp:
                values[key]=points[indexes[key]]["equity"]; indexes[key]+=1
        curve.append({"timestamp":timestamp,"equity":sum(values.values())})
    return {"total_equity":round(total,2),"initial_equity":initial,"total_profit":round(total-initial,2),"return_pct":round((total/initial-1)*100,2) if initial else 0,"margin_used":sum(a["margin_used"] or 0 for a in known),"unrealized_pnl":sum(a["unrealized_pnl"] or 0 for a in known),"today_pnl":sum(a["today_pnl"] or 0 for a in known),"month_pnl":sum(a["month_pnl"] or 0 for a in known),"account_count":len(accounts),"unknown_account_count":len(accounts)-len(known),"running_count":sum(a["status"]=="running" for a in accounts),"curve":curve,"source":"demo" if any(a["source"]=="demo" for a in accounts) else "local"}


@app.get("/api/bootstrap")
def bootstrap(current=Depends(user)):
    accounts=all_accounts(current)
    result={"current_user":public_user(current),"csrf_token":current["csrf"],"accounts":accounts,"overview":overview_data(accounts),"demo":os.getenv("MK_DEMO","0")=="1","live_enabled":False,"server_time":db.now()}
    if current["role"]=="admin":
        result.update(strategies=list_strategy_data(),experiments=list_experiments(),datasets=list_datasets(),logs=logs_data(),commands=commands_data(),services=services_data())
    return result


@app.get("/api/accounts")
def accounts(current=Depends(user)):
    return all_accounts(current)


def validate_config(manifest, parameters, symbols, market=None):
    from .strategy import validate_parameters, validate_symbols
    if not isinstance(symbols,list) or any(not isinstance(s,str) or not s.isalnum() or not s.endswith(("USDT","USDC")) for s in symbols):
        raise HTTPException(422,"仅支持以 USDT / USDC 计价的有效交易对")
    if market and (market=="spot") != (manifest.get("market","futures")=="spot"):
        raise HTTPException(422,"现货与合约策略、数据不可混用")
    try:
        validate_symbols(manifest,symbols)
        return validate_parameters(manifest,parameters)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@app.post("/api/accounts",status_code=201)
def create_account(body:dict,current=Depends(admin)):
    strategy=strategy_row(body.get("strategy_id",""))
    manifest=db.parse(strategy["manifest"],{})
    symbols=body.get("symbols",["BTCUSDT"])
    config=validate_config(manifest,body.get("config",{}),symbols)
    mode=body.get("mode","paper")
    if mode not in {"paper","live"}:
        raise HTTPException(422,"mode 必须为 paper 或 live")
    name=str(body.get("name","")).strip()
    phone=str(body.get("phone","")).strip()
    if not name or not phone:
        raise HTTPException(422,"请填写姓名与手机号")
    try:
        password=hash_password(str(body.get("password","")))
        initial=float(body.get("initial_equity",10000))
        if not math.isfinite(initial) or initial<=0:
            raise ValueError("初始资金必须大于 0")
        requirements=manifest.get("account",{})
        minimum=requirements.get("min_capital",requirements.get("minimum_capital",requirements.get("minimum_equity",0)))
        if mode=="paper" and initial<minimum:
            raise ValueError(f"策略最低资金要求 {minimum} U")
        credentials=None
        if mode=="live":
            if not body.get("api_key") or not body.get("api_secret"):
                raise ValueError("实盘账户需要 API Key 与 Secret")
            credentials=encrypt_secret(db.dump({"api_key":body["api_key"],"api_secret":body["api_secret"]}))
    except (ValueError,TypeError) as exc:
        raise HTTPException(422,str(exc)) from exc
    user_id,account_id=db.uid(),db.uid()
    try:
        with db.connect() as con:
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",(user_id,str(body.get("username",phone)),name,phone,password,"client",db.now()))
            con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(account_id,user_id,name,mode,db.uid(),strategy["id"],db.dump(config),db.dump(symbols),initial,db.dump({"equity":initial,"curve":[],"positions":[],"orders":[]}),credentials,None,None,"local",db.now()))
            con.execute("INSERT INTO instances(account_id) VALUES(?)",(account_id,))
            db.audit(con,current["id"],"account.create","account",account_id,{"strategy_id":strategy["id"],"mode":mode,"symbols":symbols})
    except db.IntegrityError as exc:
        raise HTTPException(409,"登录账号已存在") from exc
    db.log("debug","管理员创建账户",account_id=account_id,strategy_id=strategy["id"])
    return next(a for a in all_accounts(current) if a["id"]==account_id)


@app.patch("/api/accounts/{account_id}")
def update_account(account_id:str,body:dict,current=Depends(admin)):
    with db.connect() as con:
        row=con.execute("SELECT a.*,i.observed_state,i.desired_state FROM accounts a JOIN instances i ON i.account_id=a.id WHERE a.id=?",(account_id,)).fetchone()
    if not row:
        raise HTTPException(404,"账户不存在")
    if row["observed_state"]!="stopped" or row["desired_state"]!="stopped":
        raise HTTPException(409,"停止运行并收到执行端确认后才能修改策略配置")
    strategy_id=body.get("strategy_id",row["strategy_id"])
    strategy=strategy_row(strategy_id)
    symbols=body.get("symbols",db.parse(row["symbols"],[]))
    config=validate_config(db.parse(strategy["manifest"],{}),body.get("config",db.parse(row["config"],{})),symbols)
    current_metrics=db.parse(row["metrics"],{})
    changes_execution=(strategy_id!=row["strategy_id"] or symbols!=db.parse(row["symbols"],[]) or config!=db.parse(row["config"],{}))
    if changes_execution and (current_metrics.get("positions") or current_metrics.get("open_orders")):
        raise HTTPException(409,"账户仍有持仓或挂单，完成平仓撤单后才能修改策略、币种或参数")
    if changes_execution and row["mode"]=="paper":
        equity=current_metrics.get("equity",row["initial_equity"])
        if body.get("initial_equity") is not None and body["initial_equity"]!=row["initial_equity"]:
            equity=body["initial_equity"]
        try:
            equity=float(equity)
            if not math.isfinite(equity) or equity<db.parse(strategy["manifest"],{})["account"]["min_capital"]:
                raise ValueError()
        except (ValueError,TypeError):
            raise HTTPException(422,"当前模拟权益低于所选策略最低资金要求，不能切换策略或配置")
    if body.get("mode",row["mode"])!=row["mode"]:
        raise HTTPException(422,"请为另一运行环境创建独立账户，不能转换已有账本的市场环境")
    name=str(body.get("name",row["name"])).strip()
    if not name:
        raise HTTPException(422,"姓名不能为空")
    initial=row["initial_equity"]
    metrics=db.parse(row["metrics"],{})
    if body.get("initial_equity") is not None and body["initial_equity"]!=initial:
        try:
            initial=float(body["initial_equity"])
            minimum=db.parse(strategy["manifest"],{})["account"]["min_capital"]
            if not math.isfinite(initial) or initial<=0 or initial<minimum:
                raise ValueError("初始资金无效或低于策略最低要求")
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc)) from exc
        with db.connect() as con:
            has_ledger=con.execute("SELECT 1 FROM ledger WHERE account_id=? LIMIT 1",(account_id,)).fetchone()
        if row["mode"]!="paper" or row["source"]=="demo" or has_ledger:
            raise HTTPException(409,"仅未产生运行账本的独立模拟账户可修改初始资金")
        metrics={"equity":initial,"curve":[],"positions":[],"orders":[]}
    credentials=row["credentials"]
    password=None
    try:
        if body.get("password"):
            password=hash_password(str(body["password"]))
        if body.get("api_key") or body.get("api_secret"):
            if row["mode"]!="live" or not body.get("api_key") or not body.get("api_secret"):
                raise ValueError("更新实盘凭证需要同时填写 API Key 和 Secret")
            credentials=encrypt_secret(db.dump({"api_key":body["api_key"],"api_secret":body["api_secret"]}))
        with db.connect() as con:
            locked=db.lock_instance(con,account_id)
            if not locked or locked["observed_state"]!="stopped" or locked["desired_state"]!="stopped":
                raise HTTPException(409,"账户状态已改变，请等待停止回执后再修改")
            if initial!=row["initial_equity"] and con.execute("SELECT 1 FROM ledger WHERE account_id=? LIMIT 1",(account_id,)).fetchone():
                raise HTTPException(409,"账户已产生运行账本，不能修改初始资金")
            con.execute("UPDATE accounts SET name=?,strategy_id=?,config=?,symbols=?,credentials=?,initial_equity=?,metrics=?,check_result=NULL WHERE id=?",(name,strategy_id,db.dump(config),db.dump(symbols),credentials,initial,db.dump(metrics),account_id))
            current_user=con.execute("SELECT * FROM users WHERE id=?",(row["user_id"],)).fetchone()
            phone=str(body.get("phone",current_user["phone"] or "")).strip()
            if not phone:
                raise ValueError("手机号不能为空")
            username=phone if current_user["username"]==current_user["phone"] else current_user["username"]
            con.execute("UPDATE users SET name=?,phone=?,username=?,password=? WHERE id=?",(name,phone,username,password or current_user["password"],row["user_id"]))
            if changes_execution or initial!=row["initial_equity"] or credentials!=row["credentials"]:
                con.execute("UPDATE instances SET config_revision=config_revision+1 WHERE account_id=?",(account_id,))
            if password or credentials!=row["credentials"]:
                con.execute("DELETE FROM sessions WHERE user_id=?",(row["user_id"],))
            db.audit(con,current["id"],"account.update","account",account_id,{"strategy_id":strategy_id,"symbols":symbols,"parameters":config,"password_changed":bool(password),"credentials_changed":credentials!=row["credentials"]})
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except db.IntegrityError as exc:
        raise HTTPException(409,"更新后的登录账号已存在") from exc
    return next(a for a in all_accounts(current) if a["id"]==account_id)


@app.post("/api/accounts/{account_id}/check")
def check_account(account_id:str,body:dict=None,current=Depends(admin)):
    with db.connect() as con:
        account=con.execute("SELECT * FROM accounts WHERE id=?",(account_id,)).fetchone()
    if not account:
        raise HTTPException(404,"账户不存在")
    strategy=strategy_row(account["strategy_id"])
    manifest=db.parse(strategy["manifest"],{})
    if account["mode"]=="paper":
        result={"ok":True,"checks":[{"name":"模拟账户","status":"pass","message":"独立模拟账本，无交易所权限"}],"adjustments":[],"checked_at":db.now()}
    elif not account["credentials"]:
        result={"ok":False,"checks":[{"name":"API 凭证","status":"fail","message":"演示账户没有真实 API 凭证"}],"adjustments":[],"checked_at":db.now()}
    else:
        try:
            from .binance import BinanceClient
            credentials=db.parse(decrypt_secret(account["credentials"]),{})
            client=BinanceClient(market=manifest.get("market","futures"),**credentials)
            result=client.inspect_account(manifest,db.parse(account["symbols"],[]))
            if body and body.get("apply_adjustments"):
                result=client.apply_adjustments(manifest,db.parse(account["symbols"],[]),confirmed=True)
                result["applied_adjustments"]=result.get("applied",[])
            result["checked_at"]=db.now()
            identity=result.get("identity")
            if isinstance(identity,dict):
                identity=identity.get("uid",identity.get("account_id"))
            if identity:
                with db.connect() as con:
                    con.execute("UPDATE accounts SET external_uid=? WHERE id=?",(str(identity),account_id))
        except db.IntegrityError as exc:
            raise HTTPException(409,"该实际交易所账户已被其他账户绑定") from exc
        except Exception as exc:
            result={"ok":False,"checks":[{"name":"交易所连接","status":"unknown","message":f"接入检查未完成：{exchange_error(exc)}"}],"adjustments":[],"checked_at":db.now()}
            db.log("error","账户接入检查失败",account_id=account_id,details={"exception":type(exc).__name__})
    with db.connect() as con:
        con.execute("UPDATE accounts SET check_result=? WHERE id=?",(db.dump(result),account_id))
        db.audit(con,current["id"],"account.adjust" if body and body.get("apply_adjustments") else "account.check","account",account_id,{"ok":result.get("ok"),"applied_adjustments":result.get("applied_adjustments",[])})
    return result


@app.get("/api/accounts/{account_id}/commission")
def account_commission(account_id:str,symbol:str="BTCUSDT",current=Depends(admin)):
    with db.connect() as con:
        account=con.execute("SELECT * FROM accounts WHERE id=?",(account_id,)).fetchone()
    if not account or not account["credentials"]:
        raise HTTPException(422,"该账户尚未提供币安凭证；个人费率需要签名接口，不能从公开 K 线接口读取")
    if symbol not in db.parse(account["symbols"],[]):
        raise HTTPException(422,"该币种不在账户运行范围")
    try:
        from .binance import BinanceClient
        manifest=db.parse(strategy_row(account["strategy_id"])["manifest"],{})
        credentials=db.parse(decrypt_secret(account["credentials"]),{})
        return {"symbol":symbol,"source":"binance_signed_account","rates":BinanceClient(market=manifest["market"],**credentials).commission(symbol),"fetched_at":db.now()}
    except Exception as exc:
        raise HTTPException(502,f"账户费率读取失败：{exchange_error(exc)}") from exc


@app.post("/api/commands",status_code=202)
def command(body:dict,current=Depends(admin)):
    ids=body.get("account_ids",[])
    action=body.get("action")
    if action not in {"pause","flatten","resume"} or not isinstance(ids,list) or not ids or len(ids)>1000:
        raise HTTPException(422,"请选择账户及有效操作")
    ids=sorted(set(ids))
    if len(ids)>1 and not (body.get("confirmed") is True and body.get("confirmed_twice") is True):
        raise HTTPException(422,"批量操作需要两次明确确认影响范围")
    if action=="flatten" and body.get("confirmed") is not True:
        raise HTTPException(422,"平仓并停止需要明确确认")
    commands=[]
    with db.connect() as con:
        for account_id in ids:
            db.lock_instance(con,account_id)
            account=con.execute("SELECT * FROM accounts WHERE id=?",(account_id,)).fetchone()
            if not account:
                raise HTTPException(404,"账户不存在")
            if action=="resume" and account["source"]=="demo":
                raise HTTPException(409,"演示账户不可启动真实运行；请创建独立模拟账户")
            if action=="resume" and account["mode"]=="live":
                raise HTTPException(409,"当前版本实盘执行仍处于验收闸门，暂不允许启动真实交易")
            id=db.uid()
            con.execute("INSERT INTO commands(id,account_id,action,requested_by,created_at) VALUES(?,?,?,?,?)",(id,account_id,action,current["id"],db.now()))
            desired={"pause":"paused","flatten":"stopped","resume":"running"}[action]
            con.execute("UPDATE instances SET desired_state=? WHERE account_id=?",(desired,account_id))
            db.audit(con,current["id"],"command.request","command",id,{"account_id":account_id,"action":action,"confirmed":body.get("confirmed") is True,"confirmed_twice":body.get("confirmed_twice") is True})
            commands.append({"id":id,"account_id":account_id,"action":action,"status":"pending"})
    return {"commands":commands,"message":"指令已持久化，等待独立执行端确认"}


@app.get("/api/client/{route}")
def client_account(route:str,current=Depends(user)):
    with db.connect() as con:
        account=con.execute("SELECT * FROM accounts WHERE route=?",(route,)).fetchone()
    if not account or (current["role"]!="admin" and account["user_id"]!=current["id"]):
        raise HTTPException(404,"账户不存在")
    return account_data(account)


@app.get("/api/strategies")
def strategies(current=Depends(admin)):
    return list_strategy_data()


@app.post("/api/strategies",status_code=201)
def save_strategy(body:dict,current=Depends(admin)):
    source=body.get("source","")
    if not isinstance(source,str) or len(source.encode())>300000:
        raise HTTPException(422,"策略源文件不得超过 300 KB")
    from .strategy import validate_strategy_source
    try:
        manifest=validate_strategy_source(source)
    except (ValueError,SyntaxError) as exc:
        raise HTTPException(422,str(exc)) from exc
    id=db.uid()
    manifest["id"]=id
    with db.connect() as con:
        source_hash=hashlib.sha256(source.encode()).hexdigest()
        con.execute("INSERT INTO strategies VALUES(?,?,?,?,1,?)",(id,db.dump(manifest),source,source_hash,db.now()))
        version_id=db.pin_strategy_version(con,id,manifest,source,source_hash)
        db.audit(con,current["id"],"strategy.create","strategy",id,{"version_id":version_id,"source_hash":source_hash})
    return {**manifest,"source":source,"source_hash":hashlib.sha256(source.encode()).hexdigest(),"trusted":True,"review_status":"admin_trusted","message":"契约检查通过，已按管理员可信代码启用；策略在独立进程运行"}


@app.get("/api/presets")
def presets(current=Depends(admin)):
    with db.connect() as con:
        return [dict(r)|{"parameters":db.parse(r["parameters"],{})} for r in con.execute("SELECT * FROM presets ORDER BY created_at DESC").fetchall()]


@app.post("/api/presets",status_code=201)
def save_preset(body:dict,current=Depends(admin)):
    strategy_row(body.get("strategy_id",""))
    id=db.uid()
    with db.connect() as con:
        con.execute("INSERT INTO presets VALUES(?,?,?,?,?)",(id,body["strategy_id"],str(body.get("name","未命名预设")),db.dump(body.get("parameters",{})),db.now()))
        db.audit(con,current["id"],"preset.create","preset",id,{"strategy_id":body["strategy_id"]})
    return {"id":id,**body}


def timestamp(value):
    if isinstance(value,(int,float)):
        if isinstance(value,bool) or not math.isfinite(value):
            raise HTTPException(422,"时间戳必须为有限数字")
        return int(value if value>1e11 else value*1000)
    try:
        d=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if not d.tzinfo:
            d=d.replace(tzinfo=timezone.utc)
        return int(d.timestamp()*1000)
    except (ValueError,TypeError) as exc:
        raise HTTPException(422,"时间格式必须为 ISO 日期或 Unix 时间戳") from exc


@app.get("/api/datasets")
def datasets(current=Depends(admin)):
    return list_datasets()


def store_dataset(body,source,actor_id=None):
    market=body.get("market","futures")
    if market=="perpetual":
        market="futures"
    if market not in {"spot","futures","perpetual"}:
        raise HTTPException(422,"不支持该市场")
    candles=body.get("candles",{})
    symbols=body.get("symbols",list(candles) if isinstance(candles,dict) else [])
    if isinstance(candles,list) and len(symbols)==1:
        candles={symbols[0]:candles}
    if not isinstance(candles,dict) or not candles or set(candles)!=set(symbols):
        raise HTTPException(422,"K 线必须按币种提供，格式 {BTCUSDT: [{timestamp,open,high,low,close,volume}]}，且 symbols 与数据键一致")
    count=sum(len(rows) for rows in candles.values())
    if count>500000 or not count:
        raise HTTPException(422,"单个数据集需要 1–500000 根 K 线")
    normalized={}
    for symbol,rows in candles.items():
        if not isinstance(rows,list) or not rows:
            raise HTTPException(422,f"{symbol} 至少需要一根 K 线")
        seen=set(); normalized[symbol]=[]
        for c in rows:
            try:
                row={k:float(c[k]) for k in ("open","high","low","close","volume")}
                row["timestamp"]=timestamp(c.get("timestamp",c.get("open_time")))
                if not all(math.isfinite(v) for v in row.values()) or min(row[k] for k in ("open","high","low","close"))<=0 or row["volume"]<0 or row["low"]>min(row["open"],row["close"]) or row["high"]<max(row["open"],row["close"]) or row["timestamp"] in seen:
                    raise ValueError()
                seen.add(row["timestamp"]); normalized[symbol].append(row)
            except (KeyError,TypeError,ValueError) as exc:
                raise HTTPException(422,f"{symbol} 的 K 线格式、OHLC 范围或时间唯一性校验失败") from exc
        normalized[symbol].sort(key=lambda r:r["timestamp"])
    id=db.uid(); serialized=db.dump(normalized)
    artifact=storage.write_candles(normalized)
    with db.connect() as con:
        con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?)",(id,str(body.get("name","导入 K 线")),market,str(body.get("interval","1h")),db.dump(symbols),db.dump(artifact),hashlib.sha256(serialized.encode()).hexdigest(),source,db.now()))
        db.audit(con,actor_id,"dataset.create","dataset",id,{"source":source,"count":count,"hash":artifact["content_sha256"]})
    return next(x for x in list_datasets() if x["id"]==id)


@app.post("/api/datasets",status_code=201)
def import_dataset(body:dict,current=Depends(admin)):
    return store_dataset(body,"import",current["id"])


@app.post("/api/datasets/download",status_code=201)
def download_dataset(body:dict,current=Depends(admin)):
    from .binance import BinanceClient
    symbols=body.get("symbols",[])
    if not symbols or len(symbols)>10:
        raise HTTPException(422,"单次选择 1–10 个币种")
    start,end=timestamp(body.get("start",body.get("start_date"))),timestamp(body.get("end",body.get("end_date")))
    if start>=end:
        raise HTTPException(422,"下载起始日期必须早于结束日期")
    try:
        client=BinanceClient(market=body.get("market","futures"))
        candles={s:client.klines(s,body.get("interval","1h"),start,end) for s in symbols}
    except Exception as exc:
        raise HTTPException(502,f"币安行情下载失败：{exchange_error(exc)}") from exc
    return store_dataset({**body,"candles":candles},"binance_public",current["id"])


@app.get("/api/market/symbols")
def market_symbols(market:str="futures",current=Depends(admin)):
    try:
        from .binance import BinanceClient
        return BinanceClient(market=market).exchange_info()
    except Exception as exc:
        raise HTTPException(502,f"交易所规则获取失败：{exchange_error(exc)}") from exc


@app.get("/api/experiments")
def experiments(current=Depends(admin)):
    return list_experiments()


def enqueue_experiment(body, *, persist=True, rules_snapshot=None, actor_id=None):
    strategy=strategy_row(body.get("strategy_id",""))
    manifest=db.parse(strategy["manifest"],{})
    params=validate_config(manifest,body.get("parameters",body.get("params",{})),body.get("symbols",["BTCUSDT"]),body.get("market",manifest.get("market","futures")))
    if not strategy["trusted"]:
        raise HTTPException(409,"自定义策略尚待离线安全审核；控制服务不会执行上传的 Python 代码")
    with db.connect() as con:
        dataset=con.execute("SELECT * FROM datasets WHERE id=?",(body.get("dataset_id",""),)).fetchone()
    if not dataset:
        raise HTTPException(422,"请选择可用的 K 线数据集")
    if (dataset["market"]=="spot") != (manifest.get("market","futures")=="spot"):
        raise HTTPException(422,"K 线数据市场与策略市场不一致")
    symbols=body.get("symbols",["BTCUSDT"])
    if not set(symbols)<=set(db.parse(dataset["symbols"],[])):
        raise HTTPException(422,"数据集未覆盖全部选中币种")
    ranges=[timestamp(body.get(k)) for k in ("train_start","train_end","validation_start","validation_end")]
    if not ranges[0]<ranges[1]<=ranges[2]<ranges[3]:
        raise HTTPException(422,"训练集和验证集必须按时间顺序分离，区间采用 [开始,结束) 且不可重叠")
    dataset_candles=storage.read_candles(dataset["candles"], symbols=symbols, start=ranges[0], end=ranges[3])
    for symbol in symbols:
        times=[timestamp(c["timestamp"]) for c in dataset_candles[symbol]]
        for start,end in (ranges[:2],ranges[2:]):
            if sum(start<=t<end for t in times)<2:
                raise HTTPException(422,f"{symbol} 在选中区间不足 2 根 K 线")
    maker=manifest.get("execution",{}).get("model")=="maker_grid_v1"
    try:
        capital=float(body.get("initial_capital",10000)); fee=float(body.get("fee_rate",.0002 if maker else .0004)); slip=float(body.get("slippage_bps",0 if maker else 2)); seed=int(body.get("seed",42))
        if not all(math.isfinite(x) for x in (capital,fee,slip)) or capital<=0 or not 0<=fee<=.1 or not 0<=slip<=1000:
            raise ValueError()
    except (ValueError,TypeError) as exc:
        raise HTTPException(422,"资金必须大于 0，费率范围 0–0.1，滑点范围 0–1000 bps") from exc
    if capital<manifest["account"]["min_capital"]:
        raise HTTPException(422,"初始资金低于策略最低资金要求")
    if maker and slip!=0:
        raise HTTPException(422,"Maker 网格使用限价撮合，滑点须为 0；手续费请填写 Maker 费率")
    try:
        leverage=float(body.get("leverage",manifest["account"]["leverage_min"]))
        if not math.isfinite(leverage) or not leverage.is_integer() or not manifest["account"]["leverage_min"]<=leverage<=manifest["account"]["leverage_max"]:
            raise ValueError()
    except (ValueError,TypeError):
        raise HTTPException(422,"杠杆必须是策略允许范围内的整数，现货仅支持 1 倍")
    if rules_snapshot:
        filters,filters_source=rules_snapshot
    elif dataset["source"]=="synthetic":
        filters={s:{"tick_size":"0.01","step_size":"0.001","min_qty":"0.001","min_notional":"5"} for s in symbols}
        filters_source="synthetic_documented_fixture"
    else:
        try:
            from .binance import BinanceClient
            exchange_info=BinanceClient(market=manifest["market"]).exchange_info()
            filters={item["symbol"]:item for item in exchange_info["symbols"] if item["symbol"] in symbols}
            if len(filters)!=len(symbols):
                raise ValueError("交易对缺少交易规则")
            filters_source="binance_exchange_info_snapshot"
        except Exception as exc:
            raise HTTPException(502,"无法取得币安交易精度规则，实验未启动；请检查行情网络") from exc
    common=set.intersection(*(set(timestamp(c["timestamp"]) for c in dataset_candles[s]) for s in symbols))
    if any(sum(start<=t<end for t in common)<2 for start,end in (ranges[:2],ranges[2:])):
        raise HTTPException(422,"多币种在训练集或验证集缺少至少 2 根共同时间戳 K 线")
    # Do not copy display fields (id/status/result/provenance) from a cloned
    # experiment into its immutable configuration: they would shadow DB state.
    config={key:body[key] for key in ("name","strategy_id","dataset_id","train_start","train_end","validation_start","validation_end") if key in body}
    config.update(parameters=params,symbols=symbols,initial_capital=capital,fee_rate=fee,slippage_bps=slip,seed=seed,market=manifest.get("market","futures"),leverage=int(leverage),ranges_ms=ranges,symbol_filters=filters)
    provenance={"strategy_version":manifest.get("version"),"strategy_hash":strategy["source_hash"],"parameters_hash":hashlib.sha256(db.dump(params).encode()).hexdigest(),"data_version":dataset["id"],"data_hash":dataset["sha256"],"engine_version":db.ENGINE_VERSION,"fee_model":{"taker_rate":fee,"source":"manual"},"slippage_model":{"bps":slip,"type":"fixed_adverse"},"seed":seed,"intervals":dict(zip(("train_start","train_end","validation_start","validation_end"),ranges)),"environment":{"python":platform.python_version(),"platform":platform.system()},"source":"synthetic" if dataset["source"]=="synthetic" else "historical"}
    provenance["symbol_filters"]={"source":filters_source,"rules":filters}
    if maker:
        provenance.update(execution_model="maker_grid_v1",position_mode="hedge",fee_model={"maker_rate":fee,"source":"manual"},slippage_model={"bps":0,"type":"post_only_limit_no_taker_fallback"})
    prepared={"id":db.uid(),"name":str(body.get("name") or manifest.get("name","策略")+" · 参数实验"),"strategy_id":strategy["id"],"dataset_id":dataset["id"],"config":config,"provenance":provenance,"strategy_snapshot":strategy}
    if not persist:
        return prepared
    return store_experiments([prepared],actor_id=actor_id)[0]


def store_experiments(items,actor_id=None):
    with db.connect() as con:
        for item in items:
            snapshot=item["strategy_snapshot"]
            version_id=db.pin_strategy_version(con,item["strategy_id"],db.parse(snapshot["manifest"],{}),snapshot["source"],snapshot["source_hash"])
            item["config"]["strategy_version_id"]=version_id
            item["provenance"]["strategy_version_id"]=version_id
            con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at) VALUES(?,?,?,?,?,?,?,?)",(item["id"],item["name"],item["strategy_id"],item["dataset_id"],"queued",db.dump(item["config"]),db.dump(item["provenance"]),db.now()))
            db.audit(con,actor_id,"experiment.enqueue","experiment",item["id"],{"strategy_version_id":version_id,"data_hash":item["provenance"]["data_hash"],"parameters_hash":item["provenance"]["parameters_hash"]})
    return [{"id":item["id"],"status":"queued","provenance":item["provenance"]} for item in items]


@app.post("/api/experiments",status_code=202)
def create_experiment(body:dict,current=Depends(admin)):
    grid=body.get("parameter_grid")
    if not grid:
        return enqueue_experiment(body,actor_id=current["id"])
    if not isinstance(grid,dict) or not all(isinstance(v,list) and v for v in grid.values()):
        raise HTTPException(422,"批量参数格式为参数名到候选值列表")
    total=math.prod(len(v) for v in grid.values())
    if total>50:
        raise HTTPException(422,"每次批量实验最多 50 组")
    prepared=[]
    rules=None
    for i,values in enumerate(itertools.product(*grid.values())):
        item=enqueue_experiment({**body,"parameters":{**body.get("parameters",{}),**dict(zip(grid,values))},"name":f"{body.get('name','批量实验')} #{i+1}"},persist=False,rules_snapshot=rules)
        prepared.append(item)
        rules=(item["config"]["symbol_filters"],item["provenance"]["symbol_filters"]["source"])
    return {"experiments":store_experiments(prepared,actor_id=current["id"])}


@app.get("/api/experiments/{experiment_id}")
def experiment_detail(experiment_id:str,current=Depends(admin)):
    with db.connect() as con:
        row=con.execute("SELECT * FROM experiments WHERE id=?",(experiment_id,)).fetchone()
    if not row:
        raise HTTPException(404,"实验不存在")
    return experiment_data(row,True)


@app.post("/api/experiments/{experiment_id}/cancel")
def cancel_experiment(experiment_id:str,current=Depends(admin)):
    with db.connect() as con:
        row=con.execute("SELECT status FROM experiments WHERE id=? FOR UPDATE",(experiment_id,)).fetchone()
        if not row:
            raise HTTPException(404,"实验不存在")
        if row["status"] not in {"queued","running"}:
            raise HTTPException(409,"该实验已结束")
        con.execute("UPDATE experiments SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",(experiment_id,))
        db.audit(con,current["id"],"experiment.cancel","experiment",experiment_id,{})
    return {"id":experiment_id,"cancel_requested":True}


@app.post("/api/experiments/{experiment_id}/retry",status_code=202)
def retry_experiment(experiment_id:str,current=Depends(admin)):
    with db.connect() as con:
        row=con.execute("SELECT config FROM experiments WHERE id=?",(experiment_id,)).fetchone()
    if not row:
        raise HTTPException(404,"实验不存在")
    return enqueue_experiment(db.parse(row["config"],{}),actor_id=current["id"])


@app.get("/api/experiments/{experiment_id}/export")
def export_experiment(experiment_id:str,format:str="json",current=Depends(admin)):
    detail=experiment_detail(experiment_id,current)
    if format=="csv":
        stream=io.StringIO(); writer=csv.writer(stream)
        writer.writerow(["split","timestamp","equity"])
        for split,key in (("train","curve"),("validation","validation_curve")):
            for row in detail[key]:
                writer.writerow([split,row.get("timestamp",row.get("time")),row.get("equity")])
        content=stream.getvalue();media="text/csv; charset=utf-8"
    else:
        content=json.dumps(detail,ensure_ascii=False,indent=2);media="application/json"
    return Response(content,media_type=media,headers={"Content-Disposition":f'attachment; filename="experiment-{experiment_id}.{format if format=="csv" else "json"}"'})


@app.get("/api/logs")
def logs(level:str=None,strategy_id:str=None,current=Depends(admin)):
    return [r for r in logs_data() if (not level or r["level"]==level) and (not strategy_id or r["strategy_id"]==strategy_id)]


@app.get("/api/commands")
def commands(current=Depends(admin)):
    return commands_data()


@app.get("/api/protocol")
def protocol(current=Depends(admin)):
    from pathlib import Path
    path=Path(__file__).resolve().parent.parent/"docs"/"STRATEGY_PROTOCOL.md"
    content=path.read_text() if path.exists() else "策略必须包含 MANIFEST 与 Strategy.on_bar(context)。上传只执行静态语法及契约检查；自定义代码待安全审核。"
    return {"version":"1.0","content":content,"example":list_strategy_data()[0]["source"] if list_strategy_data() else ""}
