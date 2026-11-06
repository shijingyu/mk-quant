"""Simulation cost overrides, including a database-free worker tick harness."""
import copy
import json
from decimal import Decimal

import pytest

from backend import worker
from backend.strategy import validate_manifest, validate_paper_costs
from tests.test_engine import BASE


def manifest(costs=None, market="futures"):
    value=copy.deepcopy(BASE)
    value.update(market=market,history_window=720,timeframes={"primary":"1h","additional":[]})
    if market=="spot":
        value["account"].update(position_modes=["one_way"],margin_modes=["cash"],leverage_min=1,leverage_max=1)
    value["execution"]={"model":"market_next_open_v1"}
    if costs is not None:value["execution"]["paper_costs"]=costs
    return validate_manifest(value)


@pytest.mark.parametrize("costs",[
    {},{"fee_rate":0},{"fee_rate":0,"slippage_bps":0,"extra":1},
    {"fee_rate":True,"slippage_bps":0},{"fee_rate":0,"slippage_bps":False},
    {"fee_rate":-.001,"slippage_bps":0},{"fee_rate":.1001,"slippage_bps":0},
    {"fee_rate":float("nan"),"slippage_bps":0},{"fee_rate":0,"slippage_bps":float("inf")},
    {"fee_rate":0,"slippage_bps":-1},{"fee_rate":0,"slippage_bps":1001},
])
def test_invalid_cost_declarations_fail_closed(costs):
    with pytest.raises(ValueError):validate_paper_costs(costs)


def test_cost_declaration_is_copied_and_maker_cannot_claim_market_override():
    raw=copy.deepcopy(BASE)
    raw["execution"]={"model":"market_next_open_v1","paper_costs":{"fee_rate":0,"slippage_bps":0}}
    parsed=validate_manifest(raw)
    parsed["execution"]["paper_costs"]["fee_rate"]=.01
    assert raw["execution"]["paper_costs"]["fee_rate"]==0
    raw["execution"]["model"]="maker_grid_v1"
    raw["account"]["position_modes"]=["hedge"]
    with pytest.raises(ValueError,match="paper_costs"):validate_manifest(raw)


def test_declared_zero_overrides_stale_cached_nonzero_fee_and_slippage():
    result=worker._paper_execution_costs(manifest({"fee_rate":0,"slippage_bps":0}),
        {"execution_model":{"model":"market_next_open_v1","fee_rate":.0004,"slippage_bps":2}})
    assert result=={"fee_rate":0.,"slippage_bps":0.,"fee_source":"strategy_manifest_paper_costs"}


@pytest.mark.parametrize("market,fee",[("futures",.0004),("spot",.001)])
def test_undeclared_strategy_retains_old_default_costs(market,fee):
    result=worker._paper_execution_costs(manifest(market=market))
    assert result["fee_rate"]==fee and result["slippage_bps"]==2
    assert result["fee_source"]=="documented_simulation_assumption"


def test_legacy_cached_zero_fee_is_preserved_but_maker_cache_is_not_reused():
    value=manifest()
    result=worker._paper_execution_costs(value,{"execution_model":{"fee_rate":0,"slippage_bps":9}})
    assert result["fee_rate"]==0 and result["slippage_bps"]==2
    result=worker._paper_execution_costs(value,{"execution_model":{"model":"maker_grid_v1","fee_rate":0}})
    assert result["fee_rate"]==.0004


def test_ledger_restoration_changes_future_fee_only_and_preserves_historical_cash():
    account={"initial_equity":10000,"metrics":json.dumps({"execution_model":{"fee_rate":.0004}})}
    snapshot={"balance":"9990","fees":"10","positions":{},"orders":[],"closed_pnls":[]}
    ledger=worker._restore_ledger(account,manifest({"fee_rate":0,"slippage_bps":0}),snapshot)
    assert ledger.fee_rate==Decimal(0)
    assert ledger.balance==Decimal(9990) and ledger.fees==Decimal(10)


class MemoryDB:
    """Only the explicit SQL branches reached by a single synthetic tick."""
    def __init__(self, definition, flatten=False):
        self.timestamp=1735689660000
        self.now=self.timestamp/1000+90
        self.instance={"desired_state":"running","config_revision":1,"owner":"test-worker","fence":7,"lease_until":self.now+1000}
        self.account={"id":"test-account","strategy_id":"test-strategy","mode":"paper","source":"public", "initial_equity":10000,
            "symbols":json.dumps(["BTCUSDC","ETHUSDC"]),"config":"{}","metrics":json.dumps({"execution_model":{"fee_rate":.0004}})}
        self.strategy={"manifest":json.dumps(definition),"trusted":True,"source_hash":"test-source","source":"not called before complete hour"}
        self.commands=[{"id":"test-flatten","action":"flatten"}] if flatten else []
        positions={"BTCUSDC":{"quantity":"1","average_price":"100","entry_fees":"0"}} if flatten else {}
        self.snapshot={"model":"market_next_open_v1","balance":"10000","fees":"0","positions":positions,
            "orders":[],"closed_pnls":[],"last_timestamp":self.timestamp-60000,"curve":[],
            "pending":[] if flatten else [{"symbol":"BTCUSDC","side":"buy","quantity":1},
                                          {"symbol":"ETHUSDC","side":"sell","quantity":1}],
            "strategy_state":{},"bar_index":0,"frames":None,
            "revision":{"strategy_id":"test-strategy","source_hash":"test-source","config_revision":1}}
        self.cache={}
        for symbol,price,tick in [("BTCUSDC",100.,".1"),("ETHUSDC",50.,".01")]:
            bars=[dict(timestamp=t,open=price,high=price+1,low=price-1,close=price,volume=100.)
                  for t in [self.timestamp-60000,self.timestamp]]
            self.cache[symbol]={"fetched_at":self.now,"candles":json.dumps(bars),
                "filters":json.dumps({"tick_size":tick,"step_size":".001"})}
        self.rows=[];self.persisted=[];self.metrics=None

    def __enter__(self):return self
    def __exit__(self,*args):return False
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def execute(self,sql,args=()):
        self.rows=[]
        if sql.startswith("SELECT * FROM accounts"):self.rows=[self.account]
        elif sql.startswith("SELECT * FROM instances"):self.rows=[self.instance]
        elif sql.startswith("SELECT * FROM commands"):self.rows=self.commands
        elif sql.startswith("SELECT * FROM strategies"):self.rows=[self.strategy]
        elif sql.startswith("SELECT payload FROM ledger"):self.rows=[{"payload":json.dumps(self.snapshot)}]
        elif sql.startswith("SELECT * FROM market_cache"):self.rows=[self.cache[args[1]]]
        elif sql.startswith("UPDATE accounts SET metrics"):self.metrics=json.loads(args[0])
        elif sql.startswith("INSERT INTO ledger") and args[2]=="snapshot":self.snapshot=json.loads(args[4])
        elif sql.startswith(("INSERT", "UPDATE")):pass
        else:raise AssertionError("Unexpected SQL in isolated test: "+sql)
        return self


def run_mock_tick(monkeypatch, definition, flatten=False):
    memory=MemoryDB(definition,flatten)
    monkeypatch.setattr(worker,"ensure_market_cache",lambda:None)
    monkeypatch.setattr(worker.time,"time",lambda:memory.now)
    monkeypatch.setattr(worker.db,"connect",lambda:memory)
    monkeypatch.setattr(worker.db,"acquire_lease",lambda *args:7)
    monkeypatch.setattr(worker.db,"lock_instance",lambda *args:memory.instance)
    monkeypatch.setattr(worker.db,"persist_fill",lambda con,account,order:memory.persisted.append(copy.deepcopy(order)))
    monkeypatch.setattr(worker.db,"audit",lambda *args,**kwargs:None)
    worker.runtime_tick("test-account","test-worker")
    return memory


@pytest.mark.parametrize("declared",[True,False])
def test_mock_paper_tick_uses_selected_costs_in_fills_and_report(monkeypatch,declared):
    value=manifest({"fee_rate":0,"slippage_bps":0} if declared else None)
    memory=run_mock_tick(monkeypatch,value)
    assert len(memory.persisted)==2
    assert [o["price"] for o in memory.persisted]==([100.,50.] if declared else [100.1,49.99])
    assert all(o["fee"]==0 for o in memory.persisted) if declared else all(o["fee"]>0 for o in memory.persisted)
    assert memory.metrics["execution_model"]["fee_rate"]==(0. if declared else .0004)
    assert memory.metrics["execution_model"]["slippage_bps"]==(0. if declared else 2.)
    assert memory.metrics["execution_model"]["fee_source"]==(
        "strategy_manifest_paper_costs" if declared else "documented_simulation_assumption")
    assert memory.snapshot["pending"]==[]


def test_mock_paper_admin_flatten_uses_declared_zero_costs(monkeypatch):
    memory=run_mock_tick(monkeypatch,manifest({"fee_rate":0,"slippage_bps":0}),flatten=True)
    assert len(memory.persisted)==1
    assert memory.persisted[0]["reduce_only"] and memory.persisted[0]["price"]==100.
    assert memory.persisted[0]["fee"]==0
    assert memory.metrics["position_count"]==0
    assert memory.metrics["execution_model"]["fee_source"]=="strategy_manifest_paper_costs"
