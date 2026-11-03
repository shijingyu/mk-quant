"""Minute-derived strategy windows persist independently from execution ticks."""
import hashlib

from backend import db
from backend.worker import _runtime_frames, runtime_tick
from test_maker_runtime import maker, feed, command, read, MANIFEST, SOURCE


def bars(start, count):
    return [{"timestamp":start+i*60000,"open":100+i,"high":102+i,"low":99+i,"close":101+i,"volume":10} for i in range(count)]


def test_runtime_completed_windows_survive_short_shared_cache():
    start=1767225600000  # UTC aligned to a complete 5-minute bucket.
    manifest={"timeframes":{"primary":"5m","additional":["15m"]},"history_window":3}
    history=bars(start,15)
    frames,ready=_runtime_frames(manifest,["BTCUSDC"],{"BTCUSDC":history[:10]},start+9*60000)
    assert ready and len(frames.context()["history"]["BTCUSDC"])==2
    restored,ready=_runtime_frames(manifest,["BTCUSDC"],{"BTCUSDC":history[10:]},start+14*60000,frames.dump())
    assert ready
    context=restored.context()
    assert len(context["history"]["BTCUSDC"])==3
    assert context["timeframe_candles"]["15m"]["BTCUSDC"]["close"]==115
    assert context["decision_time"]==start+15*60000
    assert context["execution_candles"]["BTCUSDC"]["timestamp"]==start+14*60000


def test_runtime_gap_does_not_build_incomplete_or_future_bucket():
    start=1767225600000
    manifest={"timeframes":{"primary":"5m","additional":[]},"history_window":3}
    history=bars(start,10)
    missing=history[:2]+history[3:]
    frames,ready=_runtime_frames(manifest,["BTCUSDC"],{"BTCUSDC":missing},start+4*60000)
    assert not ready and frames.context()["history"]["BTCUSDC"]==[]
    restored,ready=_runtime_frames(manifest,["BTCUSDC"],{"BTCUSDC":missing},start+9*60000,frames.dump())
    assert ready
    assert len(restored.context()["history"]["BTCUSDC"])==1
    assert restored.context()["candles"]["BTCUSDC"]["timestamp"]==start+5*60000


def test_maker_higher_period_decision_keeps_minute_fills_after_restart(maker):
    maker["start"]=(maker["start"]//180000)*180000
    manifest={**MANIFEST,"timeframes":{"primary":"3m","additional":[]}}
    source=SOURCE.replace(repr(MANIFEST),repr(manifest))
    with db.connect() as con:
        con.execute("UPDATE strategies SET manifest=?,source=?,source_hash=? WHERE id=?",(db.dump(manifest),source,hashlib.sha256(source.encode()).hexdigest(),MANIFEST["id"]))
    command(maker,"resume")
    for minute in (0,1):
        feed(maker,minute)
        assert runtime_tick(maker["id"],"first-worker")
        assert not read(maker)["orders"]
    feed(maker,2)
    runtime_tick(maker["id"],"first-worker")
    placed=read(maker)
    assert len(placed["orders"])==2 and not placed["fills"]
    assert placed["snapshot"]["bar_index"]==1
    feed(maker,3,high=104,low=96)
    runtime_tick(maker["id"],"first-worker")
    filled=read(maker)
    assert len(filled["fills"])==2
    assert filled["snapshot"]["bar_index"]==1
    with db.connect() as con:
        con.execute("UPDATE instances SET lease_until=0 WHERE account_id=?",(maker["id"],))
    feed(maker,4)
    runtime_tick(maker["id"],"replacement-worker")
    restored=read(maker)
    assert len(restored["fills"])==2
    assert restored["snapshot"]["bar_index"]==1
    assert restored["snapshot"]["frames"]["last_timestamp"]==maker["start"]+4*60000
    feed(maker,5)
    runtime_tick(maker["id"],"replacement-worker")
    assert read(maker)["snapshot"]["bar_index"]==2


def test_market_primary_signal_executes_once_on_next_minute(maker):
    maker["start"]=(maker["start"]//180000)*180000
    manifest={**MANIFEST,"account":{**MANIFEST["account"],"position_modes":["one_way"]},"timeframes":{"primary":"3m","additional":[]}}
    manifest.pop("execution")
    source="MANIFEST = "+repr(manifest)+'''\nclass Strategy:
    def on_bar(self, context):
        if context["bar_index"] == 0:
            return [{"symbol":"BTCUSDC","side":"buy","quantity":1}]
        return []
'''
    with db.connect() as con:
        con.execute("UPDATE strategies SET manifest=?,source=?,source_hash=? WHERE id=?",(db.dump(manifest),source,hashlib.sha256(source.encode()).hexdigest(),MANIFEST["id"]))
    command(maker,"resume")
    for minute in (0,1,2):
        feed(maker,minute,high=102,low=98)
        runtime_tick(maker["id"],"market-worker")
    assert not read(maker)["fills"]
    for minute in (3,4,5,6):
        feed(maker,minute,high=102,low=98)
        runtime_tick(maker["id"],"market-worker")
    state=read(maker)
    assert len(state["fills"])==1
    assert state["fills"][0]["timestamp"]==maker["start"]+3*60000
    assert state["snapshot"]["bar_index"]==2
    assert state["snapshot"]["pending"]==[]
