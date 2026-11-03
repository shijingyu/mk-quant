"""Durable download queue integration tests against the same MySQL dialect as production."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from backend import db, downloads, storage
from backend.app import app, store_dataset
from backend.binance import BinanceClient, BinanceError
from backend.security import hash_password
from backend import worker


@pytest.fixture
def client(mysql_database):
    password=secrets.token_urlsafe(24)
    with db.connect() as con:
        con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",("download-admin","download-admin","下载测试",None,hash_password(password),"admin",db.now()))
    with TestClient(app) as client:
        login=client.post("/api/login",json={"username":"download-admin","password":password})
        assert login.status_code==200
        client.headers["X-MK-CSRF"]=login.json()["csrf_token"]
        yield client


def body(**changes):
    return {"name":"公开行情测试","market":"futures","symbols":["BTCUSDC"],"interval":"1h","start":"2026-09-01","end":"2026-09-02",**changes}


def candles(start):
    return [{"timestamp":start+i*3600000,"open":100,"high":102,"low":99,"close":101,"volume":10} for i in range(2)]


def fake_klines(self,symbol,interval,start,end,*,progress,max_rows):
    result=candles(start)
    progress({"rows":len(result),"cursor":start+7200000,"end":end})
    return result


def enqueue(client,**changes):
    response=client.post("/api/datasets/download",json=body(**changes))
    assert response.status_code==202,response.text
    return response.json()


def test_enqueue_is_fast_durable_authorized_and_deduplicated(client,monkeypatch):
    monkeypatch.setattr(BinanceClient,"klines",lambda *a,**k:pytest.fail("API must never fetch candles"))
    first=enqueue(client,request_key="request-one")
    assert first["status"]=="queued" and first["progress"]==0 and not first["reused"]
    second=enqueue(client,request_key="request-one")
    assert second["id"]==first["id"] and second["reused"]
    assert enqueue(client)["id"]==first["id"]
    other=client.post("/api/datasets/download",json=body(symbols=["ETHUSDC"]))
    assert other.status_code==409
    assert other.json()["detail"]["job"]["id"]==first["id"]
    assert client.get("/api/bootstrap").json()["download_jobs"][0]["id"]==first["id"]
    assert client.get("/api/datasets/download-jobs").json()[0]["id"]==first["id"]
    assert client.get("/api/datasets/download-jobs/missing").status_code==404
    assert "owner" not in first and "lease_until" not in first
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/datasets/download-jobs").status_code==401
        assert anonymous.post("/api/datasets/download",json=body()).status_code==401
    csrf=client.headers.pop("X-MK-CSRF")
    assert client.post("/api/datasets/download",json=body()).status_code==403
    client.headers["X-MK-CSRF"]=csrf


def test_concurrent_submit_has_one_active_job(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs=list(pool.map(lambda index:downloads.enqueue(body(request_key=f"race-{index}"),"download-admin"),range(8)))
    assert len({job["id"] for job in jobs})==1
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM data_download_jobs").fetchone()[0]==1


def test_request_key_future_end_remains_stable(client,monkeypatch):
    original=time.time()
    monkeypatch.setattr(downloads.time,"time",lambda:original)
    first=enqueue(client,start=int((original-7200)*1000),end=int((original+7200)*1000),request_key="future")
    monkeypatch.setattr(downloads.time,"time",lambda:original+2)
    assert enqueue(client,start=int((original-7200)*1000),end=int((original+7200)*1000),request_key="future")["id"]==first["id"]


def test_progress_and_dataset_publication_are_persistent(client,monkeypatch):
    job=enqueue(client,symbols=["BTCUSDC","ETHUSDC"])
    claimed,busy=downloads.claim("research-one")
    assert busy and claimed["attempts"]==1
    seen=[]
    def fetch(self,symbol,interval,start,end,*,progress,max_rows):
        rows=fake_klines(self,symbol,interval,start,end,progress=progress,max_rows=max_rows)
        seen.append(downloads.get_job(job["id"]))
        return rows
    monkeypatch.setattr(BinanceClient,"klines",fetch)
    downloads.run(job["id"],claimed["owner"])
    final=downloads.get_job(job["id"])
    assert final["status"]=="completed",final["error"]
    assert final["progress"]==100 and final["downloaded_bars"]==4
    assert final["completed_symbols"]==2 and final["dataset_id"]==job["id"]
    assert [entry["downloaded_bars"] for entry in seen]==[2,4]
    assert all(entry["status"]=="running" and entry["progress"]<100 for entry in seen)
    with db.connect() as con:
        dataset=con.execute("SELECT * FROM datasets WHERE id=?",(job["id"],)).fetchone()
    assert storage.is_manifest(db.parse(dataset["candles"]))
    assert len(storage.read_candles(dataset["candles"])["BTCUSDC"])==2
    assert downloads.claim("research-two")== (None,False)


def test_web_restart_and_real_isolated_child_complete_same_job(client):
    job=enqueue(client,request_key="survives-web")
    claimed,_=downloads.claim("research-process")
    # A new API lifespan sees the durable queue before an independent child runs.
    with TestClient(app) as fresh:
        fresh.cookies.update(client.cookies)
        assert fresh.get("/api/bootstrap").json()["download_jobs"][0]["id"]==job["id"]
    script='''
import json,sys
from backend import downloads
from backend.binance import BinanceClient

def fixture(self,symbol,interval,start,end,*,progress,max_rows):
    rows=[{"timestamp":start+i*3600000,"open":100,"high":102,"low":99,"close":101,"volume":10} for i in range(2)]
    progress({"rows":2,"cursor":start+7200000,"end":end})
    return rows
BinanceClient.klines=fixture
downloads.run(sys.argv[1],sys.argv[2])
'''
    env={**os.environ,"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","NUMEXPR_NUM_THREADS":"1"}
    result=subprocess.run([sys.executable,"-c",script,job["id"],claimed["owner"]],env=env,preexec_fn=worker._download_limits if os.name=="posix" else None,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    final=downloads.get_job(job["id"])
    assert final["status"]=="completed",final["error"]
    with db.connect() as con:
        assert con.execute("SELECT worker_pid FROM data_download_jobs WHERE id=?",(job["id"],)).fetchone()[0]!=os.getpid()
    assert enqueue(client,request_key="survives-web")["id"]==job["id"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]==1


def test_expired_lease_is_fenced_and_retry_bound_is_finite(client,monkeypatch):
    job=enqueue(client)
    old,_=downloads.claim("first")
    assert downloads.claim("second")== (None,True)
    with db.connect() as con:
        con.execute("UPDATE data_download_jobs SET lease_until=0 WHERE id=?",(job["id"],))
    current,_=downloads.claim("second")
    assert current["attempts"]==2 and current["owner"]!=old["owner"]
    with pytest.raises(downloads.DownloadLeaseLost):
        downloads.progress(job["id"],old["owner"],completed_symbols=0,total_symbols=1,downloaded_bars=2,current_symbol="BTCUSDC",fraction=.2)
    with pytest.raises(downloads.DownloadLeaseLost):
        store_dataset({**body(),"candles":{"BTCUSDC":candles(downloads._timestamp("2026-09-01"))}},"binance_public","download-admin",download_id=job["id"],download_owner=old["owner"])
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]==0
        con.execute("UPDATE data_download_jobs SET lease_until=0,attempts=? WHERE id=?",(downloads.MAX_ATTEMPTS,job["id"]))
    assert downloads.claim("third")== (None,False)
    failed=downloads.get_job(job["id"])
    assert failed["status"]=="failed" and "多次中断" in failed["error"]
    assert enqueue(client)["id"]!=job["id"]


def test_completion_transaction_rolls_back_both_dataset_and_status(client,monkeypatch):
    job=enqueue(client)
    claimed,_=downloads.claim("research")
    monkeypatch.setattr(BinanceClient,"klines",fake_klines)
    original=db.audit
    def broken(con,actor,action,*args):
        if action=="dataset.download.complete":
            raise RuntimeError("injected audit write failure")
        return original(con,actor,action,*args)
    monkeypatch.setattr(db,"audit",broken)
    downloads.run(job["id"],claimed["owner"])
    assert downloads.get_job(job["id"])["status"]=="failed"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='dataset.create'").fetchone()[0]==0
    assert enqueue(client)["id"]!=job["id"]


def test_network_failure_is_terminal_and_has_readable_error(client,monkeypatch):
    job=enqueue(client)
    claimed,_=downloads.claim("research")
    def unavailable(*args,**kwargs):
        raise BinanceError("币安请求频率受限，已进入冷却")
    monkeypatch.setattr(BinanceClient,"klines",unavailable)
    downloads.run(job["id"],claimed["owner"])
    result=client.get("/api/datasets/download-jobs/"+job["id"]).json()
    assert result["status"]=="failed" and "频率受限" in result["error"]
    assert downloads.claim("next")== (None,False)


def test_active_job_is_not_lost_behind_recent_history(client):
    active=enqueue(client)
    with db.connect() as con:
        for index in range(55):
            con.execute("INSERT INTO data_download_jobs(id,request_hash,requested_by,config,status,total_symbols,created_at,updated_at) VALUES(?,?,?,?,'failed',1,?,?)",(db.uid(),"a"*64,"download-admin",db.dump(body()),"9999-01-01",db.now()))
    visible=client.get("/api/bootstrap").json()["download_jobs"]
    assert len(visible)==50 and visible[0]["id"]==active["id"]


def test_limits_validation_and_worker_dispatch(client,monkeypatch):
    invalid=[body(interval="bad"),body(symbols=["BTCUSDC","BTCUSDC"]),body(market="invalid"),body(start="2025-01-01",end="2026-01-01",interval="1m")]
    for config in invalid:
        assert client.post("/api/datasets/download",json=config).status_code==422
    assert downloads.MAX_CANDLES==100000 and downloads.LEASE_SECONDS==180
    assert downloads.timeout_seconds()==900
    monkeypatch.setenv("MK_DOWNLOAD_TIMEOUT_SECONDS","99999")
    assert downloads.timeout_seconds()==3600
    monkeypatch.setenv("MK_DOWNLOAD_TIMEOUT_SECONDS","1")
    assert downloads.timeout_seconds()==30
    job=enqueue(client)
    monkeypatch.setenv("MK_MASTER_KEY","must-not-leak")
    monkeypatch.setenv("MK_ENABLE_LIVE","1")
    def fake_child(command,**kwargs):
        assert command[3]=="download-job" and command[4]==job["id"]
        assert "MK_MASTER_KEY" not in kwargs["env"] and "MK_ENABLE_LIVE" not in kwargs["env"]
        assert kwargs["preexec_fn"] is worker._download_limits
        class FailedProcess:
            def poll(self):return 137
        return FailedProcess()
    monkeypatch.setattr(worker.subprocess,"Popen",fake_child)
    worker.backtest_loop(once=True)
    failed=downloads.get_job(job["id"])
    assert failed["status"]=="failed" and "资源上限" in failed["error"]


def test_child_wall_deadline_bypasses_exchange_retry_chain(client,monkeypatch):
    if not hasattr(downloads.signal,"SIGALRM"):
        pytest.skip("Production worker uses POSIX alarms")
    from backend import binance
    job=enqueue(client)
    claimed,_=downloads.claim("orphaned-coordinator")
    attempts=[]
    def blocked_transport(*args,**kwargs):
        attempts.append(1)
        time.sleep(10)
        pytest.fail("Wall deadline was swallowed")
    # Exercise the real urllib transport and Binance retry wrapper. No monitor
    # or exchange network is involved; an orphan child must still stop itself.
    monkeypatch.setattr(binance,"urlopen",blocked_transport)
    monkeypatch.setattr(binance,"_ssl_context",lambda:None)
    monkeypatch.setattr(downloads,"timeout_seconds",lambda:1)
    started=time.monotonic()
    downloads.run(job["id"],claimed["owner"])
    result=downloads.get_job(job["id"])
    assert time.monotonic()-started<4 and len(attempts)==1
    assert result["status"]=="failed" and "超过任务时限" in result["error"]


@pytest.mark.parametrize("worker_pid",[None,os.getpid()])
def test_restart_waits_for_surviving_backtest_or_pid_registration(client,monkeypatch,worker_pid):
    dataset=store_dataset({**body(),"candles":{"BTCUSDC":candles(downloads._timestamp("2026-09-01"))}},"import","download-admin")
    with db.connect() as con:
        con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at,started_at,worker_pid) VALUES(?,?,?,?,'running','{}','{}',?,?,?)",("surviving-backtest","存活研究任务","momentum",dataset["id"],db.now(),db.now(),worker_pid))
    job=enqueue(client)
    monkeypatch.setattr(worker.subprocess,"Popen",lambda *a,**k:pytest.fail("Must wait for existing research child"))
    worker.backtest_loop(once=True)
    assert downloads.get_job(job["id"])["status"]=="queued"
