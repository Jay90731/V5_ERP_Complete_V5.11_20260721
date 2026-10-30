from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "orders.db"
OUTPUT = ROOT / "reports" / "BHQ_FORMAL_BASELINE_BEFORE_IMPORT.json"


def rows_hash(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    digest = hashlib.sha256()
    cursor = connection.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
    for row in cursor:
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest().upper()


def main() -> int:
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "orders", "order_versions", "order_items", "stores", "products",
                "review_queue", "screenshots", "audit_logs", "scan_jobs", "scan_items",
            )
        }
        max_ids = {
            table: connection.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
            for table in (
                "orders", "order_versions", "order_items", "stores", "products",
                "review_queue", "screenshots", "audit_logs", "scan_jobs", "scan_items",
            )
        }
        fingerprints = {
            "orders": rows_hash(connection, "SELECT * FROM orders ORDER BY id"),
            "order_versions": rows_hash(connection, "SELECT * FROM order_versions ORDER BY id"),
            "order_items": rows_hash(connection, "SELECT * FROM order_items ORDER BY id"),
            "products_except_705429": rows_hash(
                connection, "SELECT * FROM products WHERE gift_code<>'705429' ORDER BY id"
            ),
            "review_queue": rows_hash(connection, "SELECT * FROM review_queue ORDER BY id"),
            "screenshots": rows_hash(connection, "SELECT * FROM screenshots ORDER BY id"),
            "scan_jobs": rows_hash(connection, "SELECT * FROM scan_jobs ORDER BY id"),
            "scan_items": rows_hash(connection, "SELECT * FROM scan_items ORDER BY id"),
            "existing_audit_logs": rows_hash(connection, "SELECT * FROM audit_logs ORDER BY id"),
            "sr_store": rows_hash(connection, "SELECT * FROM stores WHERE store_code='SR' ORDER BY id"),
        }
        product_705429 = connection.execute(
            "SELECT * FROM products WHERE gift_code='705429'"
        ).fetchone()
        payload = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "database": str(DATABASE),
            "latest_migration": connection.execute(
                "SELECT MAX(version) FROM database_migrations"
            ).fetchone()[0],
            "migration_007": dict(connection.execute(
                "SELECT version,name,checksum,applied_at FROM database_migrations WHERE version=7"
            ).fetchone()),
            "counts": counts,
            "max_ids": max_ids,
            "fingerprints": fingerprints,
            "product_705429_before": dict(product_705429) if product_705429 else None,
            "bhq_versions": connection.execute(
                "SELECT COUNT(*) FROM order_versions WHERE source_system='BHQ'"
            ).fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_errors": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        connection.close()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
