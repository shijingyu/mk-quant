"""Minute execution, completed timeframe synthesis and shared-ledger attribution."""
import copy
import json
from decimal import Decimal

import pytest

from backend.engine import run_backtest, run_split_backtest
from backend.strategy import validate_strategy_source
from backend.timeframes import MINUTE_MS, MinuteFrames
from tests.test_engine import BASE, source, bars
from tests.test_maker_engine import book, bar, entry, SYMBOL, source as maker_source

START=1735689600000


def code(body, primary="1m", additional=None):
    manifest=copy.deepcopy(BASE)
    manifest["timeframes"]={"primary":primary,"additional":additional or []}
    return source(body,manifest=manifest)


def run(script,prices,**options):
    return run_backtest(script,{}, {"BTCUSDT":bars(prices)},1000,options.pop("fee_rate",0),options.pop("slippage_bps",0),**options)


def test_primary_five_minute_decision_uses_only_complete_candles_and_next_minute_fill():
    script=code("context['emit'](indicators={'primary':context['candles']['BTCUSDT'], 'extra':context['timeframe_candles']['15m'], 'clock':context['decision_time'], 'index':context['bar_index']})\n"
                "return [{'symbol':'BTCUSDT','side':'buy','quantity':1}] if context['bar_index']==0 else []", "5m", ["15m"])
    result=run(script,list(range(100,116)))
    assert len(result["equity"])==16
    assert [d["timestamp"] for d in result["decisions"]]==[START+i*MINUTE_MS for i in (4,9,14)]
    first=result["decisions"][0]["indicators"]
    assert first["primary"]["timestamp"]==START
    assert first["primary"]["open"]==100 and first["primary"]["close"]==104
    assert first["primary"]["volume"]==500
    assert first["primary"]["high"]==pytest.approx(114.4)
    assert first["extra"]=={} and first["clock"]==START+5*MINUTE_MS
    extra=result["decisions"][2]["indicators"]["extra"]["BTCUSDT"]
    assert extra["close"]==114 and extra["close_time"]==START+15*MINUTE_MS
    assert result["orders"][0]["price"]==105
    assert result["orders"][0]["timestamp"]==START+5*MINUTE_MS


def test_partial_leading_and_trailing_buckets_never_call_strategy():
    data={"BTCUSDT":bars([100]*12,start=START+MINUTE_MS)}
    result=run_backtest(code("return []","5m"),{},data,1000,0,0)
    assert len(result["decisions"])==1
    assert result["decisions"][0]["inputs"]["BTCUSDT"]["timestamp"]==START+5*MINUTE_MS
    assert len(result["equity"])==12


def test_mutating_future_minutes_cannot_change_earlier_decision():
    script=code("context['emit'](indicators=context['timeframe_candles'])\nreturn []","5m",["15m"])
    earlier=run(script,[100]*16)
    changed=run(script,[100]*5+[500]*11)
    assert earlier["decisions"][0]==changed["decisions"][0]


@pytest.mark.parametrize("change",["hour","gap","offset","symbol_gap"])
def test_coarse_missing_or_unaligned_source_is_rejected(change):
    data={"BTCUSDT":bars([100]*4)}
    if change=="hour":data["BTCUSDT"]=bars([100]*4,step=3600000)
    if change=="gap":data["BTCUSDT"].pop(1)
    if change=="offset":data["BTCUSDT"]=bars([100]*4,start=START+1)
    if change=="symbol_gap":data["ETHUSDT"]=bars([10]*4)[1:]
    with pytest.raises(ValueError,match="1 分钟"):
        run_backtest(code("return []"),{},data,1000,0,0)
    with pytest.raises(ValueError,match="1 分钟"):
        run_backtest(code("return []"),{},{"BTCUSDT":bars([100]*4)},1000,0,0,base_interval="1h")


def test_accumulator_dump_restore_and_runtime_gap_discard_partial_bucket():
    accumulator=MinuteFrames(["BTCUSDT"],{"primary":"5m","additional":[]},3)
    for row in bars([100,101]):assert not accumulator.push({"BTCUSDT":row})
    restored=MinuteFrames.load(json.loads(json.dumps(accumulator.dump())))
    for row in bars([102,103,104],start=START+2*MINUTE_MS):
        ready=restored.push({"BTCUSDT":row})
    assert ready and restored.context()["candles"]["BTCUSDT"]["volume"]==500
    assert not restored.push({"BTCUSDT":bars([107],start=START+7*MINUTE_MS)[0]},allow_gap=True)
    assert not restored.push({"BTCUSDT":bars([108],start=START+8*MINUTE_MS)[0]})
    assert not restored.push({"BTCUSDT":bars([109],start=START+9*MINUTE_MS)[0]})
    for row in bars([110]*5,start=START+10*MINUTE_MS):ready=restored.push({"BTCUSDT":row})
    assert ready and restored.primary_index==1
    assert len(restored.context()["history"]["BTCUSDT"])==2


def test_daily_buckets_use_utc_midnight_even_when_ui_is_beijing():
    accumulator=MinuteFrames(["BTCUSDT"],{"primary":"1d","additional":[]},2)
    for row in bars([100]*1440):ready=accumulator.push({"BTCUSDT":row})
    assert ready
    assert accumulator.context()["candles"]["BTCUSDT"]["timestamp"]==START
    assert accumulator.context()["decision_time"]==START+86400000


@pytest.mark.parametrize("side,price,opening,high,low",[("LONG",99,98,98.5,97),("SHORT",101,102,103,101.5)])
def test_gap_entirely_past_limit_never_fills_until_inclusive_touch(side,price,opening,high,low):
    b=book()
    b.submit(entry(side=side,price=price),0,{SYMBOL:100})
    b.process_bar(bar(1,opening=opening,high=high,low=low,close=opening))
    assert not b.ledger.orders
    b.process_bar(bar(2,opening=100,high=101,low=99,close=100))
    assert len(b.ledger.orders)==1 and b.ledger.orders[0]["price"]==price


def test_slippage_or_tick_rounding_outside_minute_range_never_fills():
    script=code("return [{'symbol':'BTCUSDT','side':'buy','quantity':1}]")
    data=bars([100,100])
    data[1].update(high=100.01,low=99.99)
    for slip,filters in [(10,None),(0,{"BTCUSDT":{"tick_size":"3"}})]:
        result=run_backtest(script,{}, {"BTCUSDT":data},1000,.001,slip,symbol_filters=filters)
        assert not result["orders"] and result["account"]["equity"]==1000
        assert "最高价之间" in result["decisions"][0]["risk"][0]["reason"]


def test_symbol_reports_attribute_shared_execution_with_additive_equity_and_fees():
    script=code("if context['bar_index']==0:\n"
                "    return [{'symbol':'BTCUSDT','side':'buy','quantity':6}, {'symbol':'ETHUSDT','side':'sell','quantity':2}]\n"
                "if context['bar_index']==2:\n"
                "    return [{'symbol':'BTCUSDT','side':'sell','quantity':3,'reduce_only':True}, {'symbol':'ETHUSDT','side':'buy','quantity':2,'reduce_only':True}]\n"
                "return []")
    data={"BTCUSDT":bars([100,100,110,90,95]),"ETHUSDT":bars([10,10,9,8,9])}
    events=[]
    result=run_backtest(script,{},data,1000,.001,0,progress=events.append)
    reports=result["symbol_results"]
    assert set(reports)==set(data)
    assert sum(report["metrics"]["total_orders"] for report in reports.values())==len(result["orders"])
    assert all(report["metrics"]["initial_equity"]==500 for report in reports.values())
    # BTC used more than its attribution base: these are not independent 500-U accounts.
    assert reports["BTCUSDT"]["orders"][0]["quantity"]==6
    for index,point in enumerate(result["equity"]):
        assert sum(report["equity"][index]["equity"] for report in reports.values())==pytest.approx(point["equity"],abs=1e-9)
        assert sum(report["equity"][index]["margin_used"] for report in reports.values())==pytest.approx(point["margin_used"],abs=1e-9)
    assert sum(report["metrics"]["realized_pnl"] for report in reports.values())==pytest.approx(result["metrics"]["realized_pnl"])
    assert sum(report["metrics"]["total_fees"] for report in reports.values())==pytest.approx(result["metrics"]["total_fees"])
    assert sum(len(event.get("symbol_equity",{}).get("BTCUSDT",[])) for event in events)==5
    json.dumps(result,allow_nan=False)


def test_three_symbol_fractional_allocation_and_no_trade_metrics_are_honest():
    data={symbol:bars([100]*3) for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT")}
    result=run_backtest(code("return []"),{},data,1000,0,0)
    assert sum(r["metrics"]["initial_equity"] for r in result["symbol_results"].values())==pytest.approx(1000)
    assert all(r["metrics"]["win_rate"] is None and r["metrics"]["profit_factor"] is None for r in result["symbol_results"].values())


def test_maker_dual_legs_and_protection_are_attributed_to_their_symbol():
    manifest=validate_strategy_source(maker_source())
    manifest["symbols"]={"mode":"fixed","values":["BTCUSDT","ETHUSDT"],"min_count":2}
    intents=[{**entry(symbol+leg,side=leg),"symbol":symbol} for symbol in manifest["symbols"]["values"] for leg in ("LONG","SHORT")]
    script=source("return "+repr(intents)+" if context['bar_index']==0 else []",manifest=manifest)
    minute_bars=[bar(START),bar(START+MINUTE_MS),bar(START+2*MINUTE_MS,high=111,low=100),
                 bar(START+3*MINUTE_MS,opening=109,high=110,low=108,close=109)]
    data={symbol:[dict(value[SYMBOL]) for value in minute_bars] for symbol in manifest["symbols"]["values"]}
    result=run_backtest(script,{},data,1000,.001,0)
    assert len(result["orders"])==8
    assert result["positions"]=={}
    reports=result["symbol_results"]
    for report in reports.values():
        assert report["metrics"]["total_orders"]==4
        assert {order["position_side"] for order in report["orders"]}=={"LONG","SHORT"}
        assert report["account"]["margin_used"]==0
        assert report["equity"][0]["pending_margin"]==200
    for index,point in enumerate(result["equity"]):
        for field in ("equity","margin_used","pending_margin","unrealized_pnl"):
            assert sum(report["equity"][index][field] for report in reports.values())==pytest.approx(point[field])


def test_split_exposes_each_symbols_train_validation_and_delta():
    data={"BTCUSDT":bars([100]*10),"ETHUSDT":bars([10]*10)}
    result=run_split_backtest(code("return []"),{},data,1000,0,0,START,START+5*MINUTE_MS,START+5*MINUTE_MS,START+10*MINUTE_MS)
    assert result["symbol_results"]["BTCUSDT"]["train"]["metrics"]["initial_equity"]==500
    assert result["symbol_results"]["BTCUSDT"]["differences"]["final_equity"]==0
    assert result["reproducibility"]["base_interval"]=="1m"


@pytest.mark.parametrize("definition",[{"primary":"7h"},{"primary":"1m","additional":["1m"]},{"primary":"1h","additional":["5m","5m"]},{"primary":"1M"}])
def test_invalid_timeframe_contract_is_rejected(definition):
    manifest=copy.deepcopy(BASE);manifest["timeframes"]=definition
    with pytest.raises(ValueError,match="timeframes"):
        validate_strategy_source(source("return []",manifest=manifest))
