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


def _iter_payloads(path, *, offset=0, limit=None, symbol=None, split=None):
    """Read bounded batches; a slow/aborted HTTP reader never retains a report."""
    _, pq = _arrow()
    skipped = emitted = 0
    if limit == 0:
        return
    with pq.ParquetFile(path) as parquet:
        for batch in parquet.iter_batches(batch_size=32, columns=["symbol", "split", "payload"], use_threads=False):
            for row in batch.to_pylist():
                if symbol is not None and row["symbol"] != symbol:
                    continue
                if split is not None and row["split"] != split:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                yield row["payload"]
                emitted += 1
                if limit is not None and emitted >= limit:
                    return


def iter_records(manifest, *, offset=0, limit=None, symbol=None, split=None):
    if offset < 0 or (limit is not None and limit < 0):
        raise ArtifactError("明细分页范围无效")
    path = _resolve(manifest)
    for payload in _iter_payloads(path, offset=offset, limit=limit, symbol=symbol, split=split):
        yield json.loads(payload)


def read_records(manifest, *, offset=0, limit=None, symbol=None, split=None):
    return list(iter_records(manifest, offset=offset, limit=limit, symbol=symbol, split=split))


def count_records(manifest, *, symbol=None, split=None):
    path = _resolve(manifest)
    if symbol is None and split is None:
        return int(manifest["count"])
    _, pq = _arrow()
    count = 0
    # Counts and filters use compact typed columns, never large decision JSON.
    with pq.ParquetFile(path) as parquet:
        for batch in parquet.iter_batches(batch_size=4096, columns=["symbol", "split"], use_threads=False):
            count += sum((symbol is None or row["symbol"] == symbol) and
                         (split is None or row["split"] == split) for row in batch.to_pylist())
    return count


def _chunk_text(parts, budget=65536):
    buffer, size = [], 0
    try:
        for part in parts:
            buffer.append(part)
            size += len(part)
            if size >= budget:
                yield "".join(buffer)
                buffer, size = [], 0
        if buffer:
            yield "".join(buffer)
    finally:
        close = getattr(parts, "close", None)
        if close:
            close()


def stream_report_json(report):
    """Preserve the full report contract without expanding all Parquet JSON.

    Integrity checks happen before response headers. Generators own each file
    only while iterating it, and close it when the client cancels consumption.
    """
    paths = {kind: _resolve(manifest) for kind, manifest in report.get("artifacts", {}).items() if kind in SERIES}
    scoped = report.get("curve_layout") == "portfolio_and_symbol_v1"

    def array(kind, fallback, symbol=None):
        yield "["
        first = True
        if kind in paths:
            payloads = _iter_payloads(paths[kind], symbol=(symbol or "") if scoped and kind in {"curve", "validation_curve"} else None)
        else:
            payloads = (db.dump(row) for row in fallback)
        try:
            for payload in payloads:
                if not first:
                    yield ","
                if scoped and kind in paths and kind in {"curve", "validation_curve"}:
                    point = json.loads(payload)
                    point.pop("symbol", None)
                    payload = db.dump(point)
                yield payload
                first = False
        finally:
            payloads.close()
        yield "]"

    def fields(item, symbol=None):
        yield "{"
        keys = list(item)
        if symbol is None:
            keys.extend(kind for kind in paths if kind not in item)
        for index, key in enumerate(keys):
            if index:
                yield ","
            yield db.dump(key) + ":"
            if key in SERIES and (symbol is None or key in {"curve", "validation_curve"}):
                yield from array(key, item.get(key, []), symbol=symbol)
            elif key == "symbol_results":
                yield "{"
                for symbol_index, (name, result) in enumerate(item[key].items()):
                    if symbol_index:
                        yield ","
                    yield db.dump(name) + ":"
                    yield from fields(result, symbol=name)
                yield "}"
            elif key == "curve_sampling" and paths:
                yield db.dump("full_resolution")
            else:
                yield db.dump(item[key])
        yield "}"

    return _chunk_text(fields(report))


def stream_equity_csv(report):
    """Read only equity files; CSV never loads order or decision artifacts."""
    import csv
    import io
    paths = {kind: _resolve(report["artifacts"][kind]) for kind in ("curve", "validation_curve") if kind in report.get("artifacts", {})}

    def lines():
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["split", "timestamp", "equity", "symbol"])
        yield stream.getvalue()
        for split, kind in (("train", "curve"), ("validation", "validation_curve")):
            if kind in paths:
                payloads = _iter_payloads(paths[kind])
                rows = (json.loads(payload) for payload in payloads)
            else:
                payloads = None
                rows = iter([dict(row, symbol="") for row in report.get(kind, [])] +
                            [dict(row, symbol=symbol) for symbol, item in report.get("symbol_results", {}).items() for row in item.get(kind, [])])
            try:
                for row in rows:
                    stream.seek(0)
                    stream.truncate(0)
                    writer.writerow([split, row.get("timestamp", row.get("time")), row.get("equity"), row.get("symbol") or "TOTAL"])
                    yield stream.getvalue()
            finally:
                if payloads is not None:
                    payloads.close()
    return _chunk_text(lines())


def result_preview(result):
    """Bound portfolio and symbol curves independently without mixing extrema."""
    preview = {key: value for key, value in result.items() if key not in {"orders", "decisions", "symbol_results"}}
    for kind in ("curve", "validation_curve"):
        preview[kind] = curve_preview(result.get(kind, []))
    if "symbol_results" in result:
        preview["symbol_results"] = {}
        for symbol, value in result["symbol_results"].items():
            item = {key: item for key, item in value.items() if key not in SERIES}
            for kind in ("curve", "validation_curve"):
                item[kind] = curve_preview(value.get(kind, []))
            preview["symbol_results"][symbol] = item
    preview["curve_sampling"] = "bucket_min_max_preserves_extrema"
    return preview


def store_result(result):
    """All detailed series are independent files; DB keeps summaries/previews."""
    stored = {key: value for key, value in result.items() if key not in SERIES and key not in {"artifacts", "symbol_results"}}
    symbols = result.get("symbol_results", {})
    if symbols:
        stored["symbol_results"] = {symbol: {key: item for key, item in value.items() if key not in SERIES}
                                    for symbol, value in symbols.items()}
        stored["curve_layout"] = "portfolio_and_symbol_v1"
    artifacts = dict(result.get("artifacts", {}))
    for kind in SERIES:
        rows = result.get(kind)
        if rows is None:
            continue
        # The empty symbol denotes the portfolio. Each other group is an
        # attribution from the same execution, not an independent strategy run.
        if kind in {"curve", "validation_curve"} and symbols:
            scoped_rows = [dict(row, symbol="") for row in rows]
            for symbol, value in symbols.items():
                scoped_rows.extend(dict(row, symbol=symbol) for row in value.get(kind, []))
                stored["symbol_results"][symbol][kind] = curve_preview(value.get(kind, []))
            artifacts[kind] = write_records(kind, scoped_rows)
        else:
            artifacts[kind] = write_records(kind, rows)
        if kind in {"curve", "validation_curve"}:
            stored[kind] = curve_preview(rows)
    stored["artifacts"] = artifacts
    stored["curve_sampling"] = "bucket_min_max_preserves_extrema"
    return stored


def load_result(value, *, detail=False, preview=True):
    result = db.parse(value, {}) if isinstance(value, str) else dict(value or {})
    result = dict(result)
    if "symbol_results" in result:
        result["symbol_results"] = {symbol: dict(item) for symbol, item in result["symbol_results"].items()}
    if detail:
        for kind, manifest in result.get("artifacts", {}).items():
            if kind in SERIES:
                rows = read_records(manifest)
                if kind in {"curve", "validation_curve"} and result.get("curve_layout") == "portfolio_and_symbol_v1":
                    groups = {symbol: [] for symbol in ("", *result.get("symbol_results", {}))}
                    for row in rows:
                        symbol = row.get("symbol", "")
                        if symbol not in groups:
                            raise ArtifactError("回测曲线交易对与统计清单不一致")
                        groups[symbol].append({key: value for key, value in row.items() if key != "symbol"})
                    result[kind] = groups.pop("")
                    for symbol, curve in groups.items():
                        result["symbol_results"][symbol][kind] = curve
                else:
                    result[kind] = rows
    elif preview:
        result = result_preview(result)
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
