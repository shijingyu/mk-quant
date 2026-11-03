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
    return {"name":"公开行情测试","market":"futures","symbols":["BTCUSDC"],"interval":"1m","start":"2026-09-01","end":"2026-09-02",**changes}


def candles(start):
    return [{"timestamp":start+i*60000,"open":100,"high":102,"low":99,"close":101,"volume":10} for i in range(2)]


def fake_klines(self,symbol,interval,start,end,*,progress,on_wait):
    result=candles(start)
    progress({"rows":len(result),"cursor":start+120000,"end":end})
    yield result


def enqueue(client,**changes):
    response=client.post("/api/datasets/download",json=body(**changes))
    assert response.status_code==202,response.text
    return response.json()


def test_enqueue_is_fast_durable_authorized_and_deduplicated(client,monkeypatch):
    monkeypatch.setattr(BinanceClient,"iter_klines",lambda *a,**k:pytest.fail("API must never fetch candles"))
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
    def fetch(self,symbol,interval,start,end,*,progress,on_wait):
        yield from fake_klines(self,symbol,interval,start,end,progress=progress,on_wait=on_wait)
        seen.append(downloads.get_job(job["id"]))
    monkeypatch.setattr(BinanceClient,"iter_klines",fetch)
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
    job=enqueue(client,request_key="survives-web",start="2026-01-01",end="2026-04-01")
    claimed,_=downloads.claim("research-process")
    # A new API lifespan sees the durable queue before an independent child runs.
    with TestClient(app) as fresh:
        fresh.cookies.update(client.cookies)
        assert fresh.get("/api/bootstrap").json()["download_jobs"][0]["id"]==job["id"]
    script='''
import json,sys,resource
from backend import downloads
from backend.binance import BinanceClient

def fixture(self,symbol,interval,start,end,*,progress,on_wait):
    for first in range(0,100005,1000):
        rows=[{"timestamp":start+i*60000,"open":100,"high":102,"low":99,"close":101,"volume":10} for i in range(first,min(first+1000,100005))]
        progress({"rows":first+len(rows),"cursor":rows[-1]["timestamp"]+60000,"end":end})
        yield rows
BinanceClient.iter_klines=fixture
downloads.run(sys.argv[1],sys.argv[2])
print(json.dumps({"peak_rss_kb":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,"downloaded_bars":downloads.get_job(sys.argv[1])["downloaded_bars"]}))
'''
    env={**os.environ,"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","NUMEXPR_NUM_THREADS":"1"}
    result=subprocess.run([sys.executable,"-c",script,job["id"],claimed["owner"]],env=env,preexec_fn=worker._download_limits if os.name=="posix" else None,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr
    profile=json.loads(result.stdout.strip().splitlines()[-1])
    assert profile["downloaded_bars"]==100005
    if sys.platform.startswith("linux"):
        assert profile["peak_rss_kb"]<768*1024
    print("download_child_profile="+json.dumps(profile))
    final=downloads.get_job(job["id"])
    assert final["status"]=="completed",final["error"]
    with db.connect() as con:
        assert con.execute("SELECT worker_pid FROM data_download_jobs WHERE id=?",(job["id"],)).fetchone()[0]!=os.getpid()
    assert final["downloaded_bars"]==100005
    assert enqueue(client,request_key="survives-web",start="2026-01-01",end="2026-04-01")["id"]==job["id"]
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
    monkeypatch.setattr(BinanceClient,"iter_klines",fake_klines)
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
    monkeypatch.setattr(BinanceClient,"iter_klines",unavailable)
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
    invalid=[body(interval="bad"),body(interval="1h"),body(interval="1d"),body(symbols=["BTCUSDC","BTCUSDC"]),body(market="invalid")]
    for config in invalid:
        assert client.post("/api/datasets/download",json=config).status_code==422
    assert downloads.LEASE_SECONDS==180
    assert downloads.normalize_config(body(start="2025-01-01",end="2026-01-01"))["interval"]=="1m"
    assert downloads.stall_seconds()==900
    monkeypatch.setenv("MK_DOWNLOAD_TIMEOUT_SECONDS","99999")
    assert downloads.stall_seconds()==3600
    monkeypatch.setenv("MK_DOWNLOAD_TIMEOUT_SECONDS","1")
    assert downloads.stall_seconds()==30
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


def test_omitted_download_interval_defaults_to_minute(client):
    config=body();config.pop("interval")
    response=client.post("/api/datasets/download",json=config)
    assert response.status_code==202 and response.json()["config"]["interval"]=="1m"


def test_child_stall_deadline_bypasses_exchange_retry_chain(client,monkeypatch):
    if not hasattr(downloads.signal,"SIGALRM"):
        pytest.skip("Production worker uses POSIX alarms")
    from backend import binance
    job=enqueue(client)
    claimed,_=downloads.claim("orphaned-coordinator")
    attempts=[]
    def blocked_transport(*args,**kwargs):
        attempts.append(1)
        time.sleep(10)
        pytest.fail("Stall deadline was swallowed")
    # Exercise the real urllib transport and Binance retry wrapper. No monitor
    # or exchange network is involved; an orphan child must still stop itself.
    monkeypatch.setattr(binance,"urlopen",blocked_transport)
    monkeypatch.setattr(binance,"_ssl_context",lambda:None)
    monkeypatch.setattr(downloads,"stall_seconds",lambda:1)
    started=time.monotonic()
    downloads.run(job["id"],claimed["owner"])
    result=downloads.get_job(job["id"])
    assert time.monotonic()-started<4 and len(attempts)==1
    assert result["status"]=="failed" and "长时间没有进展" in result["error"]


@pytest.mark.parametrize("worker_pid",[None,os.getpid()])
def test_restart_waits_for_surviving_backtest_or_pid_registration(client,monkeypatch,worker_pid):
    dataset=store_dataset({**body(),"candles":{"BTCUSDC":candles(downloads._timestamp("2026-09-01"))}},"import","download-admin")
    with db.connect() as con:
        con.execute("INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at,started_at,worker_pid) VALUES(?,?,?,?,'running','{}','{}',?,?,?)",("surviving-backtest","存活研究任务","momentum",dataset["id"],db.now(),db.now(),worker_pid))
    job=enqueue(client)
    monkeypatch.setattr(worker.subprocess,"Popen",lambda *a,**k:pytest.fail("Must wait for existing research child"))
    worker.backtest_loop(once=True)
    assert downloads.get_job(job["id"])["status"]=="queued"


def test_long_rate_limit_wait_renews_lease_without_total_deadline(client,monkeypatch):
    clock=[time.time()]
    monkeypatch.setattr(downloads.time,"time",lambda:clock[0])
    job=enqueue(client)
    claimed,_=downloads.claim("long-wait")
    observed=[]
    def pages(self,symbol,interval,start,end,*,progress,on_wait):
        resume=clock[0]+1200
        while clock[0]<resume:
            on_wait({"seconds":resume-clock[0],"reason":"rate_limit","retry_at":resume*1000})
            current=downloads.get_job(job["id"])
            observed.append(current)
            with db.connect() as con:
                assert con.execute("SELECT lease_until FROM data_download_jobs WHERE id=?",(job["id"],)).fetchone()[0]>clock[0]
            clock[0]+=15
        on_wait({"seconds":0,"reason":"rate_limit","retry_at":resume*1000})
        yield candles(start)
    monkeypatch.setattr(BinanceClient,"iter_klines",pages)
    downloads.run(job["id"],claimed["owner"])
    result=downloads.get_job(job["id"])
    assert result["status"]=="completed",result["error"]
    assert len(observed)==80 and all(row["waiting_reason"]=="币安限流冷却，等待恢复" and row["retry_at"]>0 for row in observed)
    assert result["waiting_reason"] is None and result["retry_at"] is None


def test_retry_respects_persisted_cooldown_before_any_request(client,monkeypatch):
    clock=[time.time()]
    monkeypatch.setattr(downloads.time,"time",lambda:clock[0])
    job=enqueue(client)
    downloads.claim("old-worker")
    resume=clock[0]+300
    with db.connect() as con:
        con.execute("UPDATE data_download_jobs SET lease_until=0,waiting_reason='币安限流冷却，等待恢复',retry_at=? WHERE id=?",(resume,job["id"]))
    claimed,_=downloads.claim("replacement-worker")
    monkeypatch.setattr(downloads.time,"sleep",lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    def pages(self,symbol,interval,start,end,*,progress,on_wait):
        assert clock[0]>=resume
        yield candles(start)
    monkeypatch.setattr(BinanceClient,"iter_klines",pages)
    downloads.run(job["id"],claimed["owner"])
    assert downloads.get_job(job["id"])["status"]=="completed"


def test_disk_shortage_fails_clearly_without_dataset_or_partial_files(client,monkeypatch):
    from types import SimpleNamespace
    job=enqueue(client)
    claimed,_=downloads.claim("disk-full")
    monkeypatch.setattr(storage.shutil,"disk_usage",lambda path:SimpleNamespace(free=0))
    monkeypatch.setattr(BinanceClient,"iter_klines",fake_klines)
    downloads.run(job["id"],claimed["owner"])
    result=downloads.get_job(job["id"])
    assert result["status"]=="failed" and "磁盘" in result["error"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]==0
    assert not list(storage.artifact_root().rglob(".writing-*"))


def test_cleanup_only_removes_dead_task_temporaries(client):
    job=enqueue(client)
    old,_=downloads.claim("dead-worker")
    directory=storage.artifact_root()/"candles"
    directory.mkdir(parents=True,exist_ok=True)
    prefix=downloads.temporary_prefix(job["id"],old["owner"])
    dead=directory/(".writing-"+prefix+"999999999-leftover.parquet")
    live=directory/(".writing-"+prefix+str(os.getpid())+"-active.parquet")
    shared=directory/("a"*64+".parquet")
    unrelated=directory/".writing-another-task.parquet"
    for path in (dead,live,shared,unrelated):path.write_bytes(b"untouched")
    with db.connect() as con:
        con.execute("UPDATE data_download_jobs SET lease_until=0 WHERE id=?",(job["id"],))
    downloads.claim("new-worker")
    assert not dead.exists() and live.exists() and shared.exists() and unrelated.exists()
    # The coordinator invokes owner cleanup only after its child has exited.
    downloads.cleanup_temporary(job["id"],old["owner"])
    assert not live.exists() and shared.exists() and unrelated.exists()


def test_download_resources_have_no_new_cumulative_cpu_deadline(monkeypatch):
    import resource
    calls=[]
    monkeypatch.setattr(worker.sys,"platform","linux")
    monkeypatch.setattr(worker.os,"nice",lambda value:calls.append(("nice",value)))
    monkeypatch.setattr(resource,"getrlimit",lambda kind:(resource.RLIM_INFINITY,512*1024*1024))
    monkeypatch.setattr(resource,"setrlimit",lambda kind,limits:calls.append((kind,limits)))
    worker._download_limits()
    assert ("nice",10) in calls
    assert (resource.RLIMIT_AS,(512*1024*1024,512*1024*1024)) in calls
    assert all(kind!=resource.RLIMIT_CPU for kind,value in calls)


def test_download_task_delete_retains_dataset_and_audit(client, monkeypatch):
    job = enqueue(client)
    claimed, busy = downloads.claim("delete-test")
    monkeypatch.setattr(BinanceClient, "iter_klines", fake_klines)
    downloads.run(job['id'], claimed['owner'])
    dataset_id = downloads.get_job(job['id'])['dataset_id']
    with db.connect() as con:
        manifest = db.parse(con.execute('SELECT candles FROM datasets WHERE id=?', (dataset_id,)).fetchone()[0])
    result = client.delete('/api/datasets/download-jobs/' + job['id'])
    assert result.status_code == 200 and result.json()['deleted']
    assert client.get('/api/datasets/download-jobs/' + job['id']).status_code == 404
    assert storage.read_candles(manifest)
    assert client.get('/api/datasets').json()[0]['id'] == dataset_id
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='dataset.download.delete' AND resource_id=?", (job['id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_download_task_delete_rejects_active(client, status):
    job = enqueue(client)
    if status == 'running':
        downloads.claim('active')
    assert client.delete('/api/datasets/download-jobs/' + job['id']).status_code == 409
    assert downloads.get_job(job['id'])['status'] == status


def test_failed_download_task_delete_requires_auth_and_csrf(client):
    job = enqueue(client)
    claimed, _ = downloads.claim('failed')
    downloads.fail(job['id'], claimed['owner'], 'fixture failure')
    assert TestClient(app).delete('/api/datasets/download-jobs/' + job['id']).status_code == 401
    csrf = client.headers.pop('X-MK-CSRF')
    assert client.delete('/api/datasets/download-jobs/' + job['id']).status_code == 403
    client.headers['X-MK-CSRF'] = csrf
    assert client.delete('/api/datasets/download-jobs/' + job['id']).status_code == 200
    assert client.delete('/api/datasets/download-jobs/' + job['id']).status_code == 404
