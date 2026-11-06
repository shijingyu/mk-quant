"""Export and native-minute input smoke tests; no external data or accounts."""
import ast
import copy
import csv
import gzip

import pytest

from backend.engine import run_backtest
from backend.strategy import load_strategy, validate_strategy_source
from scripts.backtest_pair_ratio import SOURCE, date_ms, export_source, read_minutes

START=1735689600000


@pytest.mark.parametrize("interval,lookback",[("15m",2880),("1h",168),("4h",180),("1d",90)])
def test_export_preserves_strategy_code_and_declares_correct_interval_window(interval,lookback):
    result=export_source(interval,lookback,3.,.25)
    manifest=validate_strategy_source(result)
    assert manifest["timeframes"]=={"primary":interval,"additional":[]}
    assert manifest["history_window"]>=lookback
    assert manifest["parameters"]["properties"]["lookback"]["default"]==lookback
    assert manifest["parameters"]["properties"]["lookback"]["maximum"]>=lookback
    assert manifest["execution"]["paper_costs"]=={"fee_rate":0.,"slippage_bps":0.}
    assert manifest["account"]["leverage_min"] == 2
    assert manifest["account"]["leverage_max"] == 100
    original_class=next(n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.ClassDef))
    exported_class=next(n for n in ast.parse(result).body if isinstance(n,ast.ClassDef))
    assert ast.dump(original_class)==ast.dump(exported_class)
    assert callable(load_strategy(result).on_bar)


@pytest.mark.parametrize("args",[("3h",168,3.,.25),("1h",1,3.,.25),("1h",2881,3.,.25),
                                  ("1h",168,1.,1.),("1h",168,3.,0.)])
def test_export_rejects_unsupported_period_or_invalid_cross_parameters(args):
    with pytest.raises(ValueError):export_source(*args)


def test_csv_gzip_reader_uses_half_open_requested_utc_range(tmp_path):
    path=tmp_path/"synthetic-1m.csv.gz"
    with gzip.open(path,"wt",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=["timestamp","open","high","low","close","volume"])
        writer.writeheader()
        for i in range(4):writer.writerow(dict(timestamp=START+i*60000,open=100,high=101,low=99,close=100,volume=10))
    rows=read_minutes(path,START+60000,START+180000)
    assert [r["timestamp"] for r in rows]==[START+60000,START+120000]
    assert date_ms("2025-01-01T08:00:00+08:00")==date_ms("2025-01-01T00:00:00Z")==START


@pytest.mark.parametrize("bad",["15m_spacing","missing_minute","misaligned_time","symbol_gap","nonfinite_price"])
def test_exported_runner_engine_rejects_coarse_or_invalid_minute_inputs(bad):
    rows=[dict(timestamp=START+i*60000,open=100.,high=101.,low=99.,close=100.,volume=10.) for i in range(4)]
    data={s:copy.deepcopy(rows) for s in ["BTCUSDC","ETHUSDC"]}
    if bad=="15m_spacing":
        for bars in data.values():
            for i,row in enumerate(bars):row["timestamp"]=START+i*900000
    elif bad=="missing_minute":
        for bars in data.values():bars.pop(1)
    elif bad=="misaligned_time":
        for bars in data.values():
            for row in bars:row["timestamp"]+=1
    elif bad=="symbol_gap":data["ETHUSDC"].pop(1)
    else:data["BTCUSDC"][0]["open"]=float("nan")
    with pytest.raises(ValueError):
        run_backtest(export_source(),{},data,10000.,0.,0.,leverage=2,base_interval="1m")
