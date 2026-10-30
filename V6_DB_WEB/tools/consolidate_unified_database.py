from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.canonical import content_hash, item_hash  # noqa: E402
from v6_db_web.database_lock import database_write_lock  # noqa: E402
from v6_db_web.db import checkpoint, connect  # noqa: E402
from v6_db_web.migrations import apply_migrations  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        scalar = lambda sql: int(connection.execute(sql).fetchone()[0])
        return {
            "quick_check": quick,
            "foreign_key_errors": foreign_keys,
            "orders": scalar("SELECT COUNT(*) FROM orders"),
            "versions": scalar("SELECT COUNT(*) FROM order_versions"),
            "items": scalar("SELECT COUNT(*) FROM order_items"),
            "pending": scalar("SELECT COUNT(*) FROM review_queue WHERE status='pending'"),
            "formal_orders": scalar(
                """
                SELECT COUNT(*) FROM official_orders oo
                JOIN orders o ON o.id=oo.id
                JOIN stores s ON s.id=o.store_id
                WHERE s.store_code='SR'
                """
            ),
            "duplicate_orders": scalar(
                "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"
            ),
            "duplicate_versions": scalar(
                """
                SELECT COUNT(*) FROM (
                  SELECT order_id,content_hash FROM order_versions
                  GROUP BY order_id,content_hash HAVING COUNT(*)>1
                )
                """
            ),
            "manual_order": scalar(
                "SELECT COUNT(*) FROM orders WHERE order_no='GOU26080250'"
            ),
        }
    finally:
        connection.close()


def verified_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    check = validate_database(temporary)
    if check["quick_check"] != "ok" or check["foreign_key_errors"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Backup validation failed: {check}")
    os.replace(temporary, destination)
    return {
        "path": str(destination),
        "sha256": file_hash(destination),
        "bytes": destination.stat().st_size,
        "created_at": now(),
        "validation": check,
    }


def version_order(connection: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    version = connection.execute(
        """
        SELECT o.order_no,v.order_date,v.category,v.department_code,v.is_voided
        FROM order_versions v JOIN orders o ON o.id=v.order_id WHERE v.id=?
        """,
        (version_id,),
    ).fetchone()
    details = [
        {
            "gift_code": row[0],
            "list_price": row[1],
            "quantity": row[2],
            "total_amount": row[3],
            "discount_amount": row[4],
        }
        for row in connection.execute(
            """
            SELECT gift_code,list_price,quantity,total_amount,discount_amount
            FROM order_items WHERE order_version_id=? ORDER BY line_no,id
            """,
            (version_id,),
        )
    ]
    return {
        "order_no": version["order_no"],
        "order_date": version["order_date"],
        "category": version["category"],
        "department_code": version["department_code"],
        "is_voided": bool(version["is_voided"]),
        "details": details,
    }


def normalize_manual_hashes(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT id,order_id FROM order_versions WHERE source_system='website_manual_review'"
    ).fetchall()
    changed = 0
    for row in rows:
        version_id = int(row["id"])
        order_id = int(row["order_id"])
        order = version_order(connection, version_id)
        digest = content_hash(order)
        conflict = connection.execute(
            "SELECT id FROM order_versions WHERE order_id=? AND content_hash=? AND id<>?",
            (order_id, digest, version_id),
        ).fetchone()
        if conflict:
            raise RuntimeError(
                f"Manual version {version_id} conflicts with existing version {conflict['id']}"
            )
        connection.execute(
            "UPDATE order_versions SET content_hash=? WHERE id=?", (digest, version_id)
        )
        for item in connection.execute(
            """
            SELECT id,gift_code,list_price,quantity,total_amount,discount_amount
            FROM order_items WHERE order_version_id=?
            """,
            (version_id,),
        ):
            normalized = {
                "gift_code": item["gift_code"],
                "list_price": item["list_price"],
                "quantity": item["quantity"],
                "total_amount": item["total_amount"],
                "discount_amount": item["discount_amount"],
            }
            connection.execute(
                "UPDATE order_items SET item_hash=? WHERE id=?",
                (item_hash(normalized), int(item["id"])),
            )
        changed += 1
    connection.execute(
        """
        UPDATE orders SET current_content_hash=(
          SELECT content_hash FROM order_versions WHERE id=orders.current_version_id
        ) WHERE current_version_id IS NOT NULL
        """
    )
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=ROOT / "web_v1" / "data" / "orders_web.db"
    )
    parser.add_argument("--target", type=Path, default=ROOT / "data" / "orders.db")
    parser.add_argument(
        "--backup-root", type=Path, default=ROOT / "backups" / "database_consolidation"
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "reports" / "UNIFIED_DATABASE_MERGE.json"
    )
    args = parser.parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    if source == target or source.name.lower() != "orders_web.db":
        raise SystemExit("Source must be the retired website orders_web.db")
    if target != (ROOT / "data" / "orders.db").resolve():
        raise SystemExit("Target must be the unique formal data/orders.db")
    if not source.is_file() or not target.is_file():
        raise SystemExit("Both source and current target database must exist")

    source_before = validate_database(source)
    if (
        source_before["quick_check"] != "ok"
        or source_before["foreign_key_errors"]
        or source_before["manual_order"] != 1
    ):
        raise SystemExit(f"Website source validation failed: {source_before}")

    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_dir = args.backup_root.resolve() / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    temporary = target.with_suffix(".db.unified.tmp")
    temporary.unlink(missing_ok=True)

    with database_write_lock(target, "unified database consolidation"):
        with database_write_lock(source, "unified database consolidation source"):
            old_target_backup = verified_snapshot(
                target, backup_dir / "legacy_formal_orders.db"
            )
            source_backup = verified_snapshot(
                source, backup_dir / "website_source_orders_web.db"
            )
            source_connection = sqlite3.connect(source)
            target_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()

            connection = connect(temporary)
            try:
                applied = apply_migrations(connection, ROOT / "migrations")
                connection.execute("BEGIN IMMEDIATE")
                manual_hashes = normalize_manual_hashes(connection)
                timestamp = now()
                connection.execute(
                    """
                    INSERT INTO system_settings(
                      setting_key,setting_value,value_type,description,updated_at
                    ) VALUES('unified_formal_database','data/orders.db','text',
                      'Single writable SQLite used by ERP scans and the local website',?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                      setting_value='data/orders.db',updated_at=excluded.updated_at
                    """,
                    (timestamp,),
                )
                connection.execute(
                    """
                    INSERT INTO audit_logs(
                      event_type,entity_type,entity_id,actor,details_json,created_at
                    ) VALUES('unified_database_consolidated','database','orders.db',
                      'consolidate_unified_database',?,?)
                    """,
                    (
                        json.dumps(
                            {
                                "source": "retired orders_web.db",
                                "target": "data/orders.db",
                                "source_sha256": source_backup["sha256"],
                                "previous_target_sha256": old_target_backup["sha256"],
                                "manual_hashes_normalized": manual_hashes,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    ),
                )
                connection.commit()
                checkpoint(connection)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

            candidate = validate_database(temporary)
            expected = {
                "orders": source_before["orders"],
                "versions": source_before["versions"],
                "items": source_before["items"],
                "pending": source_before["pending"],
                "formal_orders": source_before["formal_orders"],
                "manual_order": 1,
            }
            for key, value in expected.items():
                if candidate[key] != value:
                    raise RuntimeError(
                        f"Consolidated database count mismatch for {key}: {candidate[key]} != {value}"
                    )
            if (
                candidate["quick_check"] != "ok"
                or candidate["foreign_key_errors"]
                or candidate["duplicate_orders"]
                or candidate["duplicate_versions"]
            ):
                raise RuntimeError(f"Consolidated database validation failed: {candidate}")
            os.replace(temporary, target)

    final_validation = validate_database(target)
    result = {
        "status": "passed",
        "consolidated_at": now(),
        "source": str(source),
        "target": str(target),
        "target_sha256": file_hash(target),
        "backup_directory": str(backup_dir),
        "source_backup": source_backup,
        "previous_target_backup": old_target_backup,
        "migrations": applied,
        "manual_hashes_normalized": manual_hashes,
        "before": source_before,
        "after": final_validation,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
