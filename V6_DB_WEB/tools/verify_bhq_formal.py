from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_excel import parse_bhq_xls


DATABASE = ROOT / "data" / "orders.db"
BASELINE_PATH = ROOT / "reports" / "BHQ_FORMAL_BASELINE_BEFORE_IMPORT.json"
SOURCE = Path(r"C:\Users\USER\Downloads\202607ALL 訂單明細表.xls")


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("first", "final"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    parsed = parse_bhq_xls(SOURCE)
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = ("orders", "order_versions", "order_items", "stores", "products")
        current_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        count_increases = {
            table: current_counts[table] - baseline["counts"][table] for table in tables
        }
        expected_increases = {
            "orders": 257, "order_versions": 257, "order_items": 551,
            "stores": 18, "products": 1,
        }
        max_ids = baseline["max_ids"]
        current_fingerprints = {
            "orders": rows_hash(
                connection, "SELECT * FROM orders WHERE id<=? ORDER BY id", (max_ids["orders"],)
            ),
            "order_versions": rows_hash(
                connection, "SELECT * FROM order_versions WHERE id<=? ORDER BY id", (max_ids["order_versions"],)
            ),
            "order_items": rows_hash(
                connection, "SELECT * FROM order_items WHERE id<=? ORDER BY id", (max_ids["order_items"],)
            ),
            "products_except_705429": rows_hash(
                connection,
                "SELECT * FROM products WHERE id<=? AND gift_code<>'705429' ORDER BY id",
                (max_ids["products"],),
            ),
            "review_queue": rows_hash(connection, "SELECT * FROM review_queue ORDER BY id"),
            "screenshots": rows_hash(connection, "SELECT * FROM screenshots ORDER BY id"),
            "scan_jobs": rows_hash(connection, "SELECT * FROM scan_jobs ORDER BY id"),
            "scan_items": rows_hash(connection, "SELECT * FROM scan_items ORDER BY id"),
            "existing_audit_logs": rows_hash(
                connection, "SELECT * FROM audit_logs WHERE id<=? ORDER BY id", (max_ids["audit_logs"],)
            ),
            "sr_store": rows_hash(connection, "SELECT * FROM stores WHERE store_code='SR' ORDER BY id"),
        }
        fingerprint_checks = {
            key: value == baseline["fingerprints"][key]
            for key, value in current_fingerprints.items()
        }
        bhq_orders = connection.execute(
            "SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system='BHQ'"
        ).fetchone()[0]
        bhq_versions = connection.execute(
            "SELECT COUNT(*) FROM order_versions WHERE source_system='BHQ'"
        ).fetchone()[0]
        bhq_items = connection.execute(
            "SELECT COUNT(*) FROM order_items i JOIN order_versions v ON v.id=i.order_version_id WHERE v.source_system='BHQ'"
        ).fetchone()[0]
        bhq_stores = connection.execute("SELECT COUNT(*) FROM bhq_store_aliases").fetchone()[0]
        zero_items = connection.execute(
            "SELECT COUNT(*) FROM order_items i JOIN order_versions v ON v.id=i.order_version_id WHERE v.source_system='BHQ' AND i.quantity=0"
        ).fetchone()[0]
        duplicate_code_rows = connection.execute(
            """
            SELECT COALESCE(SUM(item_count-distinct_count),0) FROM (
              SELECT COUNT(*) item_count,COUNT(DISTINCT i.gift_code) distinct_count
              FROM orders o JOIN order_versions v ON v.id=o.current_version_id
              JOIN order_items i ON i.order_version_id=v.id
              WHERE v.source_system='BHQ' GROUP BY v.id
            )
            """
        ).fetchone()[0]
        uncategorized = [row[0] for row in connection.execute(
            "SELECT o.order_no FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system='BHQ' AND o.category='..' ORDER BY o.order_no"
        )]
        missing_item_names = connection.execute(
            """
            SELECT COUNT(*) FROM order_items i JOIN order_versions v ON v.id=i.order_version_id
            LEFT JOIN web_order_item_metadata m ON m.order_item_id=i.id
            WHERE v.source_system='BHQ' AND TRIM(COALESCE(m.product_name,''))=''
            """
        ).fetchone()[0]
        amount_rows = {
            row["order_no"]: (row["discounted_total_amount"], row["original_total_amount"])
            for row in connection.execute(
                """
                SELECT o.order_no,v.discounted_total_amount,m.original_total_amount
                FROM orders o JOIN order_versions v ON v.id=o.current_version_id
                JOIN bhq_order_version_metadata m ON m.order_version_id=v.id
                WHERE v.source_system='BHQ'
                """
            )
        }
        amount_mismatches = [
            order["order_no"] for order in parsed["orders"]
            if amount_rows.get(order["order_no"])
            != (order["discounted_amount"], order["discounted_amount"])
        ]
        product_705429 = dict(connection.execute(
            "SELECT * FROM products WHERE gift_code='705429'"
        ).fetchone())
        new_products = [dict(row) for row in connection.execute(
            "SELECT id,gift_code,product_name,source FROM products WHERE id>? ORDER BY id",
            (max_ids["products"],),
        )]
        import_runs = [dict(row) for row in connection.execute(
            "SELECT * FROM bhq_import_runs ORDER BY id"
        )]
        duplicate_orders = connection.execute(
            "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"
        ).fetchone()[0]
        erp_discounted_non_null = connection.execute(
            "SELECT COUNT(*) FROM order_versions WHERE source_system<>'BHQ' AND discounted_total_amount IS NOT NULL"
        ).fetchone()[0]
        checks = {
            "count_increases_match": count_increases == expected_increases,
            "bhq_orders_257": bhq_orders == 257,
            "bhq_versions_257": bhq_versions == 257,
            "bhq_items_551": bhq_items == 551,
            "bhq_stores_18": bhq_stores == 18,
            "zero_quantity_15": zero_items == 15,
            "duplicate_code_rows_79": duplicate_code_rows == 79,
            "uncategorized_three_preserved": uncategorized == [
                "MP20260701-0001", "MP20260703-0001", "MP20260712-0001"
            ],
            "amount_mismatches_zero": not amount_mismatches,
            "missing_item_names_zero": missing_item_names == 0,
            "duplicate_orders_zero": duplicate_orders == 0,
            "erp_discounted_total_remains_null": erp_discounted_non_null == 0,
            "erp_and_protected_fingerprints_unchanged": all(fingerprint_checks.values()),
            "sr_store_unchanged": fingerprint_checks["sr_store"],
            "only_expected_product_added": len(new_products) == 1 and new_products[0]["gift_code"] == "705428",
            "705429_source_preserved": product_705429["source"] == "OCR JSON",
            "705429_name_filled": bool(str(product_705429["product_name"] or "").strip()),
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        checks["integrity_check_ok"] = integrity == "ok"
        checks["foreign_key_check_zero"] = foreign_keys == 0
        if args.stage == "final":
            checks["two_import_runs_recorded"] = len(import_runs) == 2
            checks["second_run_idempotent"] = bool(import_runs) and all(
                import_runs[-1][field] == 0
                for field in (
                    "inserted_order_count", "inserted_version_count", "inserted_item_count"
                )
            ) and import_runs[-1]["skipped_order_count"] == 257
        payload = {
            "stage": args.stage,
            "generated_at": datetime.now().astimezone().isoformat(),
            "database": str(DATABASE),
            "baseline": baseline,
            "current_counts": current_counts,
            "count_increases": count_increases,
            "expected_increases": expected_increases,
            "bhq": {
                "orders": bhq_orders, "versions": bhq_versions, "items": bhq_items,
                "stores": bhq_stores, "zero_quantity_items": zero_items,
                "duplicate_product_code_rows": duplicate_code_rows,
                "uncategorized_orders": uncategorized,
                "missing_item_names": missing_item_names,
                "amount_mismatches": amount_mismatches,
            },
            "product_705429_after": product_705429,
            "new_products": new_products,
            "import_runs": import_runs,
            "fingerprint_checks": fingerprint_checks,
            "integrity_check": integrity,
            "foreign_key_errors": foreign_keys,
            "checks": checks,
            "passed": all(checks.values()),
        }
    finally:
        connection.close()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
