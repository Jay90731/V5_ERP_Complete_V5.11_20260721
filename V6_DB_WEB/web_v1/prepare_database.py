from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from wedding_web.write_service import ensure_manual_schema


FORMAL_WHERE = """
o.department_code = '4001'
AND o.is_voided = 0
AND o.status = 'confirmed'
AND EXISTS (
  SELECT 1 FROM order_items current_item
  WHERE current_item.order_version_id = o.current_version_id
)
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def normalize_code(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def load_menuitem(path: Path) -> dict[str, str]:
    import xlrd

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        sheet = workbook.sheet_by_index(0)
        header_row = code_column = name_column = None
        for row_index in range(min(10, sheet.nrows)):
            headers = [
                str(sheet.cell_value(row_index, column)).strip()
                for column in range(sheet.ncols)
            ]
            if "產品代碼" in headers and "品項名稱" in headers:
                header_row = row_index
                code_column = headers.index("產品代碼")
                name_column = headers.index("品項名稱")
                break
        if header_row is None or code_column is None or name_column is None:
            raise ValueError("MENUITEM 缺少產品代碼或品項名稱欄位")
        result: dict[str, str] = {}
        for row_index in range(header_row + 1, sheet.nrows):
            code = normalize_code(sheet.cell_value(row_index, code_column))
            name = str(sheet.cell_value(row_index, name_column) or "").strip()
            if code and name:
                result[code] = name
        if not result:
            raise ValueError("MENUITEM 沒有可使用的商品資料")
        return result
    finally:
        workbook.release_resources()


def apply_web_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS web_schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS web_metadata (
          setting_key TEXT PRIMARY KEY,
          setting_value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS web_order_metadata (
          order_id INTEGER PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
          customer_name TEXT,
          customer_source TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history_month_gaps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          month TEXT NOT NULL UNIQUE,
          expected_order_no TEXT NOT NULL,
          first_observed_order_no TEXT,
          status TEXT NOT NULL CHECK(status IN ('open', 'covered')),
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS image_cleanup_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          retention_days INTEGER NOT NULL,
          deleted_count INTEGER NOT NULL DEFAULT 0,
          missing_count INTEGER NOT NULL DEFAULT 0,
          protected_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          details_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_web_orders_date_category
          ON orders(order_date, category);
        CREATE INDEX IF NOT EXISTS idx_web_orders_department_status
          ON orders(department_code, is_voided, status);
        CREATE INDEX IF NOT EXISTS idx_web_items_version_code
          ON order_items(order_version_id, gift_code);
        CREATE INDEX IF NOT EXISTS idx_web_review_status_updated
          ON review_queue(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_web_products_name
          ON products(product_name);

        INSERT OR IGNORE INTO web_schema_migrations(version, name, applied_at)
        VALUES(1, 'web_v1_read_model', CURRENT_TIMESTAMP);

        INSERT OR IGNORE INTO web_order_metadata(
          order_id, customer_name, customer_source, updated_at
        )
        SELECT id, NULL, 'source_not_available', CURRENT_TIMESTAMP FROM orders;
        """
    )
    ensure_manual_schema(connection)


def ensure_sr(connection: sqlite3.Connection) -> dict[str, int]:
    now = datetime.now().astimezone().isoformat()
    rows = connection.execute(
        "SELECT id FROM stores WHERE store_code = 'SR' ORDER BY id"
    ).fetchall()
    if not rows:
        connection.execute(
            """
            INSERT INTO stores(
              store_code, store_name, active, status, created_at, updated_at
            ) VALUES('SR', 'SR', 1, 'active', ?, ?)
            """,
            (now, now),
        )
        sr_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        sr_id = int(rows[0][0])
    connection.execute(
        """
        UPDATE orders AS o SET store_id = ?
        WHERE """
        + FORMAL_WHERE
        + " AND (o.store_id IS NULL OR o.store_id <> ?)",
        (sr_id, sr_id),
    )
    formal_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM orders o WHERE " + FORMAL_WHERE
        ).fetchone()[0]
    )
    formal_sr_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM orders o WHERE "
            + FORMAL_WHERE
            + " AND o.store_id = ?",
            (sr_id,),
        ).fetchone()[0]
    )
    return {"sr_id": sr_id, "formal_count": formal_count, "formal_sr_count": formal_sr_count}


def import_product_names(
    connection: sqlite3.Connection, menuitem_path: Path
) -> dict[str, Any]:
    products = load_menuitem(menuitem_path)
    now = datetime.now().astimezone().isoformat()
    updated = inserted = 0
    for code, name in products.items():
        existing = connection.execute(
            "SELECT id, product_name FROM products WHERE gift_code = ?", (code,)
        ).fetchone()
        if existing:
            if str(existing[1] or "") != name:
                connection.execute(
                    """
                    UPDATE products
                    SET product_name=?, source='MENUITEM.XLS', updated_at=?
                    WHERE id=?
                    """,
                    (name, now, int(existing[0])),
                )
                updated += 1
        else:
            connection.execute(
                """
                INSERT INTO products(
                  gift_code, product_name, active, source, created_at, updated_at
                ) VALUES(?, ?, 1, 'MENUITEM.XLS', ?, ?)
                """,
                (code, name, now, now),
            )
            inserted += 1
    return {
        "source": str(menuitem_path),
        "source_sha256": sha256(menuitem_path),
        "lookup_count": len(products),
        "updated_existing": updated,
        "inserted_lookup_only": inserted,
    }


def build_month_gaps(
    connection: sqlite3.Connection, checkpoint_path: Path
) -> dict[str, Any]:
    document = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
    months = [
        item for item in document.get("months", []) if item.get("status") == "completed"
    ]
    now = datetime.now().astimezone().isoformat()
    open_months: list[str] = []
    for entry in months:
        month = str(entry["month"])
        yy, mm = month[2:4], month[5:7]
        expected = f"GOU{yy}{mm}0001"
        found_scan = int(
            connection.execute(
                "SELECT COUNT(*) FROM scan_items WHERE order_no = ?", (expected,)
            ).fetchone()[0]
        )
        found_order = int(
            connection.execute(
                "SELECT COUNT(*) FROM orders WHERE order_no = ?", (expected,)
            ).fetchone()[0]
        )
        first = connection.execute(
            """
            SELECT MIN(order_no) FROM scan_items
            WHERE order_no LIKE ? AND order_no GLOB 'GOU[0-9]*'
            """,
            (f"GOU{yy}{mm}%",),
        ).fetchone()[0]
        status = "covered" if found_scan or found_order else "open"
        if status == "open":
            open_months.append(month)
        evidence = json.dumps(
            {
                "checkpoint_status": entry.get("status"),
                "found_in_scan_items": found_scan,
                "found_in_orders": found_order,
                "first_observed_order_no": first,
                "rule": "record_only_no_rescan",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO history_month_gaps(
              month, expected_order_no, first_observed_order_no,
              status, evidence_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
              expected_order_no=excluded.expected_order_no,
              first_observed_order_no=excluded.first_observed_order_no,
              status=excluded.status,
              evidence_json=excluded.evidence_json,
              updated_at=excluded.updated_at
            """,
            (month, expected, first, status, evidence, now, now),
        )
    return {
        "completed_months": len(months),
        "open_gap_count": len(open_months),
        "open_gap_months": open_months,
    }


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "orders",
        "order_versions",
        "order_items",
        "review_queue",
        "scan_items",
        "scan_jobs",
        "screenshots",
        "products",
    ]
    result = {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }
    result["pending_review"] = int(
        connection.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
        ).fetchone()[0]
    )
    result["excluded_scan_items"] = int(
        connection.execute(
            "SELECT COUNT(*) FROM scan_items WHERE status='skipped'"
        ).fetchone()[0]
    )
    result["formal_orders"] = int(
        connection.execute(
            "SELECT COUNT(*) FROM orders o WHERE " + FORMAL_WHERE
        ).fetchone()[0]
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--menuitem", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    source = args.source_db.resolve()
    output = args.output_db.resolve()
    if source == output:
        raise SystemExit("來源資料庫與網站資料庫不可為同一路徑")
    for path in (source, args.checkpoint, args.menuitem):
        if not path.is_file():
            raise SystemExit(f"找不到必要檔案：{path}")
    if output.exists():
        raise SystemExit(f"網站資料庫已存在，拒絕覆寫：{output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    source_connection = open_readonly(source)
    try:
        source_quick = source_connection.execute("PRAGMA quick_check").fetchone()[0]
        source_fk = len(source_connection.execute("PRAGMA foreign_key_check").fetchall())
        source_counts = counts(source_connection)
        destination = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination)
        finally:
            destination.close()
    finally:
        source_connection.close()

    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        before = counts(connection)
        with connection:
            apply_web_schema(connection)
            sr = ensure_sr(connection)
            menuitem = import_product_names(connection, args.menuitem.resolve())
            gaps = build_month_gaps(connection, args.checkpoint.resolve())
            now = datetime.now().astimezone().isoformat()
            metadata = {
                "web_version": "V1",
                "source_database_sha256": sha256(source),
                "source_checkpoint_sha256": sha256(args.checkpoint.resolve()),
                "source_database_path": source.name,
                "prepared_at": now,
                "customer_data_status": "source_not_available",
                "successful_image_retention_days": "7",
                "pending_images_protected": "true",
            }
            connection.executemany(
                """
                INSERT INTO web_metadata(setting_key, setting_value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                  setting_value=excluded.setting_value,
                  updated_at=excluded.updated_at
                """,
                [(key, value, now) for key, value in metadata.items()],
            )
        after = counts(connection)
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        duplicate_orders = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        duplicate_versions = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT order_id, content_hash FROM order_versions
                  GROUP BY order_id, content_hash HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        if quick != "ok" or foreign_keys or duplicate_orders or duplicate_versions:
            raise RuntimeError("網站資料庫完整性驗證失敗")
        for table in ("orders", "order_versions", "order_items", "review_queue", "scan_items"):
            if before[table] != after[table]:
                raise RuntimeError(f"整理過程不應改變 {table} 筆數")
        if sr["formal_count"] != sr["formal_sr_count"]:
            raise RuntimeError("正式訂單尚未全部指向 SR")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    os.replace(temporary, output)
    report = {
        "status": "passed",
        "prepared_at": datetime.now().astimezone().isoformat(),
        "source_database": str(source),
        "source_database_sha256": sha256(source),
        "source_quick_check": source_quick,
        "source_foreign_key_errors": source_fk,
        "source_counts": source_counts,
        "website_database": str(output),
        "website_database_sha256": sha256(output),
        "website_quick_check": quick,
        "website_foreign_key_errors": foreign_keys,
        "duplicate_order_numbers": duplicate_orders,
        "duplicate_order_versions": duplicate_versions,
        "website_counts": after,
        "sr_validation": sr,
        "menuitem": menuitem,
        "month_gaps": gaps,
        "customer_data": "原始 OCR 與 SQLite 未提供客戶名稱，欄位已預留但未猜測補值。",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
