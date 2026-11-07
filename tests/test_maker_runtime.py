"""Maker lifecycle integration tests against the same real MySQL engine."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend import db
from backend.worker import runtime_tick
from backend.security import hash_password


MANIFEST = {
    "id": "maker-runtime-test", "name": "Maker runtime fixture", "version": "1.0.0",
    "description": "Deterministic integration fixture", "market": "futures",
    "symbols": {"mode": "any", "values": [], "min_count": 1},
    "account": {"position_modes": ["hedge"], "margin_modes": ["cross"], "leverage_min": 1, "leverage_max": 1, "min_capital": 100},
    "execution": {"model": "maker_grid_v1"}, "history_window": 4,
    "parameters": {"type": "object", "properties": {"fail": {"type": "boolean", "default": False}}},
}
SOURCE = "MANIFEST = " + repr(MANIFEST) + '''

class Strategy:
    def on_bar(self, context):
        if context["params"]["fail"] and context["bar_index"] > 0:
            raise ValueError("intentional strategy failure")
        if context["bar_index"] != 0:
            return []
        return [
            {"client_order_id":"entry-long", "symbol":"BTCUSDC", "side":"buy", "position_side":"LONG", "type":"LIMIT", "time_in_force":"GTX", "quantity":1, "price":99, "bracket":{"take_profit_price":103,"stop_price":90,"stop_limit_price":90.1}},
            {"client_order_id":"entry-short", "symbol":"BTCUSDC", "side":"sell", "position_side":"SHORT", "type":"LIMIT", "time_in_force":"GTX", "quantity":1, "price":101, "bracket":{"take_profit_price":97,"stop_price":110,"stop_limit_price":109.9}},
        ]
'''


@pytest.fixture
def maker(mysql_database, monkeypatch):
    start=int(time.time()//60)*60000-60000
    clock={"now":(start+60000)/1000+1}
    monkeypatch.setattr(time,"time",lambda:clock["now"])
    with db.connect() as con:
        con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",("maker-user","maker-test","Maker test","13900000188",hash_password("maker-runtime-test-password"),"admin",db.now()))
        con.execute("INSERT INTO strategies VALUES(?,?,?,?,1,?)",(MANIFEST["id"],db.dump(MANIFEST),SOURCE,hashlib.sha256(SOURCE.encode()).hexdigest(),db.now()))
        con.execute("INSERT INTO accounts(id,user_id,name,mode,route,strategy_id,config,symbols,initial_equity,metrics,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("maker-account","maker-user","Maker paper","paper","1"*32,MANIFEST["id"],db.dump({"fail":False}),db.dump(["BTCUSDC"]),1000,"{}","local",db.now()))
        con.execute("INSERT INTO instances(account_id) VALUES(?)",("maker-account",))
    return {"id":"maker-account","clock":clock,"start":start,"history":[]}


def feed(maker, minute, *, high=100, low=100, close=100):
    stamp=maker["start"]+minute*60000
    maker["clock"]["now"]=(stamp+60000)/1000+1
    maker["history"].append({"timestamp":stamp,"open":100,"high":high,"low":low,"close":close,"volume":100})
    with db.connect() as con:
        con.execute("INSERT INTO market_cache VALUES('futures','BTCUSDC','1m',?,?,?) ON DUPLICATE KEY UPDATE candles=VALUES(candles),fetched_at=VALUES(fetched_at)",
                    (db.dump(maker["history"]),db.dump({"tick_size":"0.01","step_size":"0.01","min_qty":"0.01","min_notional":"5"}),time.time()))


def command(maker, action):
    identity=db.uid()
    with db.connect() as con:
        db.lock_instance(con,maker["id"])
        con.execute("INSERT INTO commands(id,account_id,action,requested_by,created_at) VALUES(?,?,?,?,?)",(identity,maker["id"],action,"maker-user",db.now()))
        con.execute("UPDATE instances SET desired_state=? WHERE account_id=?",({"resume":"running","pause":"paused","flatten":"stopped"}[action],maker["id"]))
    return identity


def read(maker):
    with db.connect() as con:
        snapshot=con.execute("SELECT payload FROM ledger WHERE account_id=? AND kind='snapshot'",(maker["id"],)).fetchone()
        return {
            "snapshot":db.parse(snapshot["payload"]) if snapshot else None,
            "metrics":db.parse(con.execute("SELECT metrics FROM accounts WHERE id=?",(maker["id"],)).fetchone()[0]),
            "instance":dict(con.execute("SELECT * FROM instances WHERE account_id=?",(maker["id"],)).fetchone()),
            "orders":[db.parse(row["payload"]) for row in con.execute("SELECT payload FROM orders WHERE account_id=? ORDER BY client_order_id",(maker["id"],)).fetchall()],
            "fills":[db.parse(row["payload"]) for row in con.execute("SELECT payload FROM fills WHERE account_id=?",(maker["id"],)).fetchall()],
            "fill_ledger":con.execute("SELECT COUNT(*) FROM ledger WHERE account_id=? AND kind='fill'",(maker["id"],)).fetchone()[0],
            "commands":{row["id"]:dict(row) for row in con.execute("SELECT * FROM commands WHERE account_id=?",(maker["id"],)).fetchall()},
        }


def start(maker):
    command(maker,"resume");feed(maker,0)
    assert runtime_tick(maker["id"],"maker-worker")


def fill_entries(maker):
    start(maker)
    # Both TP levels are crossed in this entry bar. Newly attached protection
    # must nevertheless wait until the following complete bar.
    feed(maker,1,high=104,low=96)
    assert runtime_tick(maker["id"],"maker-worker")


def test_maker_orders_are_not_fills_and_restore_keeps_two_sides(maker):
    start(maker)
    placed=read(maker)
    assert len(placed["orders"])==2 and not placed["fills"]
    assert placed["metrics"]["pending_margin"]==200
    assert all(order["status"]=="NEW" for order in placed["orders"])
    feed(maker,1,high=104,low=96)
    runtime_tick(maker["id"],"maker-worker")
    filled=read(maker)
    assert len(filled["fills"])==filled["fill_ledger"]==2
    assert len(filled["orders"])==6 and len(filled["metrics"]["open_orders"])==4
    assert {lot["position_side"] for lot in filled["metrics"]["lots"]}=={"LONG","SHORT"}
    assert filled["metrics"]["margin_used"]==200 and filled["metrics"]["pending_margin"]==0
    assert len(filled["snapshot"]["book"]["lots"])==2
    # New owner after lease expiry restores the same book without replay fills.
    with db.connect() as con:
        con.execute("UPDATE instances SET lease_until=0 WHERE account_id=?",(maker["id"],))
    runtime_tick(maker["id"],"replacement-worker")
    restored=read(maker)
    assert restored["snapshot"]["book"]==filled["snapshot"]["book"]
    assert len(restored["fills"])==2 and restored["fill_ledger"]==2


def test_maker_pause_cancels_only_entry_orders(maker):
    start(maker)
    paused=command(maker,"pause")
    runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert not state["fills"] and all(order["status"]=="CANCELED" for order in state["orders"])
    assert not state["metrics"]["open_orders"]
    assert state["commands"][paused]["status"]=="completed"


def test_maker_pause_preserves_attached_protection(maker):
    fill_entries(maker)
    paused=command(maker,"pause")
    runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert len(state["metrics"]["open_orders"])==4
    assert all(order["reduce_only"] for order in state["metrics"]["open_orders"])
    assert len(state["fills"])==2 and state["commands"][paused]["status"]=="completed"


def test_maker_flatten_stays_running_until_post_only_exits_fill(maker):
    fill_entries(maker)
    stopped=command(maker,"flatten")
    assert runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert state["commands"][stopped]["status"]=="running"
    assert state["instance"]["observed_state"]=="flattening"
    assert len(state["fills"])==2 and {order["role"] for order in state["metrics"]["open_orders"]}=={"flatten"}
    feed(maker,2)
    assert runtime_tick(maker["id"],"maker-worker")
    assert read(maker)["commands"][stopped]["status"]=="running"
    feed(maker,3,high=100.02,low=99.98)
    assert not runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert state["commands"][stopped]["status"]=="completed"
    assert state["instance"]["observed_state"]=="stopped"
    assert len(state["fills"])==state["fill_ledger"]==4
    assert not state["metrics"]["lots"] and not state["metrics"]["open_orders"]
    assert all(fill["type"]=="LIMIT" and fill["liquidity"]=="simulated_maker" for fill in state["fills"])


def test_missing_market_cannot_complete_flatten_or_erase_protection(maker):
    fill_entries(maker)
    stopped=command(maker,"flatten")
    with db.connect() as con:
        con.execute("DELETE FROM market_cache")
    assert runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert state["commands"][stopped]["status"]=="running"
    assert state["instance"]["observed_state"]=="flattening" and state["instance"]["error"]
    assert len(state["fills"])==2 and len(state["metrics"]["open_orders"])==4
    assert len(state["metrics"]["lots"])==2


def test_gap_cancels_stale_entries_without_inventing_fills(maker):
    start(maker)
    feed(maker,3,high=104,low=96)
    runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert not state["fills"] and not state["metrics"]["lots"]
    assert all(order["status"]=="CANCELED" for order in state["orders"])
    assert any("断档" in record["reason"] for record in state["metrics"]["risk"])


def test_strategy_error_keeps_protection_running(maker):
    with db.connect() as con:
        con.execute("UPDATE accounts SET config=? WHERE id=?",(db.dump({"fail":True}),maker["id"]))
    fill_entries(maker)
    state=read(maker)
    assert state["instance"]["desired_state"]=="paused" and state["instance"]["observed_state"]=="error"
    assert len(state["metrics"]["lots"])==2 and len(state["metrics"]["open_orders"])==4
    feed(maker,2,high=104,low=96)
    runtime_tick(maker["id"],"maker-worker")
    protected=read(maker)
    assert len(protected["fills"])==4 and not protected["metrics"]["lots"]
    assert protected["snapshot"]["strategy_error"]=="intentional strategy failure"


def test_maker_stale_executor_cannot_commit_order_or_snapshot(maker,monkeypatch):
    command(maker,"resume");feed(maker,0)
    acquire=db.acquire_lease
    def replaced(account_id,owner):
        fence=acquire(account_id,owner)
        with db.connect() as con:
            con.execute("UPDATE instances SET owner='replacement',fence=fence+1 WHERE account_id=?",(account_id,))
        return fence
    monkeypatch.setattr(db,"acquire_lease",replaced)
    runtime_tick(maker["id"],"stale-worker")
    state=read(maker)
    assert not state["orders"] and not state["fills"] and state["snapshot"] is None
    assert all(row["status"]=="pending" for row in state["commands"].values())


def test_maker_transaction_rolls_back_fills_and_lifecycle_together(maker,monkeypatch):
    start(maker);previous=read(maker)
    feed(maker,1,high=104,low=96)
    persist=db.persist_maker_activity
    def fail_after_orders(con,account_id,orders,fills):
        persist(con,account_id,orders,fills)
        raise RuntimeError("database transaction failure")
    monkeypatch.setattr(db,"persist_maker_activity",fail_after_orders)
    with pytest.raises(RuntimeError,match="database transaction failure"):
        runtime_tick(maker["id"],"maker-worker")
    rolled_back=read(maker)
    assert rolled_back["snapshot"]==previous["snapshot"]
    assert rolled_back["orders"]==previous["orders"] and not rolled_back["fills"] and not rolled_back["fill_ledger"]
    monkeypatch.setattr(db,"persist_maker_activity",persist)
    runtime_tick(maker["id"],"maker-worker")
    assert len(read(maker)["fills"])==2


def test_maker_api_cannot_replace_config_while_book_retains_risk(maker):
    from fastapi.testclient import TestClient
    from backend.app import app
    fill_entries(maker)
    # Even an inconsistent stopped label must not authorize erasing the book.
    with db.connect() as con:
        con.execute("UPDATE instances SET desired_state='stopped',observed_state='stopped' WHERE account_id=?",(maker["id"],))
    with TestClient(app) as client:
        login=client.post("/api/login",json={"username":"maker-test","password":"maker-runtime-test-password"})
        assert login.status_code==200
        client.headers["X-MK-CSRF"]=login.json()["csrf_token"]
        assert client.patch("/api/accounts/"+maker["id"],json={"config":{"fail":True}}).status_code==409
        assert client.patch("/api/accounts/"+maker["id"],json={"strategy_id":"momentum","config":{}}).status_code==409
    state=read(maker)
    assert len(state["metrics"]["lots"])==2 and len(state["metrics"]["open_orders"])==4


def test_maker_api_rejects_insufficient_equity_only_for_execution_changes(maker):
    from fastapi.testclient import TestClient
    from backend.app import app
    with db.connect() as con:
        con.execute("UPDATE accounts SET metrics=? WHERE id=?",(db.dump({"equity":50}),maker["id"]))
    with TestClient(app) as client:
        login=client.post("/api/login",json={"username":"maker-test","password":"maker-runtime-test-password"})
        client.headers["X-MK-CSRF"]=login.json()["csrf_token"]
        assert client.patch("/api/accounts/"+maker["id"],json={"strategy_id":"usdc_hedge_grid","config":{}}).status_code==422
        assert client.patch("/api/accounts/"+maker["id"],json={"name":"Renamed safely","password":"new-test-password-123"}).status_code==200
    assert read(maker)["instance"]["config_revision"]==1


def test_flat_model_roundtrip_preserves_money_history_and_new_order_identity(maker):
    import copy
    fill_entries(maker);command(maker,"flatten")
    runtime_tick(maker["id"],"maker-worker")
    feed(maker,2,high=100.02,low=99.98)
    assert not runtime_tick(maker["id"],"maker-worker")
    original=read(maker)
    original_money=original["snapshot"]["book"]["ledger"]
    with db.connect() as con:
        con.execute("UPDATE accounts SET strategy_id='momentum',config='{}' WHERE id=?",(maker["id"],))
        con.execute("UPDATE instances SET config_revision=config_revision+1 WHERE account_id=?",(maker["id"],))
    assert not runtime_tick(maker["id"],"maker-worker")
    legacy=read(maker)
    assert legacy["snapshot"]["model"]=="market_next_open_v1"
    assert all(legacy["snapshot"][key]==original_money[key] for key in ("balance","fees","orders","closed_pnls"))
    assert legacy["snapshot"]["curve"]==original["snapshot"]["curve"]
    new_manifest=copy.deepcopy(MANIFEST);new_manifest["id"]="maker-second-generation"
    new_manifest["account"].update(leverage_min=2,leverage_max=2)
    new_source=SOURCE.replace(repr(MANIFEST),repr(new_manifest),1)
    with db.connect() as con:
        con.execute("INSERT INTO strategies VALUES(?,?,?,?,1,?)",(new_manifest["id"],db.dump(new_manifest),new_source,hashlib.sha256(new_source.encode()).hexdigest(),db.now()))
        con.execute("UPDATE accounts SET strategy_id=?,config='{}' WHERE id=?",(new_manifest["id"],maker["id"]))
        con.execute("UPDATE instances SET config_revision=config_revision+1 WHERE account_id=?",(maker["id"],))
    assert not runtime_tick(maker["id"],"maker-worker")
    converted=read(maker)
    new_money=converted["snapshot"]["book"]["ledger"]
    assert all(new_money[key]==original_money[key] for key in ("balance","fees","orders","closed_pnls"))
    assert new_money["leverage"]=="2"
    assert converted["snapshot"]["execution_id"]!=original["snapshot"]["execution_id"]
    assert converted["fill_ledger"]==len(converted["fills"])==4
    assert converted["orders"]==original["orders"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ledger WHERE account_id=? AND kind='executor_archive'",(maker["id"],)).fetchone()[0]==2
    command(maker,"resume");feed(maker,3)
    runtime_tick(maker["id"],"maker-worker")
    resumed=read(maker)
    assert len(resumed["orders"])==len(original["orders"])+2
    assert len({order["execution_id"] for order in resumed["orders"]})==2
    feed(maker,4,high=104,low=96)
    runtime_tick(maker["id"],"maker-worker")
    assert read(maker)["fill_ledger"]==len(read(maker)["fills"])==6


@pytest.mark.parametrize("model",["maker_grid_v1","market_next_open_v1"])
def test_daily_and_monthly_pnl_reset_at_beijing_midnight(maker,model):
    from backend.engine import Ledger, MakerOrderBook
    midnight=int(datetime(2026,11,1,tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
    maker["start"]=midnight;maker["history"]=[]
    curve=[{"timestamp":midnight-120000,"equity":1000},{"timestamp":midnight-60000,"equity":1050}]
    if model=="maker_grid_v1":
        ledger=Ledger(1000,"futures",1,.0002,position_mode="hedge");ledger.balance+=50
        snapshot={"model":model,"book":MakerOrderBook(ledger).dump(),"curve":curve,"last_timestamp":midnight-60000}
    else:
        snapshot={"model":model,"balance":"1050","fees":"0","positions":{},"orders":[],"closed_pnls":[],"pending":[],"curve":curve,"last_timestamp":midnight-60000}
    with db.connect() as con:
        if model=="market_next_open_v1":
            con.execute("UPDATE accounts SET strategy_id='momentum' WHERE id=?",(maker["id"],))
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?)",(db.uid(),maker["id"],"snapshot","snapshot:"+maker["id"],db.dump(snapshot),db.now()))
    feed(maker,0)
    runtime_tick(maker["id"],"maker-worker")
    metrics=read(maker)["metrics"]
    assert metrics["equity"]==1050
    assert metrics["today_pnl"]==metrics["month_pnl"]==0


def test_same_bar_heartbeat_does_not_load_or_rewrite_maker_book(maker,monkeypatch):
    from backend.engine import MakerOrderBook
    start(maker);previous=read(maker)
    def forbidden_load(state):
        raise AssertionError("unchanged heartbeat must not reconstruct the full order book")
    monkeypatch.setattr(MakerOrderBook,"load",forbidden_load)
    assert runtime_tick(maker["id"],"maker-worker")
    current=read(maker)
    assert current["snapshot"]==previous["snapshot"] and current["orders"]==previous["orders"]


def test_rejected_maker_entries_have_visible_reason_without_fake_fills(maker):
    command(maker,"resume");feed(maker,0)
    with db.connect() as con:
        con.execute("UPDATE market_cache SET filters=?",(db.dump({"tick_size":"0.01","step_size":"0.01","min_qty":"2","min_notional":"5"}),))
    runtime_tick(maker["id"],"maker-worker")
    state=read(maker)
    assert len(state["orders"])==2 and all(order["status"]=="REJECTED" for order in state["orders"])
    assert not state["fills"] and not state["metrics"]["open_orders"]
    assert len(state["metrics"]["risk"])==2
    assert all(item["status"]=="rejected" and "LOT_SIZE" in item["reason"] for item in state["metrics"]["risk"])
    with db.connect() as con:
        logs=con.execute("SELECT details FROM logs WHERE account_id=? AND level='error'",(maker["id"],)).fetchall()
        assert len(logs)==1 and db.parse(logs[0]["details"])["rejected_count"]==2


def test_maker_api_experiment_preserves_defaults_version_and_full_artifacts(maker):
    from fastapi.testclient import TestClient
    from backend.app import app, store_dataset
    from backend import storage
    from backend.worker import run_experiment
    start_time=maker["start"]
    bars=[{"timestamp":start_time+i*60000,"open":100,"high":104 if i%2 else 100,"low":96 if i%2 else 100,"close":100,"volume":100} for i in range(4)]
    dataset=store_dataset({"market":"futures","symbols":["BTCUSDC"],"interval":"1m","candles":{"BTCUSDC":bars}},"synthetic")
    body={"strategy_id":MANIFEST["id"],"dataset_id":dataset["id"],"symbols":["BTCUSDC"],"initial_capital":1000,
          "train_start":start_time,"train_end":start_time+120000,"validation_start":start_time+120000,"validation_end":start_time+240000}
    with TestClient(app) as client:
        login=client.post("/api/login",json={"username":"maker-test","password":"maker-runtime-test-password"})
        assert login.status_code==200
        client.headers["X-MK-CSRF"]=login.json()["csrf_token"]
        assert client.post("/api/experiments",json={**body,"slippage_bps":2}).status_code==422
        response=client.post("/api/experiments",json=body)
        assert response.status_code==202,response.text
        identity=response.json()["id"]
        queued=client.get("/api/experiments/"+identity).json()
        assert queued["fee_rate"]==.0002 and queued["slippage_bps"]==0
        assert queued["provenance"]["strategy_version_id"]
        run_experiment(identity)
        result=client.get("/api/experiments/"+identity).json()
        assert result["status"]=="completed",result.get("error")
        assert len(result["open_orders"])==8 and len(result["lots"])==4
        assert {order["split"] for order in result["open_orders"]}=={"train","validation"}
        assert {lot["split"] for lot in result["lots"]}=={"train","validation"}
        assert result["account_summaries"]["train"]["position_mode"]=="hedge"
        assert {event["type"] for decision in result["decisions"] for event in decision["execution_events"]}>={"ORDER_ACCEPTED","ORDER_FILLED","LOT_OPENED"}
    with db.connect() as con:
        row=con.execute("SELECT result,config FROM experiments WHERE id=?",(identity,)).fetchone()
        stored=db.parse(row["result"])
        assert "decisions" not in stored and "events" not in stored and "order_book" not in stored
        assert stored["artifacts"]["decisions"]["count"]==4
        assert storage.load_result(stored,detail=True)["decisions"]==result["decisions"]
        version=con.execute("SELECT source_hash FROM strategy_versions WHERE id=?",(db.parse(row["config"])["strategy_version_id"],)).fetchone()
        assert version["source_hash"]==hashlib.sha256(SOURCE.encode()).hexdigest()
        assert con.execute("SELECT COUNT(*) FROM fills WHERE account_id=?",(maker["id"],)).fetchone()[0]==0


def test_account_selected_leverage_controls_margin_and_survives_restore(maker):
    with db.connect() as con:
        manifest=dict(MANIFEST,account={**MANIFEST['account'],'leverage_max':5})
        con.execute('UPDATE strategies SET manifest=? WHERE id=?',(db.dump(manifest),MANIFEST['id']))
        con.execute('INSERT INTO account_execution_settings VALUES(?,?,?)',(maker['id'],4,db.now()))
    start(maker)
    assert read(maker)['metrics']['pending_margin']==50
    feed(maker,1,high=104,low=96)
    runtime_tick(maker['id'],'maker-worker')
    filled=read(maker)
    assert filled['metrics']['margin_used']==50
    assert filled['metrics']['leverage']==4
    with db.connect() as con:
        con.execute('UPDATE instances SET lease_until=0 WHERE account_id=?',(maker['id'],))
    runtime_tick(maker['id'],'replacement-worker')
    restored=read(maker)
    assert restored['metrics']['margin_used']==50
    assert len(restored['fills'])==2
