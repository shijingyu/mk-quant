"""One-time offline import of a legacy backup into an empty MySQL database.

The application never imports this module or opens a file database. Stop all
writers and take a consistent legacy backup before invoking this utility.
Environment selects the MySQL target; credentials are never CLI arguments.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import db


LEGACY_TABLES = (
    "users", "strategies", "sessions", "accounts", "instances", "commands", "datasets",
    "experiments", "presets", "logs", "ledger", "runtime_events", "services", "market_cache",
    "binance_order_intents",
)


def read_source(path):
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("Legacy backup integrity check failed")
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'") if not row[0].startswith("sqlite_")}
    unknown = tables - set(LEGACY_TABLES)
    if unknown:
        raise RuntimeError("Unmapped legacy tables; migration must be reviewed: " + ", ".join(sorted(unknown)))
    result = {name: [dict(row) for row in source.execute(f'SELECT * FROM "{name}"')] for name in LEGACY_TABLES if name in tables}
    source.close()
    return result


def verify_rows(source, *, hydrated):
    from backend import storage
    with db.connect() as con:
        for table, rows in source.items():
            current = con.execute(f"SELECT * FROM `{table}`").fetchall()
            if len(current) != len(rows):
                raise RuntimeError(f"Row count mismatch: {table}")
            keys = ("market", "symbol", "interval") if table == "market_cache" else (("account", "client_id") if table == "binance_order_intents" else (("account_id",) if table == "instances" else (("name",) if table == "services" else (("token_hash",) if table == "sessions" else ("id",)))))
            index = {tuple(row[k] for k in keys): row for row in current}
            for old in rows:
                new = index.get(tuple(old[k] for k in keys))
                if new is None:
                    raise RuntimeError(f"Missing imported record: {table}")
                for column, value in old.items():
                    if hydrated and table == "datasets" and column == "candles":
                        equal = storage.read_candles(db.parse(new[column])) == db.parse(value)
                    elif hydrated and table == "experiments" and column == "result" and value:
                        actual = storage.load_result(db.parse(new[column]), detail=True)
                        expected = db.parse(value)
                        equal = all(actual.get(k) == v for k, v in expected.items())
                    else:
                        equal = new[column] == value
                    if not equal:
                        # Never include private record contents in errors.
                        raise RuntimeError(f"Value mismatch: {table}.{column}")


def migrate(source_path, apply=False):
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError("A consistent legacy backup file is required")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = read_source(source_path)
    counts = {table: len(rows) for table, rows in source.items()}
    report = {"source_sha256": source_hash, "tables": counts, "applied": False}
    if not apply:
        return report
    from backend import storage
    db.init_schema()
    marker = "legacy-" + source_hash[:32]
    with db.connect() as con:
        imported = con.execute("SELECT version FROM schema_migrations WHERE version=?", (marker + "-rows",)).fetchone()
        complete = con.execute("SELECT version FROM schema_migrations WHERE version=?", (marker + "-complete",)).fetchone()
        if not imported:
            populated = [table for table in db.TABLES if table != "schema_migrations" and con.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0]]
            if populated:
                raise RuntimeError("Refusing non-empty target: " + ", ".join(populated))
            for table, rows in source.items():
                for row in rows:
                    columns = ",".join("`" + name + "`" for name in row)
                    placeholders = ",".join("?" for _ in row)
                    con.execute(f"INSERT INTO `{table}`({columns}) VALUES({placeholders})", tuple(row.values()))
            con.execute("INSERT INTO schema_migrations VALUES(?,?)", (marker + "-rows", db.now()))
    if not complete:
        # All original row values are checked before replacing bulk JSON with
        # checksummed immutable Parquet references. Backfill itself is resumable.
        if not imported:
            verify_rows(source, hydrated=False)
        storage.backfill_artifacts()
        with db.connect() as con:
            for row in con.execute("SELECT * FROM strategies").fetchall():
                db.pin_strategy_version(con, row["id"], db.parse(row["manifest"]), row["source"], row["source_hash"])
            for row in con.execute("SELECT account_id,payload,created_at FROM ledger WHERE kind='fill'").fetchall():
                db.persist_fill(con, row["account_id"], db.parse(row["payload"]), row["created_at"])
            for account in con.execute("SELECT id,metrics,created_at FROM accounts").fetchall():
                for order in db.parse(account["metrics"], {}).get("orders", []):
                    db.persist_fill(con, account["id"], order, account["created_at"])
            db.audit(con, None, "storage.migrated", "database", "legacy-import", {"source_sha256": source_hash, "counts": counts})
            con.execute("INSERT INTO schema_migrations VALUES(?,?)", (marker + "-complete", db.now()))
    verify_rows(source, hydrated=True)
    with db.connect() as con:
        engines = con.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()").fetchall()
        if any(row["ENGINE"] != "InnoDB" for row in engines):
            raise RuntimeError("All business tables must use InnoDB")
    report.update(applied=True, already_imported=bool(complete), values_verified=True, innodb_verified=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Import into the configured empty MySQL target")
    parser.add_argument("--report", type=Path, help="Optional private verification report")
    args = parser.parse_args()
    report = migrate(args.source, args.apply)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n")
        args.report.chmod(0o600)
    print(encoded)


if __name__ == "__main__":
    main()
