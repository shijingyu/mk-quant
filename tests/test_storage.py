import copy
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
