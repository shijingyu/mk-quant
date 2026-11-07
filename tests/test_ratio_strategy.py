"""Independent pair-strategy contract and synthetic minute-execution tests.

No exchange/private-account/database operations are used here. These tests
explicitly retain the framework's quantity-at-signal and sequential-leg model.
"""
import copy
import json
from pathlib import Path

import pytest

from backend.engine import run_backtest
from backend.strategy import load_strategy, validate_parameters, validate_strategy_source, validate_symbols

SOURCE_PATH = Path(__file__).resolve().parents[1]/"backend/strategies/btc_eth_usdc_ratio.py"
START = 1735689600000
HOUR = 3_600_000
MINUTE = 60_000
SYMBOLS = ("BTCUSDC", "ETHUSDC")


def source():
    return SOURCE_PATH.read_text()


def context(ratios, *, positions=None, overrides=None, equity=10000.):
    manifest = validate_strategy_source(source())
    params = validate_parameters(manifest,overrides or {})
    histories = {}
    for symbol in SYMBOLS:
        histories[symbol] = [dict(timestamp=START+i*HOUR,open=float(ratio*100 if symbol==SYMBOLS[0] else 100),
            high=float(ratio*100 if symbol==SYMBOLS[0] else 100),low=float(ratio*100 if symbol==SYMBOLS[0] else 100),
            close=float(ratio*100 if symbol==SYMBOLS[0] else 100),volume=100.,close_time=START+(i+1)*HOUR)
            for i,ratio in enumerate(ratios)]
    display = {}
    return dict(params=params,history=histories,candles={s:copy.deepcopy(rows[-1]) for s,rows in histories.items() if rows},
        positions=copy.deepcopy(positions or {}),account=dict(equity=equity,available=equity,leverage=2),
        timestamp=START+len(ratios)*HOUR-MINUTE,decision_time=START+len(ratios)*HOUR,
        signal_interval="1h",base_interval="1m",bar_index=max(0,len(ratios)-1),
        log=lambda *args,**kwargs:None,emit=lambda **kwargs:display.update(kwargs),display=display)


def positions(a=-1.,b=2.):
    return {s+(":LONG" if q>0 else ":SHORT"):dict(symbol=s,quantity=q,average_price=100.,position_side="LONG" if q>0 else "SHORT")
            for s,q in zip(SYMBOLS,(a,b)) if q}


def run_signal(ratios, *, held=None, overrides=None, strategy=None):
    ctx = context(ratios,positions=held,overrides=overrides)
    return (strategy or load_strategy(source())).on_bar(ctx),ctx


def minute_data(hourly_ratios, *, next_open=None, zero_volume=None):
    result = {s:[] for s in SYMBOLS}
    for hour,ratio in enumerate(hourly_ratios):
        for minute in range(60):
            for symbol in SYMBOLS:
                close = ratio*100 if symbol==SYMBOLS[0] else 100.
                opening = next_open if next_open is not None and symbol==SYMBOLS[0] and hour==3 and minute==0 else close
                result[symbol].append(dict(timestamp=START+hour*HOUR+minute*MINUTE,open=opening,
                    high=max(opening,close)*1.1,low=min(opening,close)*.9,close=close,
                    volume=0. if zero_volume==(symbol,hour,minute) else 100.))
    return result


def backtest(data, *, filters=None):
    return run_backtest(source(),dict(lookback=3,entry_z=1.1,exit_z=.25),data,
                        10000.,0.,0.,leverage=2,symbol_filters=filters or {
                            "BTCUSDC":{"tick_size":".1","step_size":".001"},
                            "ETHUSDC":{"tick_size":".01","step_size":".001"}})


def test_manifest_fixed_pair_defaults_and_simulation_costs():
    manifest = validate_strategy_source(source())
    params = validate_parameters(manifest,{})
    assert params["lookback"]==168 and params["entry_z"]==3 and params["exit_z"]==.25
    assert manifest["symbols"]["mode"]=="fixed"
    assert set(manifest["symbols"]["values"])==set(SYMBOLS)
    assert manifest["timeframes"]["primary"]=="1h" and manifest["history_window"]>=720
    assert manifest["account"]["leverage_min"]==2
    assert manifest["account"]["leverage_max"]==100
    assert manifest["execution"]["paper_costs"]=={"fee_rate":0,"slippage_bps":0}
    with pytest.raises(ValueError):
        validate_symbols(manifest,["BTCUSDT","ETHUSDT"])


def test_default_high_ratio_shorts_btc_and_longs_eth_with_equal_signal_notionals():
    intents,ctx = run_signal([1.]*167+[1.2])
    assert [(x["symbol"],x["side"]) for x in intents]==[("BTCUSDC","sell"),("ETHUSDC","buy")]
    assert all(not x.get("reduce_only",False) for x in intents)
    for intent in intents:
        assert intent["quantity"]*ctx["candles"][intent["symbol"]]["close"]==pytest.approx(5000.)


def test_default_low_ratio_longs_btc_and_shorts_eth():
    intents,_ = run_signal([1.]*167+[.8])
    assert [(x["symbol"],x["side"]) for x in intents]==[("BTCUSDC","buy"),("ETHUSDC","sell")]


def test_maximum_account_leverage_preserves_pair_notional_sizing():
    low = context([1.]*167+[1.2])
    high = copy.deepcopy(low)
    high["account"]["leverage"] = 100
    assert load_strategy(source()).on_bar(low) == load_strategy(source()).on_bar(high)


def test_executor_accepts_100x_and_rejects_above_declared_limit():
    data = minute_data([1., 1., 1.2, 1.2])
    output = run_backtest(source(), dict(lookback=3,entry_z=1.1,exit_z=.25),
                          data, 10000., 0., 0., leverage=100, base_interval="1m")
    assert output["account"]["leverage"] == 100
    assert len(output["orders"]) == 2
    with pytest.raises(ValueError, match="杠杆不符合策略声明范围"):
        run_backtest(source(), {}, data, 10000., 0., 0., leverage=101, base_interval="1m")


def test_population_std_current_closed_bar_can_trigger_when_sample_std_would_not():
    # Population z=sqrt(3/2)=1.2247; sample z=1.0. Threshold 1.1 distinguishes them.
    intents,_ = run_signal([1.,2.,3.],overrides=dict(lookback=3,entry_z=1.1,exit_z=.25))
    assert len(intents)==2


@pytest.mark.parametrize("ratios",[[1.,3.],[3.,1.]])
def test_entry_threshold_equality_is_not_a_trade(ratios):
    intents,_ = run_signal(ratios,overrides=dict(lookback=2,entry_z=1.,exit_z=.25))
    assert intents==[]


def test_exit_band_is_strict_and_crossing_the_band_is_not_an_exit():
    overrides=dict(lookback=2,entry_z=2.,exit_z=1.)
    intents,_ = run_signal([1.,3.],held=positions(),overrides=overrides)
    assert intents==[]  # z exactly +1.
    intents,_ = run_signal([3.,1.],held=positions(),overrides=dict(lookback=2,entry_z=2.,exit_z=.25))
    assert intents==[]  # z crossed zero to -1 without landing inside the band.


def test_valid_exit_closes_exact_existing_quantities_without_reversal():
    intents,_ = run_signal([1.,3.,2.],held=positions(-1.234,5.678),
                          overrides=dict(lookback=3,entry_z=1.1,exit_z=.25))
    assert [(i["side"],i["quantity"],i["reduce_only"]) for i in intents]==[("buy",1.234,True),("sell",5.678,True)]


def test_exit_retry_remains_reduce_only_after_z_leaves_band_until_position_is_flat():
    strategy = load_strategy(source())
    overrides=dict(lookback=3,entry_z=1.1,exit_z=.25)
    first,_ = run_signal([1.,3.,2.],held=positions(),overrides=overrides,strategy=strategy)
    restored=load_strategy(source())
    restored.__dict__.update(json.loads(json.dumps(strategy.__dict__,allow_nan=False)))
    strategy=restored
    again,_ = run_signal([1.,2.,10.],held=positions(),overrides=overrides,strategy=strategy)
    assert len(first)==len(again)==2 and all(x["reduce_only"] for x in again)
    json.dumps(strategy.__dict__,allow_nan=False)
    new,_ = run_signal([1.,2.,10.],held={},overrides=overrides,strategy=strategy)
    assert len(new)==2 and all(not x.get("reduce_only",False) for x in new)


@pytest.mark.parametrize("held",[positions(-1.,0.),positions(0.,2.),positions(1.,2.),positions(-1.,-2.)])
def test_orphan_or_same_direction_position_is_reduced_before_warmup(held):
    intents,_ = run_signal([],held=held)
    assert len(intents)==len(held)
    assert all(i["reduce_only"] for i in intents)
    for intent in intents:
        original=held[intent["symbol"]+":"+intent["position_side"]]["quantity"]
        assert intent["quantity"]==abs(original)
        assert intent["side"]==("sell" if original>0 else "buy")


@pytest.mark.parametrize("ratios",[[1.]*167,[1.]*168])
def test_insufficient_history_and_zero_variance_do_not_open(ratios):
    assert run_signal(ratios)[0]==[]


def test_identical_noninteger_ratios_have_exact_zero_variance():
    intents,ctx=run_signal([1.5097261970421827]*3,
                          overrides=dict(lookback=3,entry_z=.5,exit_z=.25))
    assert intents==[]
    assert ctx["display"]["indicators"]["BTCUSDC/ETHUSDC"]["std"]==0.


@pytest.mark.parametrize("change",["different_leg_timestamp","gap","different_current_candle"])
def test_incomplete_or_misaligned_hour_history_cannot_generate_entry(change):
    ctx=context([1.]*167+[1.2])
    if change=="different_leg_timestamp":ctx["history"]["ETHUSDC"][-2]["timestamp"]+=HOUR
    elif change=="gap":
        for symbol in SYMBOLS:ctx["history"][symbol][-2]["timestamp"]-=HOUR
    else:ctx["candles"]["ETHUSDC"]["timestamp"]+=HOUR
    assert load_strategy(source()).on_bar(ctx)==[]


def test_existing_pair_has_no_hidden_z_stop_or_time_stop():
    strategy=load_strategy(source())
    ctx=context([1.]*167+[1.2],positions=positions())
    assert strategy.on_bar(ctx)==[]
    for symbol in SYMBOLS:
        for row in ctx["history"][symbol]:row["timestamp"]+=365*24*HOUR
        ctx["candles"][symbol]["timestamp"]+=365*24*HOUR
    ctx["timestamp"]+=365*24*HOUR;ctx["decision_time"]+=365*24*HOUR
    assert strategy.on_bar(ctx)==[]


def test_minute_engine_waits_for_closed_hour_then_fills_next_open_and_freezes_quantity():
    result=backtest(minute_data([1.,2.,3.,2.5,2.6],next_open=350.))
    orders=result["orders"]
    assert len(orders)==4
    assert [o["timestamp"] for o in orders]==[START+3*HOUR]*2+[START+4*HOUR]*2
    assert orders[0]["signal_timestamp"]==START+3*HOUR-MINUTE
    assert [o["side"] for o in orders]==["sell","buy","buy","sell"]
    assert orders[0]["price"]==350.
    assert orders[0]["quantity"]==16.666 and orders[1]["quantity"]==50.
    assert orders[2]["quantity"]==orders[0]["quantity"] and orders[3]["quantity"]==orders[1]["quantity"]
    assert sum(o["notional"] for o in orders[:2])>10000.  # close-sizing is explicitly not next-open re-budgeting.
    assert all(o["fee"]==0 for o in orders)
    assert result["metrics"]["final_equity"]==pytest.approx(11499.94)


def test_sequential_rejection_leaves_orphan_then_strategy_cleans_it_at_next_hour():
    filters={"BTCUSDC":{"tick_size":".1","step_size":".001"},
             "ETHUSDC":{"tick_size":".01","step_size":".001","min_notional":"6000"}}
    result=backtest(minute_data([1.,2.,3.,2.5,2.6]),filters=filters)
    assert len(result["orders"])==2
    assert [o["symbol"] for o in result["orders"]]==["BTCUSDC","BTCUSDC"]
    assert result["orders"][1]["reduce_only"]
    assert result["orders"][1]["timestamp"]==START+4*HOUR
    assert result["positions"]=={}


def test_final_hour_signal_expires_without_invented_open():
    result=backtest(minute_data([1.,2.,3.]))
    assert result["orders"]==[]
    assert result["decisions"][-1]["status"]=="expired"


def test_future_minute_change_does_not_rewrite_earlier_pair_signal():
    original=minute_data([1.,2.,3.,2.5])
    revised=copy.deepcopy(original)
    for row in revised["BTCUSDC"][180:]:
        for field in ("open","high","low","close"):row[field]*=2
    before,after=backtest(original),backtest(revised)
    assert before["decisions"][:3]==after["decisions"][:3]


@pytest.mark.parametrize("interval,width",[("15m",900000),("4h",14400000),("1d",86400000)])
def test_generic_primary_interval_uses_its_own_contiguous_bar_width(interval,width):
    ctx=context([1.,2.,3.],overrides=dict(lookback=3,entry_z=1.1,exit_z=.25))
    ctx["signal_interval"]=interval
    ctx["timestamp"]=START+3*width-MINUTE
    ctx["decision_time"]=START+3*width
    for symbol in SYMBOLS:
        for i,row in enumerate(ctx["history"][symbol]):
            row["timestamp"]=START+i*width
            row["close_time"]=START+(i+1)*width
        ctx["candles"][symbol]=copy.deepcopy(ctx["history"][symbol][-1])
    assert len(load_strategy(source()).on_bar(ctx))==2
    for symbol in SYMBOLS:ctx["history"][symbol][1]["timestamp"]+=MINUTE
    assert load_strategy(source()).on_bar(ctx)==[]


def test_unsupported_primary_interval_is_rejected():
    ctx=context([1.,2.,3.],overrides=dict(lookback=3,entry_z=1.1,exit_z=.25))
    ctx["signal_interval"]="3h"
    with pytest.raises(ValueError):load_strategy(source()).on_bar(ctx)
