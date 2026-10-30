from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .canonical import normalize_order
from .importer import pending_source_key, read_json, resolve_v5_state
from .store_rules import is_official_order, store_policy_metrics


def scalar(connection: sqlite3.Connection, sql: str, parameters=()) -> int:
    return int(connection.execute(sql, parameters).fetchone()[0])


def database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "orders",
        "order_versions",
        "order_items",
        "scan_jobs",
        "scan_items",
        "review_queue",
        "screenshots",
        "products",
        "stores",
        "store_department_mappings",
        "audit_logs",
        "system_settings",
        "database_migrations",
        "backup_records",
    ]
    return {
        table: scalar(connection, f"SELECT COUNT(*) FROM {table}") for table in tables
    }


def current_db_order(connection: sqlite3.Connection, order_no: str):
    return connection.execute(
        """
        SELECT o.id, o.order_no, o.order_date, o.category, o.department_code,
               o.store_id, o.is_voided, o.current_version_id
        FROM orders o
        WHERE o.order_no = ?
        """,
        (order_no,),
    ).fetchone()


def current_db_items(
    connection: sqlite3.Connection, version_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT gift_code, list_price, quantity, total_amount, discount_amount
        FROM order_items
        WHERE order_version_id = ?
        ORDER BY line_no
        """,
        (version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_v5_import(
    connection: sqlite3.Connection,
    source: str | Path,
) -> dict[str, Any]:
    state_dir = resolve_v5_state(source)
    history = read_json(state_dir / "order_history.json")
    current_path = state_dir / "current_orders.json"
    current = read_json(current_path) if current_path.exists() else {"orders": []}
    source_orders = history["orders"]

    source_detail_rows = 0
    source_multi_item_orders = 0
    field_mismatches: list[dict[str, Any]] = []
    missing_orders: list[str] = []
    item_mismatches: list[dict[str, Any]] = []
    source_departments: Counter[str] = Counter()
    source_official_orders = 0

    for order_no, entry in source_orders.items():
        source_order = normalize_order(entry["current_version"])
        source_detail_rows += len(source_order["details"])
        source_multi_item_orders += int(len(source_order["details"]) > 1)
        source_departments[source_order["department_code"]] += 1
        source_official_orders += int(is_official_order(source_order))
        db_order = current_db_order(connection, source_order["order_no"])
        if db_order is None:
            missing_orders.append(order_no)
            continue
        expected_fields = {
            "order_no": source_order["order_no"],
            "order_date": source_order["order_date"],
            "category": source_order["category"],
            "department_code": source_order["department_code"],
            "is_voided": int(source_order["is_voided"]),
        }
        actual_fields = {key: db_order[key] for key in expected_fields}
        if actual_fields != expected_fields:
            field_mismatches.append(
                {
                    "order_no": order_no,
                    "expected": expected_fields,
                    "actual": actual_fields,
                }
            )
        db_items = current_db_items(connection, int(db_order["current_version_id"]))
        if db_items != source_order["details"]:
            item_mismatches.append(
                {
                    "order_no": order_no,
                    "expected": source_order["details"],
                    "actual": db_items,
                }
            )

    expected_pending = [
        record
        for record in current.get("orders") or []
        if record.get("status") == "待確認"
    ]
    missing_pending = []
    for record in expected_pending:
        key, _ = pending_source_key(record)
        if not connection.execute(
            "SELECT 1 FROM review_queue WHERE source_key = ?", (key,)
        ).fetchone():
            missing_pending.append(key)

    db_order_numbers = {
        row[0] for row in connection.execute("SELECT order_no FROM orders")
    }
    source_order_numbers = {
        normalize_order(entry["current_version"])["order_no"]
        for entry in source_orders.values()
    }
    unexpected_orders = sorted(db_order_numbers - source_order_numbers)
    duplicate_order_numbers = connection.execute(
        """
        SELECT order_no, COUNT(*) AS count
        FROM orders
        GROUP BY order_no
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    duplicate_versions = connection.execute(
        """
        SELECT order_id, content_hash, COUNT(*) AS count
        FROM order_versions
        GROUP BY order_id, content_hash
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    db_multi_item_orders = scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM (
            SELECT o.id
            FROM orders o
            JOIN order_items i ON i.order_version_id = o.current_version_id
            GROUP BY o.id
            HAVING COUNT(i.id) > 1
        )
        """,
    )
    current_detail_rows = scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM order_items i
        JOIN orders o ON o.current_version_id = i.order_version_id
        """,
    )
    db_departments = Counter(
        {
            row["department_code"]: int(row["count"])
            for row in connection.execute(
                """
                SELECT department_code, COUNT(*) AS count
                FROM orders
                GROUP BY department_code
                """
            )
        }
    )
    foreign_key_errors = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    store_metrics = store_policy_metrics(connection)

    checks = {
        "order_count_matches": len(source_orders) == len(db_order_numbers),
        "all_source_orders_present": not missing_orders,
        "no_unexpected_orders": not unexpected_orders,
        "no_duplicate_order_numbers": not duplicate_order_numbers,
        "current_detail_count_matches": source_detail_rows == current_detail_rows,
        "multi_item_orders_preserved": (
            source_multi_item_orders == db_multi_item_orders
        ),
        "amounts_and_quantities_unchanged": not item_mismatches,
        "header_fields_unchanged": not field_mismatches,
        "department_codes_preserved": source_departments == db_departments,
        "one_sr_store_only": store_metrics["sr_store_count"] == 1
        and store_metrics["store_count"] == 1,
        "all_official_orders_use_sr": store_metrics["official_orders_with_sr"]
        == source_official_orders,
        "non_official_orders_have_no_store": store_metrics[
            "non_official_with_store"
        ]
        == 0,
        "pending_records_preserved": not missing_pending,
        "no_duplicate_versions": not duplicate_versions,
        "foreign_keys_valid": not foreign_key_errors,
        "database_quick_check_ok": quick_check == "ok",
    }
    return {
        "source": {
            "state_dir": str(state_dir),
            "orders": len(source_orders),
            "current_detail_rows": source_detail_rows,
            "multi_item_orders": source_multi_item_orders,
            "pending_records": len(expected_pending),
            "departments": dict(sorted(source_departments.items())),
        },
        "database": {
            "counts": database_counts(connection),
            "current_detail_rows": current_detail_rows,
            "multi_item_orders": db_multi_item_orders,
            "departments": dict(sorted(db_departments.items())),
            **store_metrics,
            "quick_check": quick_check,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "differences": {
            "missing_orders": missing_orders,
            "unexpected_orders": unexpected_orders,
            "duplicate_order_numbers": [dict(row) for row in duplicate_order_numbers],
            "duplicate_versions": [dict(row) for row in duplicate_versions],
            "field_mismatches": field_mismatches,
            "item_mismatches": item_mismatches,
            "missing_pending": missing_pending,
            "foreign_key_errors": foreign_key_errors,
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    source = report["source"]
    database = report["database"]
    checks = report["checks"]
    lines = [
        "# V5.10 六個月資料匯入驗證報告",
        "",
        f"- 驗收結果：{'通過' if report['passed'] else '失敗'}",
        f"- V5.10 訂單數：{source['orders']}",
        f"- SQLite 訂單數：{database['counts']['orders']}",
        f"- 目前版本明細列：{database['current_detail_rows']}",
        f"- 多品項訂單：{database['multi_item_orders']}",
        f"- 待確認：{source['pending_records']}",
        f"- store_id 非 NULL：{database['non_null_store_ids']}",
        f"- SQLite quick_check：{database['quick_check']}",
        "",
        "## 驗收項目",
        "",
    ]
    for name, passed in checks.items():
        lines.append(f"- [{'x' if passed else ' '}] {name}")
    lines.extend(
        [
            "",
            "## 資料表筆數",
            "",
            "| 資料表 | 筆數 |",
            "|---|---:|",
        ]
    )
    for table, count in database["counts"].items():
        lines.append(f"| {table} | {count} |")
    lines.append("")
    return "\n".join(lines)


def write_report(path: str | Path, report: dict[str, Any]) -> tuple[Path, Path]:
    json_path = Path(path).resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return json_path, markdown_path
