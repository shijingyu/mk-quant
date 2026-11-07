import json
import os
import secrets
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from backend import db, storage
from backend.app import app, overview_data
from backend.security import encrypt_secret, decrypt_secret
from backend.worker import run_experiment, runtime_tick, ensure_market_cache


@pytest.fixture
def client(mysql_database,monkeypatch):
    monkeypatch.setenv("MK_DEMO","1")
    monkeypatch.setenv("MK_DEMO_PASSWORD",secrets.token_urlsafe(24))
    with TestClient(app) as c:
        response=c.post("/api/login",json={"username":"admin","password":os.environ["MK_DEMO_PASSWORD"]})
        assert response.status_code==200
        c.headers["X-MK-CSRF"]=response.json()["csrf_token"]
        yield c


def new_paper(client,phone="13900000000"):
    r=client.post("/api/accounts",json={"name":"测试客户","phone":phone,"password":"test-password-123","mode":"paper","initial_equity":1000,"strategy_id":"momentum","symbols":["BTCUSDT"],"config":{"fast_period":2,"slow_period":4}})
    assert r.status_code==201,r.text
    return r.json()


def test_demo_requires_local_password_and_does_not_reset_existing(mysql_database, monkeypatch):
    monkeypatch.delenv("MK_DEMO_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MK_DEMO_PASSWORD"):
        db.seed_demo()
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    command = [sys.executable, "-m", "backend.worker", "init-demo", "--generate-password"]
    first = subprocess.run(command, capture_output=True, text=True, check=True)
    generated = json.loads(first.stdout)
    assert generated["created"] and len(generated["password"]) >= 24
    with db.connect() as con:
        original = con.execute("SELECT password FROM users WHERE username='admin'").fetchone()[0]
    second = subprocess.run(command, capture_output=True, text=True, check=True)
    assert generated["password"] not in second.stdout
    with db.connect() as con:
        assert con.execute("SELECT password FROM users WHERE username='admin'").fetchone()[0] == original


def test_seed_and_csrf_cookie(client):
    r=client.get("/api/bootstrap")
    assert r.status_code==200
    body=r.json()
    assert body["csrf_token"]
    assert len(body["accounts"])==5
    assert all(a["status"]=="stopped" and a["source"]=="demo" for a in body["accounts"])
    assert '"credentials":' not in json.dumps(body)
    old=client.headers.pop("X-MK-CSRF")
    assert client.post("/api/logout").status_code==403
    client.headers["X-MK-CSRF"]=old
    assert client.post("/api/logout",headers={"Origin":"https://evil.invalid"}).status_code==403
    login=client.post("/api/login",json={"username":"admin","password":os.environ["MK_DEMO_PASSWORD"]})
    cookie=login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_client_cannot_read_other_route_or_admin(client):
    accounts=client.get("/api/accounts").json()
    login=client.post("/api/login",json={"username":"demo1","password":os.environ["MK_DEMO_PASSWORD"]}).json()
    client.headers["X-MK-CSRF"]=login["csrf_token"]
    own=client.get("/api/accounts").json()
    assert len(own)==1
    foreign=next(a for a in accounts if a["id"]!=own[0]["id"])
    assert client.get(f"/api/client/{foreign['route']}").status_code==404
    assert client.get("/api/strategies").status_code==403
    assert client.post("/api/register",json={}).status_code==404
    assert len(own[0]["route"])==32


def test_account_constraints_and_durable_command(client):
    account=new_paper(client)
    assert account["config"]["allow_short"] is True
    r=client.post("/api/commands",json={"account_ids":[account["id"]],"action":"resume"})
    assert r.status_code==202
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM instances WHERE account_id=?",(account["id"],)).fetchone()[0]==1
        state=con.execute("SELECT * FROM instances WHERE account_id=?",(account["id"],)).fetchone()
        assert state["desired_state"]=="running" and state["observed_state"]=="stopped"
        assert con.execute("SELECT status FROM commands WHERE id=?",(r.json()["commands"][0]["id"],)).fetchone()[0]=="pending"
    assert client.patch(f"/api/accounts/{account['id']}",json={"config":{"fast_period":3}}).status_code==409
    assert client.post("/api/commands",json={"account_ids":[a["id"] for a in client.get("/api/accounts").json()],"action":"pause"}).status_code==422


def test_fence_expiry_and_single_owner(client):
    account=new_paper(client)
    one=db.acquire_lease(account["id"],"worker-one")
    assert db.execution_authorized(account["id"],"worker-one",one)
    assert db.acquire_lease(account["id"],"worker-two") is None
    with db.connect() as con:
        con.execute("UPDATE instances SET lease_until=? WHERE account_id=?",(time.time()-1,account["id"]))
    two=db.acquire_lease(account["id"],"worker-two")
    assert two>one
    assert not db.execution_authorized(account["id"],"worker-one",one)
    assert db.execution_authorized(account["id"],"worker-two",two)


def experiment_body():
    return {"strategy_id":"momentum","dataset_id":"demo-1m","parameters":{"fast_period":4,"slow_period":12},"symbols":["BTCUSDT"],"market":"futures","initial_capital":1000,"fee_rate":.0004,"slippage_bps":2,"train_start":"2026-09-01","train_end":"2026-09-02","validation_start":"2026-09-02","validation_end":"2026-09-03","seed":42}


def test_split_backtest_job_results_and_export(client):
    body=experiment_body()
    body["symbols"]=["BTCUSDT","ETHUSDT"]
    response=client.post("/api/experiments",json=body)
    assert response.status_code==202,response.text
    id=response.json()["id"]
    run_experiment(id)
    result=client.get(f"/api/experiments/{id}").json()
    assert result["status"]=="completed",result.get("error")
    assert result["progress"]==100 and not result["incomplete"]
    assert result["metrics"]["total_trades"]>0
    assert result["validation_metrics"]["final_equity"]>0
    assert result["provenance"]["data_hash"]
    assert result["provenance"]["symbol_filters"]["source"]=="synthetic_documented_fixture"
    assert result["provenance"]["base_interval"]=="1m"
    assert result["provenance"]["timeframes"]["primary"]=="1h"
    assert set(result["symbol_results"])==set(body["symbols"])
    for symbol,item in result["symbol_results"].items():
        assert item["allocation"]=={"method":"equal_initial_capital","initial_capital":500}
        assert item["metrics"]["initial_equity"]==500
        assert len(item["curve"])==len(result["curve"])
        assert len(item["validation_curve"])==len(result["validation_curve"])
    for index,point in enumerate(result["curve"]):
        assert sum(item["curve"][index]["equity"] for item in result["symbol_results"].values())==pytest.approx(point["equity"])
    assert result["curve"][-1]["timestamp"]<result["validation_curve"][0]["timestamp"]
    with db.connect() as con:
        persisted=db.parse(con.execute("SELECT result FROM experiments WHERE id=?",(id,)).fetchone()[0])
    assert "orders" not in persisted and "decisions" not in persisted
    assert all(len(item["curve"])<=600 for item in persisted["symbol_results"].values())
    assert persisted["artifacts"]["orders"]["count"]==len(result["orders"])
    assert len(client.get("/api/experiments").json()[0]["curve"])<=600
    exported=client.get(f"/api/experiments/{id}/export?format=csv").text
    assert "split,timestamp,equity,symbol" in exported and "BTCUSDT" in exported and "ETHUSDT" in exported and "TOTAL" in exported
    interactive=client.get(f"/api/experiments/{id}?view=interactive").json()
    assert len(interactive["curve"])<=600 and "decisions" not in interactive and "orders" not in interactive
    assert interactive["detail_pagination"]["orders"]["total"]==len(result["orders"])
    assert all(len(item["curve"])<=600 for item in interactive["symbol_results"].values())
    page=client.get(f"/api/experiments/{id}/records/decisions?offset=1&limit=2").json()
    assert page["rows"]==result["decisions"][1:3] and page["total"]==len(result["decisions"]) and page["has_more"]
    subset=[item for item in result["orders"] if item["symbol"]=="BTCUSDT" and item["split"]=="train"]
    page=client.get(f"/api/experiments/{id}/records/orders?symbol=BTCUSDT&split=train&offset=1&limit=2").json()
    assert page["rows"]==subset[1:3] and page["total"]==len(subset)
    assert client.get(f"/api/experiments/{id}/records/decisions?symbol=BTCUSDT&split=validation").json()["total"] > 0
    assert client.get(f"/api/experiments/{id}/records/orders?limit=101").status_code==422
    assert client.get(f"/api/experiments/{id}/records/orders?offset=-1").status_code==422
    assert client.get(f"/api/experiments/{id}/export?format=json").json()==result
    body["validation_start"]="2026-09-09"
    assert client.post("/api/experiments",json=body).status_code==422


def test_cancel_and_retry(client):
    id=client.post("/api/experiments",json=experiment_body()).json()["id"]
    assert client.post(f"/api/experiments/{id}/cancel").status_code==200
    assert client.get(f"/api/experiments/{id}").json()["status"]=="cancelled"
    new=client.post(f"/api/experiments/{id}/retry")
    assert new.status_code==202 and new.json()["id"]!=id


def test_independent_worker_completes_persisted_queue(client):
    id=client.post("/api/experiments",json=experiment_body()).json()["id"]
    result=subprocess.run([sys.executable,"-m","backend.worker","backtest","--once"],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=client.get(f"/api/experiments/{id}").json()
    assert report["status"]=="completed",report.get("error")
    assert report["worker_pid"]!=os.getpid()


def test_uploaded_source_never_executes(client):
    source=client.get("/api/strategies").json()[0]["source"]
    upload=client.post("/api/strategies",json={"source":source})
    assert upload.status_code==201
    assert upload.json()["trusted"] is True
    body=experiment_body();body["strategy_id"]=upload.json()["id"]
    assert client.post("/api/experiments",json=body).status_code==202
    assert client.post("/api/strategies",json={"source":"import os\nos.system('touch /tmp/unsafe')"}).status_code==422


def test_experiment_pins_immutable_source_version(client):
    experiment=client.post("/api/experiments",json=experiment_body()).json()
    with db.connect() as con:
        version=con.execute("SELECT source FROM strategy_versions WHERE id=?",(experiment["provenance"]["strategy_version_id"],)).fetchone()
        assert version
        con.execute("UPDATE strategies SET source=? WHERE id='momentum'",("raise RuntimeError('mutable source must not execute')",))
    run_experiment(experiment["id"])
    assert client.get(f"/api/experiments/{experiment['id']}").json()["status"]=="completed"


def test_dataset_uses_parquet_and_backfill_is_repeatable(client):
    rows=[{"timestamp":1788220800000+i*60000,"open":100,"high":102,"low":99,"close":101,"volume":0} for i in range(3)]
    response=client.post("/api/datasets",json={"symbols":["BTCUSDT"],"market":"futures","interval":"1m","candles":{"BTCUSDT":rows}})
    assert response.status_code==201,response.text
    dataset=response.json()
    assert dataset["storage"]=="parquet" and dataset["count"]==3
    with db.connect() as con:
        raw=con.execute("SELECT candles FROM datasets WHERE id=?",(dataset["id"],)).fetchone()[0]
    assert storage.is_manifest(db.parse(raw))
    assert storage.read_candles(raw)["BTCUSDT"][0]["volume"]==0
    storage.backfill_artifacts()
    assert storage.backfill_artifacts()=={"datasets":0,"experiments":0}


def test_minute_import_rejects_coarse_mislabeled_and_gapped_rows(client):
    row={"timestamp":1788220800000,"open":100,"high":102,"low":99,"close":101,"volume":1}
    for interval,stamps in (("1h",[0,60000]),("1m",[0,3600000]),("1m",[0,60000,180000]),("1m",[1,60001])):
        response=client.post("/api/datasets",json={"symbols":["BTCUSDT"],"market":"futures","interval":interval,
            "candles":{"BTCUSDT":[dict(row,timestamp=row["timestamp"]+offset) for offset in stamps]}})
        assert response.status_code==422,response.text


def test_new_backtest_rejects_legacy_dataset_but_preserves_existing_reports(client):
    with db.connect() as con:
        con.execute("UPDATE datasets SET `interval`='30m' WHERE id='demo-1m'")
    response=client.post("/api/experiments",json=experiment_body())
    assert response.status_code==422 and "1 分钟" in response.text
    existing=client.get('/api/experiments/demo-exp-1')
    assert existing.status_code==200 and existing.json()["status"]=="completed"


def test_paper_fills_orders_and_audit_commit_together(client,monkeypatch):
    account=new_paper(client)
    command=client.post("/api/commands",json={"account_ids":[account["id"]],"action":"resume"}).json()["commands"][0]
    # The builtin signal runs on complete 1h bars, while execution remains 1m.
    # Prime five complete hours and advance just one minute for the next fill.
    tick=int(time.time()//3600)*3600000
    clock=tick/1000+5
    monkeypatch.setattr(time,"time",lambda:clock)
    bars=[{"timestamp":tick-(300-i)*60000,"open":100+i,"high":102+i,"low":99+i,"close":101+i,"volume":100} for i in range(300)]
    rules={"tick_size":"0.01","step_size":"0.001","min_qty":"0.001","min_notional":"5"}
    with db.connect() as con:
        con.execute("INSERT INTO market_cache VALUES(?,?,'1m',?,?,?)",("futures","BTCUSDT",db.dump(bars),db.dump(rules),time.time()))
    runtime_tick(account["id"],"fill-test")
    clock+=61
    bars.append({"timestamp":tick,"open":400,"high":402,"low":399,"close":401,"volume":100})
    with db.connect() as con:
        con.execute("UPDATE market_cache SET candles=?,fetched_at=?",(db.dump(bars),time.time()))
    runtime_tick(account["id"],"fill-test")
    runtime_tick(account["id"],"fill-test")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM orders WHERE account_id=?",(account["id"],)).fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM fills WHERE account_id=?",(account["id"],)).fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM ledger WHERE account_id=? AND kind='fill'",(account["id"],)).fetchone()[0]==1
        audits=con.execute("SELECT action,payload FROM audit_records").fetchall()
        assert {"account.create","command.request","command.complete"} <= {row["action"] for row in audits}
        assert "test-password-123" not in json.dumps([dict(row) for row in audits])


def test_paper_worker_ack_and_no_fabricated_live_data(client):
    account=new_paper(client)
    client.post("/api/commands",json={"account_ids":[account["id"]],"action":"resume"})
    runtime_tick(account["id"],"test-runtime")
    assert client.get(f"/api/client/{account['route']}").json()["status"]=="starting"
    ensure_market_cache()
    now=int(time.time()//60)*60000
    candles=[{"timestamp":now-(10-i)*60000,"open":100+i,"high":102+i,"low":99+i,"close":101+i,"volume":100} for i in range(10)]
    rules={"tick_size":"0.01","step_size":"0.001","min_qty":"0.001","min_notional":"5"}
    with db.connect() as con:
        con.execute("INSERT INTO market_cache VALUES(?,?,'1m',?,?,?)",("futures","BTCUSDT",db.dump(candles),db.dump(rules),time.time()))
    runtime_tick(account["id"],"test-runtime")
    report=client.get(f"/api/client/{account['route']}").json()
    assert report["status"]=="running"
    assert report["orders"]==[]  # First closed candle only generates next-bar intent.
    command=client.post("/api/commands",json={"account_ids":[account["id"]],"action":"flatten","confirmed":True})
    assert command.status_code==202
    runtime_tick(account["id"],"test-runtime")
    assert client.get(f"/api/client/{account['route']}").json()["status"]=="stopped"
    assert client.patch(f"/api/accounts/{account['id']}",json={"initial_equity":2000}).status_code==409


def test_replaced_actor_cannot_acknowledge_stop_without_market_data(client,monkeypatch):
    account=new_paper(client)
    command=client.post("/api/commands",json={"account_ids":[account["id"]],"action":"flatten","confirmed":True}).json()["commands"][0]
    original_acquire=db.acquire_lease
    def replaced_lease(account_id,owner):
        fence=original_acquire(account_id,owner)
        with db.connect() as con:
            con.execute("UPDATE instances SET owner='replacement',fence=fence+1 WHERE account_id=?",(account_id,))
        return fence
    monkeypatch.setattr(db,"acquire_lease",replaced_lease)
    assert runtime_tick(account["id"],"expired-worker") is True
    with db.connect() as con:
        assert con.execute("SELECT status FROM commands WHERE id=?",(command["id"],)).fetchone()[0]=="pending"
        assert con.execute("SELECT owner FROM instances WHERE account_id=?",(account["id"],)).fetchone()[0]=="replacement"


def test_edit_pre_run_account_and_credentials(client):
    account=new_paper(client)
    response=client.patch(f"/api/accounts/{account['id']}",json={"name":"更新客户","phone":"13900000002","initial_equity":2000,"config":{"fast_period":3,"slow_period":10}})
    assert response.status_code==200,response.text
    assert response.json()["name"]=="更新客户"
    assert response.json()["equity"]==2000
    assert response.json()["config_revision"]==2
    assert client.post("/api/login",json={"username":"13900000002","password":"test-password-123"}).status_code==200


def test_secret_encryption_and_live_gate(client,monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MK_MASTER_KEY",Fernet.generate_key().decode())
    secret=encrypt_secret("sensitive-secret")
    assert secret!="sensitive-secret" and decrypt_secret(secret)=="sensitive-secret"
    response=client.post("/api/accounts",json={"name":"live test","phone":"13900000001","password":"test-password-123","mode":"live","strategy_id":"momentum","symbols":["BTCUSDT"],"api_key":"key","api_secret":"secret"})
    assert response.status_code==201,response.text
    account=response.json()
    assert account["equity"] is None and account["data_source"]=="unverified"
    assert client.post("/api/commands",json={"account_ids":[account["id"]],"action":"resume"}).status_code==409


def test_aggregate_curve_carries_last_equity():
    base={"initial_equity":100,"margin_used":0,"unrealized_pnl":0,"today_pnl":0,"month_pnl":0,"source":"local","status":"stopped"}
    a={**base,"id":"a","equity":110,"curve":[{"timestamp":1,"equity":100},{"timestamp":3,"equity":110}]}
    b={**base,"id":"b","equity":120,"curve":[{"timestamp":2,"equity":120}]}
    assert [p["equity"] for p in overview_data([a,b])["curve"]]==[200,220,230]


def test_cloned_experiment_cannot_override_persisted_identity(client):
    original=client.post("/api/experiments",json=experiment_body()).json()
    copied=client.get(f"/api/experiments/{original['id']}").json()
    copied.update(status="completed",progress=100,metrics={"final_equity":999999})
    new=client.post("/api/experiments",json=copied).json()
    result=client.get(f"/api/experiments/{new['id']}").json()
    assert result["id"]==new["id"]!=original["id"]
    assert result["status"]=="queued" and result["progress"]==0
    assert result["metrics"]=={}


def test_batch_parameters_validate_atomically(client):
    before=len(client.get("/api/experiments").json())
    body=experiment_body()
    body["parameter_grid"]={"fast_period":[4,-1]}
    assert client.post("/api/experiments",json=body).status_code==422
    assert len(client.get("/api/experiments").json())==before
    body["parameter_grid"]={"fast_period":[4,6]}
    result=client.post("/api/experiments",json=body)
    assert result.status_code==202 and len(result.json()["experiments"])==2
    body=experiment_body();body["leverage"]=10000
    assert client.post("/api/experiments",json=body).status_code==422


def test_create_admin_noninteractive_is_non_destructive(mysql_database):
    env={**os.environ,"MK_DEMO":"0"}
    command=[sys.executable,"-m","backend.worker","create-admin","--username","owner","--generate-password"]
    first=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
    assert first.returncode==0,first.stderr
    generated=json.loads(first.stdout)
    assert generated["username"]=="owner" and len(generated["password"])>=24
    second=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
    assert second.returncode!=0 and not second.stdout
    assert generated["password"] not in second.stderr


def test_running_job_cancellation_terminates_child(client):
    from backend.worker import _monitor_job
    job_id=client.post("/api/experiments",json=experiment_body()).json()["id"]
    with db.connect() as con:
        con.execute("UPDATE experiments SET status='running',cancel_requested=1 WHERE id=?",(job_id,))
    child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])
    try:
        _monitor_job(child,job_id,"cancel-test",time.monotonic())
        assert child.poll() is not None
        assert client.get(f"/api/experiments/{job_id}").json()["status"]=="cancelled"
    finally:
        if child.poll() is None:
            child.kill();child.wait()


def test_strategy_deadline_does_not_hang_other_accounts():
    from backend.worker import strategy_deadline
    with pytest.raises(TimeoutError):
        with strategy_deadline(.02):
            time.sleep(1)
    # The timer and prior handler are restored even on failure.
    with strategy_deadline(.02):
        assert 2+2==4


def test_experiment_enqueue_does_not_load_candle_payloads(client, monkeypatch):
    monkeypatch.setattr(storage, 'read_candles', lambda *a, **k: pytest.fail('API must use streaming timestamp validation'))
    result = client.post('/api/experiments', json=experiment_body())
    assert result.status_code == 202, result.text


def test_backtest_memory_failure_has_actionable_result(client, monkeypatch):
    result = client.post('/api/experiments', json=experiment_body())
    assert result.status_code == 202
    def exhausted(*args, **kwargs):
        raise MemoryError()
    from backend.research import CandleWindow
    monkeypatch.setattr(CandleWindow, 'prepare', exhausted)
    run_experiment(result.json()['id'])
    report = client.get('/api/experiments/' + result.json()['id']).json()
    assert report['status'] == 'failed'
    assert '单任务内存预算' in report['error']
    assert client.get('/healthz').status_code == 200


def test_correlation_admin_snapshot(client,monkeypatch):
    from backend import correlation
    monkeypatch.setattr(correlation,'snapshot',lambda quote='USDT':{'windows':{},'refreshing':True,'updated_at':None,'stale':True})
    response=client.get('/api/tools/correlation')
    assert response.status_code==200 and response.json()['refreshing']


def test_decision_symbol_outside_experiment_is_rejected(client):
    response=client.post('/api/experiments',json=experiment_body())
    assert response.status_code==202
    result=client.get('/api/experiments/'+response.json()['id']+'/records/decisions?symbol=INVALIDUSDT')
    assert result.status_code==422


def test_correlation_quote_and_detail_validation(client,monkeypatch):
    from backend import correlation
    monkeypatch.setattr(correlation,'snapshot',lambda quote='USDT':{'quote':quote,'windows':{}})
    assert client.get('/api/tools/correlation?quote=USDC').json()['quote']=='USDC'
    assert client.get('/api/tools/correlation?quote=USD').status_code==422
    monkeypatch.setattr(correlation,'detail',lambda quote,window,a,b:dict(quote=quote,window=window,a=a,b=b))
    response=client.get('/api/tools/correlation/pair?quote=USDC&window=90d&a=BTCUSDC&b=ETHUSDC')
    assert response.status_code==200 and response.json()['window']=='90d'
    assert client.get('/api/tools/correlation/pair?quote=USDC&window=invalid&a=BTCUSDC&b=ETHUSDC').status_code==422
    def unavailable(*args):raise ValueError('所选配对暂无完整行情')
    monkeypatch.setattr(correlation,'detail',unavailable)
    assert client.get('/api/tools/correlation/pair?a=BTCUSDC&b=ETHUSDC').status_code==422
    assert client.post('/api/logout').status_code==200
    assert client.get('/api/tools/correlation?quote=USDC').status_code==401


def test_account_leverage_persists_and_enforces_strategy_limits(client):
    account=new_paper(client)
    path=f"/api/accounts/{account['id']}"
    assert account['configured_leverage']==account['leverage']==1
    for invalid in (0,6,1.5,True,'bad'):
        assert client.patch(path,json={'leverage':invalid}).status_code==422
    response=client.patch(path,json={'leverage':4})
    assert response.status_code==200,response.text
    assert response.json()['configured_leverage']==response.json()['leverage']==4
    assert 'leverage' not in response.json()['config']
    with db.connect() as con:
        assert con.execute('SELECT config_revision FROM instances WHERE account_id=?',(account['id'],)).fetchone()[0]==2
        con.execute('UPDATE accounts SET metrics=? WHERE id=?',(db.dump({'equity':1000,'positions':[{'symbol':'BTCUSDT'}]}),account['id']))
    assert client.patch(path,json={'leverage':3}).status_code==409
    with db.connect() as con:
        con.execute('UPDATE accounts SET metrics=? WHERE id=?',(db.dump({'equity':1000,'open_orders':[{'symbol':'BTCUSDT'}]}),account['id']))
    assert client.patch(path,json={'leverage':3}).status_code==409
    with db.connect() as con:
        con.execute('UPDATE accounts SET metrics=? WHERE id=?',(db.dump({'equity':1000}),account['id']))
    client.post('/api/commands',json={'account_ids':[account['id']],'action':'resume'})
    assert client.patch(path,json={'leverage':3}).status_code==409
    assert next(a for a in client.get('/api/accounts').json() if a['id']==account['id'])['configured_leverage']==4


def test_account_creation_leverage_and_spot_constraint(client):
    body={'name':'Leverage test','phone':'13900000001','password':'test-password-123','mode':'paper','initial_equity':1000,'strategy_id':'momentum','symbols':['BTCUSDT'],'config':{},'leverage':3}
    response=client.post('/api/accounts',json=body)
    assert response.status_code==201,response.text
    assert response.json()['configured_leverage']==3
    spot=next(s for s in client.get('/api/strategies').json() if s['market']=='spot')
    body.update(phone='13900000002',strategy_id=spot['id'],leverage=2)
    assert client.post('/api/accounts',json=body).status_code==422
    body['leverage']=1
    assert client.post('/api/accounts',json=body).status_code==201


def test_live_account_inspection_uses_selected_leverage(client,monkeypatch):
    from cryptography.fernet import Fernet
    from backend.binance import BinanceClient
    monkeypatch.setenv('MK_MASTER_KEY',Fernet.generate_key().decode())
    captures=[]
    def inspect(self,manifest,symbols):
        captures.append((manifest['account']['leverage_min'],manifest['account']['leverage_max'],symbols))
        return {'ok':True,'checks':[],'adjustments':[]}
    monkeypatch.setattr(BinanceClient,'inspect_account',inspect)
    response=client.post('/api/accounts',json={'name':'Live check','phone':'13900000003','password':'test-password-123','mode':'live','initial_equity':1000,'strategy_id':'momentum','symbols':['BTCUSDT'],'config':{},'leverage':5,'api_key':'test-key','api_secret':'test-secret'})
    assert response.status_code==201,response.text
    result=client.post(f"/api/accounts/{response.json()['id']}/check",json={})
    assert result.json()['ok'],result.text
    assert captures==[(5,5,['BTCUSDT'])]


def test_strategy_delete_blocks_bindings_and_preserves_history(client):
    assert client.delete('/api/strategies/momentum').status_code==409
    source=next(s['source'] for s in client.get('/api/strategies').json() if s['id']=='momentum')
    strategy=client.post('/api/strategies',json={'source':source}).json()
    sid=strategy['id']
    preset=client.post('/api/presets',json={'strategy_id':sid,'name':'Archive test','parameters':{}})
    assert preset.status_code==201,preset.text
    body=experiment_body();body['strategy_id']=sid
    response=client.post('/api/experiments',json=body)
    assert response.status_code==202,response.text
    experiment=response.json()
    assert client.delete(f'/api/strategies/{sid}').status_code==409
    run_experiment(experiment['id'])
    before=client.get(f"/api/experiments/{experiment['id']}").json()
    assert before['status']=='completed',before.get('error')
    assert client.delete(f'/api/strategies/{sid}').status_code==200
    assert client.delete(f'/api/strategies/{sid}').status_code==200
    assert sid not in {s['id'] for s in client.get('/api/strategies').json()}
    assert preset.json()['id'] not in {s['id'] for s in client.get('/api/presets').json()}
    assert client.get(f"/api/experiments/{experiment['id']}").json()==before
    assert client.post('/api/experiments',json=body).status_code==404
    assert client.post('/api/presets',json={'strategy_id':sid,'parameters':{}}).status_code==404
    with db.connect() as con:
        assert con.execute('SELECT 1 FROM strategy_versions WHERE strategy_id=?',(sid,)).fetchone()
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='strategy.delete' AND resource_id=?",(sid,)).fetchone()[0]==1
    db.init_db()
    assert sid not in {s['id'] for s in client.get('/api/strategies').json()}


def test_deleted_builtin_does_not_reappear_after_restart(client):
    sid=next(s['id'] for s in client.get('/api/strategies').json() if s['market']=='spot')
    with db.connect() as con:
        con.execute("UPDATE accounts SET strategy_id='momentum' WHERE strategy_id=?",(sid,))
    assert client.delete(f'/api/strategies/{sid}').status_code==200
    db.init_db()
    assert sid not in {s['id'] for s in client.get('/api/strategies').json()}
    login=client.post('/api/login',json={'username':'demo1','password':os.environ['MK_DEMO_PASSWORD']}).json()
    client.headers['X-MK-CSRF']=login['csrf_token']
    assert client.delete('/api/strategies/momentum').status_code==403


def test_account_delete_revokes_access_execution_and_keeps_history(client):
    account=new_paper(client)
    aid=account['id']
    old_fence=db.acquire_lease(aid,'old-worker')
    with db.connect() as con:
        con.execute("INSERT INTO ledger(id,account_id,kind,payload,created_at) VALUES(?,?,?,?,?)",(db.uid(),aid,'test-audit','{}',db.now()))
    result=client.delete(f'/api/accounts/{aid}')
    assert result.status_code==200,result.text
    assert aid not in {a['id'] for a in client.get('/api/accounts').json()}
    assert client.get(f"/api/client/{account['route']}").status_code==404
    assert client.patch(f'/api/accounts/{aid}',json={'name':'revive'}).status_code==404
    assert client.post('/api/commands',json={'account_ids':[aid],'action':'resume'}).status_code==404
    assert client.post(f'/api/accounts/{aid}/check',json={}).status_code==404
    assert not db.execution_authorized(aid,'old-worker',old_fence)
    assert db.acquire_lease(aid,'new-worker') is None
    assert client.delete(f'/api/accounts/{aid}').status_code==200
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM ledger WHERE account_id=?',(aid,)).fetchone()[0]==1
        assert con.execute("SELECT 1 FROM audit_records WHERE action='account.delete' AND resource_id=?",(aid,)).fetchone()
    login=client.post('/api/login',json={'username':'13900000000','password':'test-password-123'})
    assert login.status_code==401


def test_account_delete_refuses_positions_orders_and_pending_commands(client):
    account=new_paper(client);aid=account['id']
    for metrics in ({'positions':[{}]},{'open_orders':[{}]},{'position_count':1},{'open_order_count':1}):
        with db.connect() as con:con.execute('UPDATE accounts SET metrics=? WHERE id=?',(db.dump(metrics),aid))
        assert client.delete(f'/api/accounts/{aid}').status_code==409
    with db.connect() as con:con.execute("UPDATE accounts SET metrics='{}' WHERE id=?",(aid,))
    client.post('/api/commands',json={'account_ids':[aid],'action':'resume'})
    assert client.delete(f'/api/accounts/{aid}').status_code==409
    with db.connect() as con:con.execute("UPDATE instances SET desired_state='stopped',observed_state='stopped' WHERE account_id=?",(aid,))
    assert client.delete(f'/api/accounts/{aid}').status_code==409


def test_protocol_is_neutral_and_independent_of_strategy_catalog(client):
    from backend.strategy import validate_strategy_source
    from backend.protocol_template import PROTOCOL_TEMPLATE
    result=client.get('/api/protocol').json()
    assert result['example']==PROTOCOL_TEMPLATE
    validate_strategy_source(PROTOCOL_TEMPLATE)
    namespace={};exec(PROTOCOL_TEMPLATE,namespace)
    assert namespace['Strategy']().on_bar({})==[]
    assert '均线做多' not in result['content']
    assert '"properties": {}' in result['example']


def test_live_delete_requires_recent_exchange_flat_check(client,monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('MK_MASTER_KEY',Fernet.generate_key().decode())
    account=client.post('/api/accounts',json={'name':'Delete live','phone':'13900000004','password':'test-password-123','mode':'live','initial_equity':1000,'strategy_id':'momentum','symbols':['BTCUSDT'],'config':{},'api_key':'test-key','api_secret':'test-secret'}).json()
    aid=account['id']
    assert client.delete(f'/api/accounts/{aid}').status_code==409
    checked={'checked_at':db.now(),'checks':[{'name':key,'status':'pass'} for key in ('existing_positions','existing_orders')],'positions':[],'orders':[]}
    with db.connect() as con:con.execute('UPDATE accounts SET check_result=? WHERE id=?',(db.dump(checked),aid))
    assert client.delete(f'/api/accounts/{aid}').status_code==200
    with db.connect() as con:assert con.execute('SELECT credentials FROM accounts WHERE id=?',(aid,)).fetchone()[0] is None


def test_complete_protocol_includes_valid_neutral_skeleton_and_contract(client):
    import re
    from pathlib import Path
    from backend.protocol_template import PROTOCOL_VERSION, PROTOCOL_TEMPLATE
    from backend.strategy import load_strategy
    response=client.get('/api/protocol')
    assert response.status_code==200,response.text
    payload=response.json()
    doc=(Path(__file__).resolve().parent.parent/'docs'/'STRATEGY_PROTOCOL.md').read_text()
    assert payload['version']==PROTOCOL_VERSION
    assert payload['content']==doc
    skeleton=re.search(r'```python\n(.*?)\n```',doc,re.S).group(1)
    assert skeleton.strip()==PROTOCOL_TEMPLATE.strip()==payload['example'].strip()
    assert load_strategy(skeleton).on_bar({})==[]
    for contract in ('execution.model','market_next_open_v1','maker_grid_v1','minimum','default','timeframe_history','decision_time','execution_events','client_order_id','reduce_only','stop_limit_price','JSON','白名单','to-do'):
        assert contract in doc
    assert '持续模拟取声明的最小值' not in doc
    assert '持续模拟优先使用账户保存的杠杆' in doc
    assert 'grid-example-L-1-1' not in doc


@pytest.mark.parametrize('failure',['missing','incomplete'])
def test_protocol_never_returns_partial_copy_when_document_is_unavailable(client,monkeypatch,failure):
    from pathlib import Path
    original=Path.read_text
    def damaged(path,*args,**kwargs):
        if path.name=='STRATEGY_PROTOCOL.md':
            if failure=='missing':raise FileNotFoundError('test missing protocol')
            return '# Partial document'
        return original(path,*args,**kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path,'read_text',damaged)
        response=client.get('/api/protocol')
        assert response.status_code==503
        assert 'example' not in response.json()
    assert client.get('/api/protocol').status_code==200
