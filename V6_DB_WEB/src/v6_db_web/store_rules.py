from __future__ import annotations

import sqlite3
from typing import Any


SR_STORE_CODE = "SR"
TARGET_DEPARTMENT = "4001"


def ensure_sr_store(connection: sqlite3.Connection, timestamp: str) -> int:
    connection.execute(
        """
        INSERT INTO stores(
            store_code, store_name, active, created_at, updated_at, status
        ) VALUES ('SR', 'SR', 1, ?, ?, 'active')
        ON CONFLICT(store_code) DO UPDATE SET
            store_name = 'SR', active = 1, status = 'active', updated_at = ?
        """,
        (timestamp, timestamp, timestamp),
    )
    row = connection.execute(
        "SELECT id FROM stores WHERE store_code = 'SR'"
    ).fetchone()
    if row is None:
        raise RuntimeError("SR store could not be created")
    return int(row["id"])


def is_official_order(order: dict[str, Any]) -> bool:
    return (
        str(order.get("department_code") or "").strip() == TARGET_DEPARTMENT
        and not bool(order.get("is_voided"))
        and bool(order.get("details"))
    )


def store_id_for_order(
    connection: sqlite3.Connection,
    order: dict[str, Any],
    timestamp: str,
) -> int | None:
    if not is_official_order(order):
        return None
    return ensure_sr_store(connection, timestamp)


def store_policy_metrics(connection: sqlite3.Connection) -> dict[str, int]:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(stores)")
    }
    status_clause = "AND s.status = 'active'" if "status" in columns else ""
    return {
        "official_orders": connection.execute(
            "SELECT COUNT(*) FROM official_orders"
        ).fetchone()[0],
        "official_orders_with_sr": connection.execute(
            f"""
            SELECT COUNT(*) FROM official_orders o
            JOIN stores s ON s.id = o.store_id
            WHERE s.store_code = 'SR' AND s.store_name = 'SR'
              AND s.active = 1 {status_clause}
            """
        ).fetchone()[0],
        "non_official_with_store": connection.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE store_id IS NOT NULL
              AND id NOT IN (SELECT id FROM official_orders)
            """
        ).fetchone()[0],
        "non_null_store_ids": connection.execute(
            "SELECT COUNT(*) FROM orders WHERE store_id IS NOT NULL"
        ).fetchone()[0],
        "sr_store_count": connection.execute(
            "SELECT COUNT(*) FROM stores WHERE store_code='SR'"
        ).fetchone()[0],
        "store_count": connection.execute(
            "SELECT COUNT(*) FROM stores"
        ).fetchone()[0],
    }
