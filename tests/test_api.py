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
    return {"strategy_id":"momentum","dataset_id":"demo-30m","parameters":{"fast_period":4,"slow_period":12},"symbols":["BTCUSDT"],"market":"futures","initial_capital":1000,"fee_rate":.0004,"slippage_bps":2,"train_start":"2026-09-01","train_end":"2026-09-10","validation_start":"2026-09-10","validation_end":"2026-09-15","seed":42}


def test_split_backtest_job_results_and_export(client):
    body=experiment_body()
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
    assert result["curve"][-1]["timestamp"]<result["validation_curve"][0]["timestamp"]
    with db.connect() as con:
        persisted=db.parse(con.execute("SELECT result FROM experiments WHERE id=?",(id,)).fetchone()[0])
    assert "orders" not in persisted and "decisions" not in persisted
    assert persisted["artifacts"]["orders"]["count"]==len(result["orders"])
    assert len(client.get("/api/experiments").json()[0]["curve"])<=600
    assert "split,timestamp,equity" in client.get(f"/api/experiments/{id}/export?format=csv").text
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


def test_paper_fills_orders_and_audit_commit_together(client,monkeypatch):
    account=new_paper(client)
    command=client.post("/api/commands",json={"account_ids":[account["id"]],"action":"resume"}).json()["commands"][0]
    tick=int(time.time()//60)*60000
    bars=[{"timestamp":tick-(10-i)*60000,"open":100+i,"high":102+i,"low":99+i,"close":101+i,"volume":100} for i in range(10)]
    rules={"tick_size":"0.01","step_size":"0.001","min_qty":"0.001","min_notional":"5"}
    with db.connect() as con:
        con.execute("INSERT INTO market_cache VALUES(?,?,'1m',?,?,?)",("futures","BTCUSDT",db.dump(bars),db.dump(rules),time.time()))
    runtime_tick(account["id"],"fill-test")
    later=time.time()+61
    monkeypatch.setattr(time,"time",lambda:later)
    bars.append({"timestamp":tick,"open":110,"high":112,"low":109,"close":111,"volume":100})
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
