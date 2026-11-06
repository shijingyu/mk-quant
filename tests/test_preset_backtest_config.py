"""Preset HTTP/schema tests with memory substitutes, no server or database."""
import copy
import importlib
import json
from fastapi.testclient import TestClient
import pytest
from backend import db
from backend.strategy import validate_manifest
from tests.test_engine import BASE

api=importlib.import_module("backend.app")
CONFIG={"initial_capital":10000,"leverage":2,"fee_rate":0,"slippage_bps":0}


def manifest(maker=False,spot=False):
    value=copy.deepcopy(BASE)
    value["account"].update(min_capital=1000,leverage_min=1,leverage_max=100)
    value["parameters"]={"type":"object","properties":{"period":{"type":"integer","default":20,"minimum":2,"maximum":100}}}
    if spot:
        value["market"]="spot";value["account"].update(position_modes=["one_way"],margin_modes=["cash"],leverage_max=1)
    if maker:
        value["execution"]={"model":"maker_grid_v1"};value["account"]["position_modes"]=["hedge"]
    return validate_manifest(value)


def test_zero_costs_capital_boundary_and_integer_leverage_are_preserved():
    result=api.validate_preset_backtest_config(manifest(),dict(CONFIG,initial_capital=1000,leverage=100.))
    assert result==dict(initial_capital=1000.,leverage=100,fee_rate=0.,slippage_bps=0.)
    assert type(result["leverage"]) is int
    assert api.validate_preset_backtest_config(manifest(),{})=={}
    limits=api.validate_preset_backtest_config(manifest(),dict(CONFIG,fee_rate=.1,slippage_bps=1000))
    assert limits["fee_rate"]==.1 and limits["slippage_bps"]==1000


@pytest.mark.parametrize("key",list(CONFIG))
@pytest.mark.parametrize("value",[True,float("nan"),float("inf"),None,"2"])
def test_config_requires_finite_json_numbers(key,value):
    with pytest.raises(ValueError,match="有限数字"):
        api.validate_preset_backtest_config(manifest(),dict(CONFIG,**{key:value}))


@pytest.mark.parametrize("changes",[{"initial_capital":0},{"initial_capital":999.99},{"initial_capital":10**400},
    {"leverage":0},{"leverage":101},{"leverage":2.5},{"fee_rate":-.001},{"fee_rate":.10001},
    {"slippage_bps":-1},{"slippage_bps":1000.01},{"slippage_bps":10000}])
def test_account_and_cost_bounds_fail_closed(changes):
    with pytest.raises(ValueError):api.validate_preset_backtest_config(manifest(),dict(CONFIG,**changes))


@pytest.mark.parametrize("value",[None,[],"",{"initial_capital":10000},{**CONFIG,"dataset_id":"excluded"}])
def test_config_is_empty_legacy_or_exact_four_fields(value):
    with pytest.raises(ValueError):api.validate_preset_backtest_config(manifest(),value)


def test_maker_zero_slippage_and_spot_one_times_constraint():
    assert api.validate_preset_backtest_config(manifest(maker=True),CONFIG)["slippage_bps"]==0
    with pytest.raises(ValueError,match="Maker"):
        api.validate_preset_backtest_config(manifest(maker=True),dict(CONFIG,slippage_bps=.01))
    assert api.validate_preset_backtest_config(manifest(spot=True),dict(CONFIG,leverage=1))["leverage"]==1
    with pytest.raises(ValueError,match="杠杆"):
        api.validate_preset_backtest_config(manifest(spot=True),CONFIG)


class PresetMemory:
    def __init__(self):
        self.strategies={key:{"id":key,"manifest":db.dump(value)} for key,value in {
            "market":manifest(),"maker":manifest(maker=True)}.items()}
        self.presets=[];self.rows=[];self.audits=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def execute(self,sql,args=()):
        self.rows=[]
        if sql.startswith("SELECT * FROM strategies"):
            self.rows=[self.strategies[args[0]]] if args[0] in self.strategies else []
        elif sql.startswith("SELECT * FROM presets"):self.rows=copy.deepcopy(self.presets)
        elif sql.startswith("INSERT INTO presets("):
            columns=sql.split("(",1)[1].split(")",1)[0].split(",")
            assert set(columns)=={"id","strategy_id","name","parameters","created_at","backtest_config"}
            self.presets.append(dict(zip(columns,args)))
        else:raise AssertionError("Unexpected SQL: "+sql)
        return self


@pytest.fixture
def preset_api(monkeypatch):
    memory=PresetMemory()
    monkeypatch.setattr(db,"connect",lambda:memory)
    monkeypatch.setattr(db,"audit",lambda *args:memory.audits.append(args))
    monkeypatch.setattr(db,"uid",lambda:"server-id")
    monkeypatch.setattr(db,"init_db",lambda:pytest.fail("Real startup/database initialization forbidden"))
    previous=api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.admin]=lambda:{"id":"test-admin","role":"admin"}
    client=TestClient(api.app)  # No context manager: lifespan is not started.
    try:yield client,memory
    finally:
        client.close();api.app.dependency_overrides.clear();api.app.dependency_overrides.update(previous)


def test_http_round_trip_separates_strategy_and_backtest_settings(preset_api):
    client,memory=preset_api
    response=client.post("/api/presets",json={"id":"untrusted-id","strategy_id":"market","name":"零费率",
        "parameters":{"period":30},"backtest_config":CONFIG})
    assert response.status_code==201,response.text
    saved=response.json()
    assert saved["id"]=="server-id" and saved["parameters"]=={"period":30} and saved["backtest_config"]==CONFIG
    assert json.loads(memory.presets[0]["parameters"])=={"period":30}
    assert json.loads(memory.presets[0]["backtest_config"])==CONFIG
    assert client.get("/api/presets").json()==[saved]
    assert len(memory.audits)==1


def test_legacy_post_and_null_or_missing_old_column_return_empty_config(preset_api):
    client,memory=preset_api
    response=client.post("/api/presets",json={"strategy_id":"market","parameters":{"period":12}})
    assert response.status_code==201 and response.json()["backtest_config"]=={}
    memory.presets[0]["backtest_config"]=None
    assert client.get("/api/presets").json()[0]["backtest_config"]=={}
    del memory.presets[0]["backtest_config"]
    assert client.get("/api/presets").json()[0]["backtest_config"]=={}


@pytest.mark.parametrize("body",[{"parameters":{"initial_capital":10000}},{"parameters":{"period":1}},
    {"backtest_config":{"initial_capital":10000}},{"backtest_config":{**CONFIG,"fee_rate":True}},
    {"backtest_config":{**CONFIG,"leverage":2.5}},{"backtest_config":None}])
def test_http_invalid_preset_has_no_insert_or_audit(preset_api,body):
    client,memory=preset_api
    assert client.post("/api/presets",json={"strategy_id":"market",**body}).status_code==422
    assert memory.presets==[] and memory.audits==[]


def test_missing_strategy_and_maker_slippage_fail_without_writes(preset_api):
    client,memory=preset_api
    assert client.post("/api/presets",json={"strategy_id":"missing"}).status_code==404
    assert client.post("/api/presets",json={"strategy_id":"maker","backtest_config":dict(CONFIG,slippage_bps=2)}).status_code==422
    assert memory.presets==[]


class SchemaMemory:
    def __init__(self,*,fresh=False,lock_result=1,fail_alter=False):
        self.columns={"data_download_jobs":{"waiting_reason","retry_at"}}
        if not fresh:self.columns["presets"]={"id","strategy_id","name","parameters","created_at"}
        self.legacy={"id":"old","parameters":'{"period":12}',"created_at":"original"}
        self.rows=[];self.locked=False;self.lock_result=lock_result;self.fail_alter=fail_alter
        self.alters=[];self.versions=set();self.releases=0
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def execute(self,sql,args=()):
        self.rows=[]
        if sql.startswith("CREATE TABLE IF NOT EXISTS"):
            if "`presets`" in sql and "presets" not in self.columns:
                assert "backtest_config LONGTEXT NULL" in sql
                self.columns["presets"]={"id","strategy_id","name","parameters","created_at","backtest_config"}
        elif sql.startswith("SELECT GET_LOCK"):
            self.locked=self.lock_result==1;self.rows=[db.Row(result=self.lock_result)]
        elif sql.startswith("SELECT RELEASE_LOCK"):
            assert self.locked;self.locked=False;self.releases+=1
        elif "information_schema.COLUMNS" in sql:
            assert self.locked
            table="presets" if "TABLE_NAME='presets'" in sql else "data_download_jobs"
            self.rows=[db.Row(COLUMN_NAME=column) for column in self.columns[table]]
        elif sql.startswith("ALTER TABLE presets"):
            assert self.locked and "backtest_config" not in self.columns["presets"]
            assert sql=="ALTER TABLE presets ADD COLUMN `backtest_config` LONGTEXT NULL"
            if self.fail_alter:raise RuntimeError("synthetic DDL failure")
            self.columns["presets"].add("backtest_config");self.legacy["backtest_config"]=None;self.alters.append(sql)
        elif sql.startswith("INSERT INTO schema_migrations"):self.versions.add(args[0])
        else:raise AssertionError("Unexpected migration SQL: "+sql)
        return self


def schema_memory(monkeypatch,**options):
    memory=SchemaMemory(**options)
    monkeypatch.setenv("MK_DB_NAME","preset_pure_test")
    monkeypatch.setattr(db,"connect",lambda:memory)
    return memory


def test_schema_adds_nullable_column_once_under_lock_and_preserves_legacy_values(monkeypatch):
    memory=schema_memory(monkeypatch)
    db.init_schema();db.init_schema()
    assert len(memory.alters)==1 and memory.releases==2 and not memory.locked
    assert "mysql-004-presets-backtest-config" in memory.versions
    assert memory.legacy=={"id":"old","parameters":'{"period":12}',"created_at":"original","backtest_config":None}


def test_fresh_schema_needs_no_redundant_alter(monkeypatch):
    memory=schema_memory(monkeypatch,fresh=True);db.init_schema()
    assert "backtest_config" in memory.columns["presets"] and memory.alters==[]
    assert "mysql-004-presets-backtest-config" in memory.versions


def test_schema_failure_releases_lock_and_retry_is_safe(monkeypatch):
    memory=schema_memory(monkeypatch,fail_alter=True)
    with pytest.raises(RuntimeError,match="synthetic DDL"):db.init_schema()
    assert not memory.locked and memory.releases==1 and "mysql-004-presets-backtest-config" not in memory.versions
    memory.fail_alter=False;db.init_schema()
    assert len(memory.alters)==1 and "mysql-004-presets-backtest-config" in memory.versions


def test_schema_lock_timeout_prevents_column_change(monkeypatch):
    memory=schema_memory(monkeypatch,lock_result=0)
    with pytest.raises(RuntimeError,match="结构更新锁"):db.init_schema()
    assert memory.alters==[] and "mysql-004-presets-backtest-config" not in memory.versions
