import copy
import csv
import io
import json
from pathlib import Path

import pytest

from backend import storage


@pytest.fixture
def artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MK_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    return tmp_path / "artifacts"


def candles():
    return {"BTCUSDT": [{"timestamp": 1788220800000 + i * 60000, "open": 100 + i,
                         "high": 102 + i, "low": 99 + i, "close": 101 + i,
                         "volume": 0 if i == 0 else 1.25} for i in range(4)]}


def test_parquet_candles_roundtrip_and_filtered_window(artifact_root):
    original = candles()
    manifest = storage.write_candles(original)
    assert manifest["count"] == 4 and manifest["storage"] == "parquet"
    assert manifest["start"] == original["BTCUSDT"][0]["timestamp"]
    assert storage.read_candles(manifest) == original
    assert storage.write_candles(original) == manifest
    window = storage.read_candles(manifest, symbols=["BTCUSDT"], start=manifest["start"] + 60000, end=manifest["start"] + 180000)
    assert window["BTCUSDT"] == original["BTCUSDT"][1:3]
    path = artifact_root / manifest["path"]
    assert path.read_bytes()[:4] == b"PAR1"
    assert not list(artifact_root.rglob(".writing-*"))


def test_artifact_missing_tampered_and_traversal_fail_closed(artifact_root):
    manifest = storage.write_candles(candles())
    with pytest.raises(storage.ArtifactError, match="路径"):
        storage.read_candles({**manifest, "path": "../../private.parquet"})
    path = artifact_root / manifest["path"]
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")
    with pytest.raises(storage.ArtifactError, match="校验"):
        storage.read_candles(manifest)
    path.unlink()
    with pytest.raises(storage.ArtifactError, match="缺失"):
        storage.read_candles(manifest)


@pytest.mark.parametrize("changed_column", ["payload", "timestamp", "volume"])
def test_republishing_cannot_bless_changed_rows_with_original_metadata(artifact_root, changed_column):
    import pyarrow as pa
    import pyarrow.parquet as pq
    original = candles()
    manifest = storage.write_candles(original)
    path = artifact_root / manifest["path"]
    table = pq.read_table(path)
    rows = table.to_pylist()
    if changed_column == "payload":
        rows[0]["payload"] = rows[0]["payload"].replace('"volume":0', '"volume":999')
    else:
        rows[0][changed_column] += 999
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    with pytest.raises(storage.ArtifactError, match="内容哈希"):
        storage.write_candles(original)


def test_artifact_publication_rejects_external_symlink(artifact_root, tmp_path):
    artifact_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifact_root / "candles").symlink_to(outside, target_is_directory=True)
    with pytest.raises(storage.ArtifactError, match="存储根目录"):
        storage.write_candles(candles())


def test_result_files_retain_full_decisions_while_summary_stays_bounded(artifact_root):
    curve = [{"timestamp": 1788220800000 + i * 60000, "equity": 100 + (i % 23)} for i in range(2000)]
    curve[500]["equity"] = 200
    curve[501]["equity"] = 20
    original = {"curve": curve, "validation_curve": [], "orders": [{"timestamp": curve[0]["timestamp"], "symbol": "BTCUSDT", "quantity": 0.01}],
                "decisions": [{"timestamp": p["timestamp"], "inputs": {"history": [1, 2, 3]}, "intent": None} for p in curve], "metrics": {"profit_factor": None, "net_profit": 0}}
    snapshot = copy.deepcopy(original)
    stored = storage.store_result(original)
    assert len(stored["curve"]) <= 600
    assert min(p["equity"] for p in stored["curve"]) == 20
    assert max(p["equity"] for p in stored["curve"]) == 200
    assert "decisions" not in stored and "orders" not in stored
    summary = storage.load_result(stored)
    assert "decisions" not in summary and summary["metrics"]["net_profit"] == 0
    restored = storage.load_result(stored, detail=True)
    for key in storage.SERIES:
        assert restored[key] == original[key]
    assert storage.read_records(stored["artifacts"]["decisions"], offset=999, limit=2) == original["decisions"][999:1001]
    assert original == snapshot


def test_symbol_curves_share_files_and_restore_without_mixing_portfolio(artifact_root):
    a=[{"timestamp":1788220800000+i*60000,"equity":500+(i%31)} for i in range(2000)]
    b=[{"timestamp":p["timestamp"],"equity":500-(i%17)} for i,p in enumerate(a)]
    total=[{"timestamp":x["timestamp"],"equity":x["equity"]+y["equity"]} for x,y in zip(a,b)]
    original={"curve":total,"validation_curve":list(reversed(total)),"metrics":{"initial_equity":1000},"orders":[],"decisions":[],
              "symbol_results":{symbol:{"metrics":{"initial_equity":500},"validation_metrics":{"initial_equity":500},
                  "curve":curve,"validation_curve":list(reversed(curve)),"allocation":{"method":"equal_initial_capital","initial_capital":500}}
                  for symbol,curve in (("BTCUSDT",a),("ETHUSDT",b))}}
    snapshot=copy.deepcopy(original)
    stored=storage.store_result(original)
    assert set(stored["artifacts"])==set(storage.SERIES)
    assert stored["artifacts"]["curve"]["count"]==6000
    assert all(len(value["curve"])<=600 for value in stored["symbol_results"].values())
    records=storage.read_records(stored["artifacts"]["curve"])
    assert {row["symbol"] for row in records}=={"","BTCUSDT","ETHUSDT"}
    restored=storage.load_result(stored,detail=True)
    assert restored["curve"]==total
    for symbol in original["symbol_results"]:
        assert restored["symbol_results"][symbol]==original["symbol_results"][symbol]
    preview=storage.result_preview(original)
    assert all(len(value["curve"])<=600 for value in preview["symbol_results"].values())
    assert original==snapshot


def test_stream_report_keeps_full_portfolio_and_symbol_results(artifact_root):
    curve=[{"timestamp":i*60000,"equity":1000+i,"margin_used":0} for i in range(1200)]
    part=[{**point,"equity":point["equity"]/2} for point in curve]
    original={"curve":curve,"validation_curve":curve,"orders":[{"symbol":"BTCUSDC","split":"train"}],
              "decisions":[{"timestamp":i,"inputs":{"BTCUSDC":{"close":100}},"reason":"等待完整分钟"} for i in range(500)],
              "metrics":{"profit_factor":None},"symbol_results":{symbol:{"curve":part,"validation_curve":part,"metrics":{"initial_equity":500}} for symbol in ("BTCUSDC","ETHUSDC")}}
    stored=storage.store_result(original)
    snapshot=copy.deepcopy(stored)
    chunks=list(storage.stream_report_json(stored))
    restored=json.loads("".join(chunks))
    assert len(chunks)>1
    assert all(restored[k]==original[k] for k in original)
    assert restored["curve_sampling"]=="full_resolution"
    assert stored==snapshot
    # A legacy report with inline arrays follows the same streaming contract.
    assert json.loads("".join(storage.stream_report_json(original)))==original


def test_paginated_records_filter_before_offset_and_csv_ignores_decisions(artifact_root):
    orders=[{"timestamp":i,"symbol":"BTCUSDC" if i%2 else "ETHUSDC","split":"train" if i<6 else "validation"} for i in range(10)]
    stored=storage.store_result({"curve":[{"timestamp":0,"equity":100}],"validation_curve":[],"orders":orders,"decisions":[{"reason":"test"}]})
    manifest=stored["artifacts"]["orders"]
    assert storage.count_records(manifest,symbol="BTCUSDC",split="train")==3
    assert storage.read_records(manifest,symbol="BTCUSDC",split="train",offset=1,limit=1)==[orders[3]]
    assert storage.read_records(manifest,offset=100,limit=10)==[]
    (artifact_root/stored["artifacts"]["decisions"]["path"]).unlink()
    rows=list(csv.DictReader(io.StringIO("".join(storage.stream_equity_csv(stored)))))
    assert rows==[{"split":"train","timestamp":"0","equity":"100","symbol":"TOTAL"}]
    with pytest.raises(storage.ArtifactError,match="缺失"):
        storage.stream_report_json(stored)


def test_aborted_report_stream_closes_active_artifact_iterator(artifact_root,monkeypatch):
    stored=storage.store_result({"orders":[{"symbol":"BTCUSDC"}]})
    closed=[]
    count=[0]
    def rows(*args,**kwargs):
        try:
            for i in range(10000):
                count[0]+=1
                yield json.dumps({"i":i,"reason":"a"*1024})
        finally:
            closed.append(True)
    monkeypatch.setattr(storage,"_iter_payloads",rows)
    stream=storage.stream_report_json(stored)
    next(stream)
    stream.close()
    assert closed==[True] and count[0]<100


def test_streamed_candles_are_canonical_compatible_and_lossless(artifact_root):
    original={symbol:[{**{key:float(row[key]) for key in ("open","high","low","close","volume")},"timestamp":row["timestamp"]} for row in candles()["BTCUSDT"]] for symbol in ("BTCUSDC","ETHUSDC")}
    ticks=[]
    with storage.CandleStreamWriter(list(reversed(original)),heartbeat=lambda:ticks.append(True)) as writer:
        for symbol,rows in original.items():
            writer.append(symbol,rows[:1])
            writer.append(symbol,rows[1:])
        streamed=writer.finish()
    assert streamed==storage.write_candles(original)
    assert storage.read_candles(streamed)==original
    assert streamed["count"]==8 and len(ticks)>=4
    assert not list(artifact_root.rglob(".writing-*"))


@pytest.mark.parametrize("mutation",["gap","duplicate","unaligned","bad_ohlc","missing_symbol"])
def test_stream_rejects_invalid_pages_without_publishing(artifact_root,mutation):
    rows=candles()["BTCUSDT"]
    if mutation=="gap":rows[2]["timestamp"]+=60000
    elif mutation=="duplicate":rows[2]["timestamp"]=rows[1]["timestamp"]
    elif mutation=="unaligned":rows[2]["timestamp"]+=1
    elif mutation=="bad_ohlc":rows[2]["high"]=1
    with pytest.raises(storage.ArtifactError):
        with storage.CandleStreamWriter(["BTCUSDT","ETHUSDT"] if mutation=="missing_symbol" else ["BTCUSDT"]) as writer:
            writer.append("BTCUSDT",rows[:2])
            writer.append("BTCUSDT",rows[2:])
            writer.finish()
    assert not list(artifact_root.rglob("*.parquet"))


def test_lifecycle_lock_serializes_processes_and_is_reentrant(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import time
    monkeypatch.setenv("MK_ARTIFACT_ROOT", str(tmp_path / "locked-artifacts"))
    marker = tmp_path / "acquired"
    script = "from backend.storage import lifecycle_lock; from pathlib import Path; import sys;\nwith lifecycle_lock(): Path(sys.argv[1]).write_text('acquired')"
    with storage.lifecycle_lock():
        with storage.lifecycle_lock():
            child = subprocess.Popen([sys.executable, '-c', script, str(marker)], env=os.environ.copy())
            try:
                time.sleep(0.3)
                assert child.poll() is None
                assert not marker.exists()
            except BaseException:
                child.kill(); child.wait()
                raise
    assert child.wait(timeout=10) == 0
    assert marker.read_text() == 'acquired'


def test_cleanup_rejects_symlinks_and_changed_files(artifact_root, tmp_path):
    from backend import artifact_cleanup
    manifest = storage.write_candles(candles())
    path = artifact_root / manifest['path']
    original = path.read_bytes()
    path.write_bytes(b'changed')
    with pytest.raises(storage.ArtifactError):
        artifact_cleanup._remove(manifest)
    assert path.exists()
    path.unlink()
    outside = tmp_path / 'outside.parquet'
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(storage.ArtifactError):
        artifact_cleanup._remove(manifest)
    assert outside.read_bytes() == original
    path.unlink()
    assert artifact_cleanup._remove(manifest) == 0


def test_minute_validation_streams_indices_without_loading_candles(artifact_root, monkeypatch):
    data = candles()
    data['ETHUSDT'] = copy.deepcopy(data['BTCUSDT'])
    manifest = storage.write_candles(data)
    start = data['BTCUSDT'][0]['timestamp']
    _, pq = storage._arrow()
    monkeypatch.setattr(pq, 'read_table', lambda *a, **kw: pytest.fail('Full table materialization is forbidden'))
    result = storage.validate_minute_windows(manifest, ['BTCUSDT', 'ETHUSDT'], [(start, start + 120000), (start + 120000, start + 240000)])
    assert result[0]['BTCUSDT'] == result[0]['ETHUSDT'] == [start, start+60000, 2]
    assert storage.read_candles(manifest, symbols=['BTCUSDT'], start=start+60000, end=start+180000)['BTCUSDT'] == data['BTCUSDT'][1:3]


@pytest.mark.parametrize('problem', ['gap', 'unaligned', 'different_window', 'insufficient'])
def test_minute_validation_preserves_rejections(artifact_root, problem):
    data = candles()
    data['ETHUSDT'] = copy.deepcopy(data['BTCUSDT'])
    start = data['BTCUSDT'][0]['timestamp']
    if problem == 'gap':
        data['ETHUSDT'].pop(1)
    elif problem == 'unaligned':
        data['ETHUSDT'][1]['timestamp'] += 1
    elif problem == 'different_window':
        data['ETHUSDT'].pop(0)
    else:
        data['ETHUSDT'] = data['ETHUSDT'][:1]
    manifest = storage.write_candles(data)
    with pytest.raises(ValueError):
        storage.validate_minute_windows(manifest, ['BTCUSDT', 'ETHUSDT'], [(start, start + 240000)])


def test_decisions_symbol_and_split_filter(tmp_path,monkeypatch):
    monkeypatch.setenv('MK_ARTIFACT_ROOT',str(tmp_path))
    rows=[{'timestamp':i,'split':'train' if i<2 else 'validation','inputs':{'BTCUSDT':{}},'intents':[]} for i in range(3)]
    rows.append({'timestamp':4,'split':'validation','inputs':{'ETHUSDT':{}},'intents':[]})
    manifest=storage.write_records('decisions',rows)
    assert storage.count_records(manifest,symbol='BTCUSDT',split='validation')==1
    assert storage.read_records(manifest,symbol='BTCUSDT',split='validation')==[rows[2]]
    assert storage.read_records(manifest,symbol='BTCUSDT',offset=1,limit=1)==[rows[1]]
    assert storage.count_records(manifest,symbol='SOLUSDT')==0
