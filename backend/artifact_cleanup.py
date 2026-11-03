"""Durable, reference-aware artifact deletion. Audit events form the cleanup outbox."""
import os
import re
from . import db, storage


def enqueue(con, actor_id, manifests):
    identity = db.uid()
    db.audit(con, actor_id, "artifact.cleanup.pending", "artifact_cleanup", identity,
             {"manifests": [m for m in manifests if isinstance(m, dict) and m.get("storage") == "parquet"]})
    return identity


def _references(con):
    paths = set()
    for row in con.execute("SELECT candles FROM datasets").fetchall():
        manifest = db.parse(row["candles"], {})
        if isinstance(manifest, dict) and manifest.get("path"):
            paths.add(manifest["path"])
    for row in con.execute("SELECT result FROM experiments WHERE result IS NOT NULL").fetchall():
        for manifest in db.parse(row["result"], {}).get("artifacts", {}).values():
            paths.add(manifest["path"])
    return paths


def _remove(manifest):
    name = manifest.get("path", "")
    if not re.fullmatch(r"(?:candles|curve|validation_curve|orders|decisions)/[a-f0-9]{64}\.parquet", name):
        raise storage.ArtifactError("文件清理路径无效")
    root = storage.artifact_root()
    path = root / name
    if path.is_symlink() or path.parent.is_symlink() or root not in path.resolve().parents:
        raise storage.ArtifactError("不能清理符号链接或存储目录外的文件")
    if not path.exists():
        return 0
    storage._resolve(manifest)  # Refuse to delete a replaced or corrupted object.
    size = path.stat().st_size
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return size


def drain(identity=None):
    """Run after the delete transaction commits; retry unfinished work on startup.

    Rechecks current references under the same lock used by every publisher.
    Missing files are successful retries. Failures remain durably pending.
    """
    reports = {}
    with storage.lifecycle_lock():
        with db.connect() as con:
            pending = con.execute("""SELECT p.resource_id,p.payload FROM audit_records p
                WHERE p.action='artifact.cleanup.pending' AND NOT EXISTS
                (SELECT 1 FROM audit_records c WHERE c.action='artifact.cleanup.complete'
                 AND c.resource_id=p.resource_id)""" +
                (" AND p.resource_id=?" if identity else ""), (identity,) if identity else ()).fetchall()
            referenced = _references(con)
        for row in pending:
            report = {"freed_bytes": 0, "deleted_files": 0, "shared_files": 0, "cleanup_pending": False}
            seen = set()
            for manifest in db.parse(row["payload"], {}).get("manifests", []):
                name = manifest.get("path", "")
                if name in seen:
                    continue
                seen.add(name)
                if name in referenced:
                    report["shared_files"] += 1
                    continue
                try:
                    size = _remove(manifest)
                    report["freed_bytes"] += size
                    report["deleted_files"] += int(size > 0)
                except (OSError, storage.ArtifactError):
                    report["cleanup_pending"] = True
            with db.connect() as con:
                db.audit(con, None, "artifact.cleanup.retry" if report["cleanup_pending"] else "artifact.cleanup.complete",
                         "artifact_cleanup", row["resource_id"], report)
            reports[row["resource_id"]] = report
    return reports.get(identity, {}) if identity else reports


def response(identity, cleanup_id, label):
    report = drain(cleanup_id)
    size = report.get("freed_bytes", 0)
    released = f"{size / 1048576:.2f} MiB" if size >= 1048576 else f"{size:,} 字节"
    if report.get("cleanup_pending"):
        message = f"{label}已删除，部分文件清理待重试，请查看审计记录"
    elif report.get("shared_files"):
        message = f"{label}已删除，释放 {released}；共享文件仍被其他记录引用"
    else:
        message = f"{label}及对应文件已删除，释放 {released}"
    return {"id": identity, "deleted": True, **report, "message": message}
