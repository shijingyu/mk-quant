"""Immutable Parquet artifacts; MySQL owns their manifests and lifecycle.

Artifacts are published with fsync + atomic rename before the referencing MySQL
transaction commits. A failed transaction may leave an unreferenced immutable
file, but can never leave a committed row pointing at a partially written file.
Exchange timestamps are always integer Unix milliseconds, independent of UI TZ.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from . import db

FORMAT_VERSION = 1
SERIES = ("curve", "validation_curve", "orders", "decisions")


class ArtifactError(ValueError):
    pass


def artifact_root():
    root = Path(os.environ.get("MK_ARTIFACT_ROOT", "data/artifacts")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    return root


def _arrow():
    import pyarrow as pa
    import pyarrow.parquet as pq
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    return pa, pq


def _digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _logical_digest(path, kind):
    """Hash canonical original rows without retaining a second full dataset."""
    _, pq = _arrow()
    digest = hashlib.sha256()
    digest.update(b"{" if kind == "candles" else b"[")
    first = True
    symbol = None
    ordinal = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, use_threads=False):
        for row in batch.to_pylist():
            payload = json.loads(row["payload"])
            # Analytical columns determine filtering and pagination. They must
            # agree with the lossless payload as well as its content hash.
            if kind == "candles":
                expected = {"timestamp": int(payload["timestamp"]),
                            **{key: float(payload[key]) for key in ("open", "high", "low", "close", "volume")}}
            else:
                expected = {"row_index": ordinal,
                            "timestamp": int(payload["timestamp"]) if payload.get("timestamp") is not None else None,
                            "symbol": str(payload.get("symbol", "")), "split": str(payload.get("split", ""))}
            if any(row.get(key) != value for key, value in expected.items()):
                raise ArtifactError("已存在的文件索引与内容哈希不一致")
            ordinal += 1
            if kind == "candles" and row["symbol"] != symbol:
                if symbol is not None:
                    digest.update(b"],")
                symbol = row["symbol"]
                digest.update(db.dump(symbol).encode() + b":[")
                first = True
            if not first:
                digest.update(b",")
            digest.update(row["payload"].encode())
            first = False
    if kind == "candles" and symbol is not None:
        digest.update(b"]")
    digest.update(b"}" if kind == "candles" else b"]")
    return digest.hexdigest()


def _resolve(manifest):
    if not isinstance(manifest, dict) or manifest.get("storage") != "parquet" or manifest.get("version") != FORMAT_VERSION:
        raise ArtifactError("不支持的行情/回测文件清单")
    name = manifest.get("path", "")
    if not re.fullmatch(r"(?:candles|curve|validation_curve|orders|decisions)/[a-f0-9]{64}\.parquet", str(name)):
        raise ArtifactError("行情/回测文件路径无效")
    root = artifact_root()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise ArtifactError("行情/回测文件缺失，不能使用不完整结果")
    if _digest_file(path) != manifest.get("sha256"):
        raise ArtifactError("行情/回测文件校验失败，内容已改变")
    return path


def _publish(kind, table, content_hash, count, **metadata):
    _, pq = _arrow()
    if kind not in {"candles", *SERIES}:
        raise ArtifactError("不支持的文件类型")
    root = artifact_root()
    directory = (root / kind).resolve()
    if root not in directory.parents:
        raise ArtifactError("文件目录必须位于独立存储根目录内")
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = directory / f"{content_hash}.parquet"
    if root not in path.resolve().parents:
        raise ArtifactError("文件路径必须位于独立存储根目录内")
    # A schema/content-addressed filename is deterministic. Existing files are
    # never overwritten; damaged content fails closed rather than self-healing.
    expected_meta = {b"mk.content_sha256": content_hash.encode(), b"mk.version": b"1", b"mk.kind": kind.encode()}
    table = table.replace_schema_metadata(expected_meta)
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".writing-", suffix=".parquet", dir=directory)
        os.close(fd)
        try:
            pq.write_table(table, temporary, compression="zstd", compression_level=3, row_group_size=32768)
            os.chmod(temporary, 0o640)
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            # link is no-clobber: concurrent identical publications share one
            # content address and neither writer can replace another version.
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Verify both pre-existing content and a concurrent publication winner. A
    # copied footer cannot bless different rows under an existing content hash.
    try:
        existing = pq.read_metadata(path)
        if existing.num_rows != count or any((existing.metadata or {}).get(k) != v for k, v in expected_meta.items()) or _logical_digest(path, kind) != content_hash:
            raise ArtifactError("已存在的文件版本与内容哈希不一致")
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError("行情/回测文件无法校验，拒绝发布") from exc
    return {"storage": "parquet", "version": FORMAT_VERSION, "kind": kind,
            "path": str(path.relative_to(root)), "sha256": _digest_file(path),
            "content_sha256": content_hash, "count": count, **metadata}


def is_manifest(value):
    return isinstance(value, dict) and value.get("storage") == "parquet"


def write_candles(candles):
    """Keep typed analytical columns and exact JSON rows for lossless replay."""
    if not candles or any(not rows for rows in candles.values()):
        raise ArtifactError("每个币种至少需要一根 K 线")
    pa, _ = _arrow()
    records = []
    for symbol in sorted(candles):
        for row in sorted(candles[symbol], key=lambda item: item["timestamp"]):
            records.append({"symbol": symbol, "timestamp": int(row["timestamp"]),
                            **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
                            "payload": db.dump(row)})
    if not records:
        raise ArtifactError("不能保存空行情数据集")
    schema = pa.schema([("symbol", pa.string()), ("timestamp", pa.int64()),
                        *[(key, pa.float64()) for key in ("open", "high", "low", "close", "volume")],
                        ("payload", pa.string())])
    canonical = db.dump({symbol: sorted(rows, key=lambda row: row["timestamp"]) for symbol, rows in sorted(candles.items())})
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return _publish("candles", pa.Table.from_pylist(records, schema=schema), digest, len(records),
                    symbols=sorted(candles), start=min(r["timestamp"] for r in records), end=max(r["timestamp"] for r in records))


def read_candles(value, *, symbols=None, start=None, end=None):
    stored = db.parse(value, {}) if isinstance(value, str) else value
    if not is_manifest(stored):
        # Imported rows can still contain JSON until the migration backfill;
        # this is MySQL data compatibility, never a legacy database connection.
        result = stored or {}
        return {s: [r for r in rows if (start is None or r["timestamp"] >= start) and (end is None or r["timestamp"] < end)]
                for s, rows in result.items() if symbols is None or s in symbols}
    path = _resolve(stored)
    _, pq = _arrow()
    filters = []
    if symbols is not None:
        filters.append(("symbol", "in", list(symbols)))
    if start is not None:
        filters.append(("timestamp", ">=", int(start)))
    if end is not None:
        filters.append(("timestamp", "<", int(end)))
    rows = pq.read_table(path, columns=["symbol", "payload"], filters=filters or None, use_threads=False).to_pylist()
    result = {s: [] for s in stored["symbols"] if symbols is None or s in symbols}
    for row in rows:
        result[row["symbol"]].append(json.loads(row["payload"]))
    return result


def candle_metadata(value):
    stored = db.parse(value, {}) if isinstance(value, str) else value
    if is_manifest(stored):
        return {key: stored[key] for key in ("count", "start", "end")} | {"storage": "parquet"}
    times = [r["timestamp"] for rows in (stored or {}).values() for r in rows]
    return {"count": len(times), "start": min(times) if times else None,
            "end": max(times) if times else None, "storage": "mysql_json_pending_migration"}


def curve_preview(rows, budget=600):
    """Retain both extrema per bucket plus endpoints; never smooth the line."""
    if len(rows) <= budget:
        return list(rows)
    chosen = {0, len(rows) - 1}
    width = max(1, math.ceil((len(rows) - 2) / max(1, (budget - 2) // 2)))
    for first in range(1, len(rows) - 1, width):
        indices = range(first, min(first + width, len(rows) - 1))
        chosen.add(min(indices, key=lambda i: rows[i].get("equity", 0)))
        chosen.add(max(indices, key=lambda i: rows[i].get("equity", 0)))
    return [rows[i] for i in sorted(chosen)]


def write_records(kind, rows):
    if kind not in SERIES:
        raise ArtifactError("不支持的回测明细类型")
    pa, _ = _arrow()
    records = [{"row_index": i, "timestamp": int(row["timestamp"]) if row.get("timestamp") is not None else None,
                "symbol": str(row.get("symbol", "")), "split": str(row.get("split", "")), "payload": db.dump(row)}
               for i, row in enumerate(rows)]
    schema = pa.schema([("row_index", pa.int64()), ("timestamp", pa.int64()), ("symbol", pa.string()), ("split", pa.string()), ("payload", pa.string())])
    digest = hashlib.sha256(db.dump(rows).encode()).hexdigest()
    return _publish(kind, pa.Table.from_pylist(records, schema=schema), digest, len(rows))


def read_records(manifest, *, offset=0, limit=None):
    if offset < 0 or (limit is not None and limit < 0):
        raise ArtifactError("明细分页范围无效")
    _, pq = _arrow()
    path = _resolve(manifest)
    if limit == 0:
        return []
    end = offset + limit if limit is not None else None
    result = []
    for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=["row_index", "payload"], use_threads=False):
        for row in batch.to_pylist():
            if row["row_index"] < offset:
                continue
            if end is not None and row["row_index"] >= end:
                return result
            result.append(json.loads(row["payload"]))
    return result


def store_result(result):
    """All detailed series are independent files; DB keeps summaries/previews."""
    stored = {key: value for key, value in result.items() if key not in SERIES and key != "artifacts"}
    artifacts = dict(result.get("artifacts", {}))
    for kind in SERIES:
        rows = result.get(kind)
        if rows is None:
            continue
        artifacts[kind] = write_records(kind, rows)
        if kind in {"curve", "validation_curve"}:
            stored[kind] = curve_preview(rows)
    stored["artifacts"] = artifacts
    stored["curve_sampling"] = "bucket_min_max_preserves_extrema"
    return stored


def load_result(value, *, detail=False):
    result = db.parse(value, {}) if isinstance(value, str) else dict(value or {})
    result = dict(result)
    if detail:
        for kind, manifest in result.get("artifacts", {}).items():
            if kind in SERIES:
                result[kind] = read_records(manifest)
    else:
        result.pop("orders", None)
        result.pop("decisions", None)
        for kind in ("curve", "validation_curve"):
            result[kind] = curve_preview(result.get(kind, []))
    return result


def backfill_artifacts():
    """Idempotently externalize migrated MySQL rows; run with writers stopped."""
    converted = {"datasets": 0, "experiments": 0}
    with db.connect() as con:
        datasets = con.execute("SELECT id,candles FROM datasets").fetchall()
        experiments = con.execute("SELECT id,result FROM experiments WHERE result IS NOT NULL").fetchall()
    for row in datasets:
        original = db.parse(row["candles"], {})
        if is_manifest(original):
            _resolve(original)
            continue
        manifest = write_candles(original)
        assert read_candles(manifest) == original, "历史行情文件回读不一致"
        with db.connect() as con:
            con.execute("UPDATE datasets SET candles=? WHERE id=? AND candles=?", (db.dump(manifest), row["id"], row["candles"]))
        converted["datasets"] += 1
    for row in experiments:
        original = db.parse(row["result"], {})
        if not original or original.get("artifacts"):
            if original.get("artifacts"):
                for artifact in original["artifacts"].values():
                    _resolve(artifact)
            continue
        stored = store_result(original)
        restored = load_result(stored, detail=True)
        assert all(restored.get(kind, []) == original.get(kind, []) for kind in SERIES), "回测明细文件回读不一致"
        with db.connect() as con:
            con.execute("UPDATE experiments SET result=? WHERE id=? AND result=?", (db.dump(stored), row["id"], row["result"]))
        converted["experiments"] += 1
    return converted


if __name__ == "__main__":
    print(db.dump(backfill_artifacts()))
