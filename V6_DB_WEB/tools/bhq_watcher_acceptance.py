from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.database_lock import (  # noqa: E402
    DatabaseWriteLockError,
    database_write_lock,
)


CORE_TABLES = ("orders", "order_versions", "order_items", "stores", "products")
PROTECTED_TABLES = ("review_queue", "screenshots")
TRACKED_TABLES = CORE_TABLES + PROTECTED_TABLES + (
    "audit_logs",
    "bhq_import_runs",
    "backup_records",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def table_digest(connection: sqlite3.Connection, name: str) -> str:
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({name})")]
    if not columns:
        return ""
    quoted = ",".join(f'"{column}"' for column in columns)
    order = '"id"' if "id" in columns else quoted
    digest = hashlib.sha256()
    for row in connection.execute(f'SELECT {quoted} FROM "{name}" ORDER BY {order}'):
        line = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"))
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest().upper()


def snapshot(database: Path) -> dict[str, Any]:
    database = database.resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in TRACKED_TABLES
            if table_exists(connection, name)
        }
        fingerprints = {
            name: table_digest(connection, name)
            for name in CORE_TABLES + PROTECTED_TABLES
            if table_exists(connection, name)
        }
        bhq = dict(
            zip(
                ("orders", "versions", "items", "discounted_total_amount"),
                connection.execute(
                    """
                    SELECT
                      COUNT(DISTINCT o.id),
                      COUNT(DISTINCT v.id),
                      COUNT(i.id),
                      COALESCE(SUM(DISTINCT CASE
                        WHEN v.id = o.current_version_id
                        THEN (v.id * 1000000000000) + v.discounted_total_amount
                      END), 0)
                      - COALESCE(SUM(DISTINCT CASE
                        WHEN v.id = o.current_version_id
                        THEN v.id * 1000000000000
                      END), 0)
                    FROM orders o
                    JOIN order_versions v ON v.id=o.current_version_id
                    LEFT JOIN order_items i ON i.order_version_id=v.id
                    WHERE v.source_system='BHQ'
                    """
                ).fetchone(),
            )
        )
        erp = dict(
            zip(
                ("orders", "items"),
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT o.id), COUNT(i.id)
                    FROM orders o
                    JOIN order_versions v ON v.id=o.current_version_id
                    LEFT JOIN order_items i ON i.order_version_id=v.id
                    WHERE COALESCE(v.source_system,'')<>'BHQ'
                    """
                ).fetchone(),
            )
        )
        sr = connection.execute(
            "SELECT id,store_code,store_name,active,created_at,updated_at FROM stores WHERE store_code='SR'"
        ).fetchone()
        migration = connection.execute(
            "SELECT version,name,checksum,applied_at FROM database_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    return {
        "captured_at": utc_now(),
        "database": str(database),
        "database_sha256": file_sha256(database),
        "database_size": database.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "migration": list(migration) if migration else None,
        "counts": counts,
        "fingerprints": fingerprints,
        "bhq": bhq,
        "erp": erp,
        "sr_store": list(sr) if sr else None,
    }


def compare(before: dict[str, Any], after: dict[str, Any], strict: bool) -> dict[str, Any]:
    deltas = {
        name: after["counts"].get(name, 0) - before["counts"].get(name, 0)
        for name in sorted(set(before["counts"]) | set(after["counts"]))
    }
    stable_names = CORE_TABLES + PROTECTED_TABLES
    fingerprints_unchanged = {
        name: before["fingerprints"].get(name) == after["fingerprints"].get(name)
        for name in stable_names
    }
    errors = []
    if after.get("integrity_check") != "ok":
        errors.append("integrity_check is not ok")
    if after.get("foreign_key_errors") != 0:
        errors.append("foreign_key_check has errors")
    for name, unchanged in fingerprints_unchanged.items():
        if not unchanged:
            errors.append(f"protected fingerprint changed: {name}")
    if before.get("bhq") != after.get("bhq"):
        errors.append("BHQ core aggregate changed")
    if before.get("erp") != after.get("erp"):
        errors.append("ERP aggregate changed")
    if before.get("sr_store") != after.get("sr_store"):
        errors.append("SR store changed")
    if strict:
        changed = [name for name, delta in deltas.items() if delta]
        if changed:
            errors.append(f"strict repeat changed table counts: {changed}")
        if before.get("database_sha256") != after.get("database_sha256"):
            errors.append("strict repeat changed orders.db SHA-256")
    return {
        "passed": not errors,
        "strict": strict,
        "count_deltas": deltas,
        "fingerprints_unchanged": fingerprints_unchanged,
        "database_sha256_unchanged": before.get("database_sha256")
        == after.get("database_sha256"),
        "errors": errors,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="BHQ watcher target acceptance helper")
    actions = parser.add_subparsers(dest="action", required=True)
    snap = actions.add_parser("snapshot")
    snap.add_argument("--db", type=Path, required=True)
    snap.add_argument("--output", type=Path, required=True)
    comp = actions.add_parser("compare")
    comp.add_argument("--before", type=Path, required=True)
    comp.add_argument("--after", type=Path, required=True)
    comp.add_argument("--output", type=Path, required=True)
    comp.add_argument("--strict", action="store_true")
    hold = actions.add_parser("hold-lock")
    hold.add_argument("--db", type=Path, required=True)
    hold.add_argument("--seconds", type=float, default=8)
    hold.add_argument("--ready", type=Path, required=True)
    wait = actions.add_parser("wait-lock")
    wait.add_argument("--db", type=Path, required=True)
    wait.add_argument("--timeout", type=float, default=30)
    wait.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.action == "snapshot":
        with database_write_lock(args.db, owner="BHQ acceptance snapshot"):
            payload = snapshot(args.db)
        write_json(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action == "compare":
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        payload = compare(before, after, args.strict)
        write_json(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["passed"] else 1
    if args.action == "hold-lock":
        with database_write_lock(args.db, owner="simulated ERP writer"):
            write_json(args.ready, {"status": "locked", "at": utc_now()})
            print("LOCKED", flush=True)
            time.sleep(max(0, args.seconds))
        print("RELEASED", flush=True)
        return 0

    deadline = time.monotonic() + max(1, args.timeout)
    deferred = False
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with database_write_lock(args.db, owner="BHQ acceptance wait probe"):
                payload = {
                    "passed": deferred,
                    "deferred_observed": deferred,
                    "eventually_acquired": True,
                    "attempts": attempts,
                    "finished_at": utc_now(),
                }
                write_json(args.output, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0 if deferred else 1
        except DatabaseWriteLockError:
            deferred = True
            time.sleep(0.25)
    payload = {
        "passed": False,
        "deferred_observed": deferred,
        "eventually_acquired": False,
        "attempts": attempts,
        "finished_at": utc_now(),
    }
    write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
