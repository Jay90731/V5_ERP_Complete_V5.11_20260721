from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_excel import dry_run_summary, parse_bhq_xls, write_dry_run_report
from v6_db_web.bhq_import_service import import_bhq
from v6_db_web.database_lock import database_write_lock
from v6_db_web.db import connect
from v6_db_web.formal_database import (
    create_formal_backup,
    database_mode,
    require_formal_marker,
)
from v6_db_web.migrations import apply_migrations


TEST_DATABASE = (ROOT / "data" / "orders_bhq_test.db").resolve()
FORMAL_DATABASE = (ROOT / "data" / "orders.db").resolve()
APPROVED_SOURCE_SHA256 = "9F3C1CA4B567B3E57F3EBF05AE07333BFCA7DC61202B2E2CFA0440063C1977BB"


def require_test_database(value: str) -> Path:
    path = Path(value).resolve()
    if path != TEST_DATABASE:
        raise ValueError(f"BHQ 測試階段只允許寫入 {TEST_DATABASE}")
    return path


def require_formal_database(value: str) -> Path:
    path = Path(value).resolve()
    if path != FORMAL_DATABASE:
        raise ValueError(f"BHQ 正式模式只允許寫入 {FORMAL_DATABASE}")
    return path


def require_approved_source(parsed: dict, approved_sha256: str | None) -> None:
    approved = str(approved_sha256 or "").strip().upper()
    if approved != APPROVED_SOURCE_SHA256:
        raise ValueError("正式模式必須提供核准的 --approved-sha256")
    if parsed["file_sha256"].upper() != approved:
        raise ValueError(
            f"來源檔 SHA-256 不符：actual={parsed['file_sha256']}, approved={approved}"
        )


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def preflight_formal(
    database: Path,
    parsed: dict,
    *,
    expected_migration: int,
    allow_existing_bhq: bool,
) -> dict:
    expected_dry_run = {
        "source_row_count": 903,
        "order_count": 257,
        "valid_item_count": 551,
        "empty_product_code_count": 352,
        "store_count": 18,
        "anomaly_count": 0,
    }
    actual_dry_run = {key: parsed[key] for key in expected_dry_run}
    if actual_dry_run != expected_dry_run:
        raise RuntimeError(
            f"正式 dry-run 基準不符：actual={actual_dry_run}, expected={expected_dry_run}"
        )
    connection = readonly_connection(database)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        latest_migration = connection.execute(
            "SELECT MAX(version) FROM database_migrations"
        ).fetchone()[0]
        mode = database_mode(connection)
        bhq_versions = connection.execute(
            "SELECT COUNT(*) FROM order_versions WHERE source_system='BHQ'"
        ).fetchone()[0]
        placeholders = ",".join("?" for _ in parsed["orders"])
        order_nos = [order["order_no"] for order in parsed["orders"]]
        collisions = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT o.order_no,v.source_system FROM orders o
                LEFT JOIN order_versions v ON v.id=o.current_version_id
                WHERE o.order_no IN ({placeholders}) AND COALESCE(v.source_system,'')<>'BHQ'
                ORDER BY o.order_no
                """,
                order_nos,
            )
        ]
        if integrity != "ok" or foreign_key_errors != 0:
            raise RuntimeError(
                f"正式庫健康檢查失敗：integrity={integrity}, fk={foreign_key_errors}"
            )
        if mode != "formal":
            raise RuntimeError(f"正式庫 marker 不符：database_mode={mode!r}")
        if latest_migration != expected_migration:
            raise RuntimeError(
                f"正式 migration 版本不符：actual={latest_migration}, expected={expected_migration}"
            )
        if not allow_existing_bhq and bhq_versions != 0:
            raise RuntimeError(f"正式庫已存在 BHQ 版本：{bhq_versions}")
        if collisions:
            raise RuntimeError(f"BHQ 與既有非 BHQ 訂單撞號：{collisions}")
        return {
            "database": str(database),
            "database_mode": mode,
            "integrity_check": integrity,
            "foreign_key_errors": foreign_key_errors,
            "latest_migration": latest_migration,
            "bhq_versions": bhq_versions,
            "cross_system_collisions": collisions,
            "dry_run": actual_dry_run,
        }
    finally:
        connection.close()


def validate_bhq_schema(database: Path) -> dict:
    connection = readonly_connection(database)
    try:
        version_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(order_versions)")
        }
        required_tables = {
            "bhq_import_runs", "bhq_store_aliases", "bhq_order_version_metadata"
        }
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required_indexes = {
            "idx_bhq_import_file", "idx_bhq_version_import",
            "idx_order_versions_source_discounted",
        }
        indexes = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        erp_non_null = connection.execute(
            "SELECT COUNT(*) FROM order_versions WHERE source_system<>'BHQ' AND discounted_total_amount IS NOT NULL"
        ).fetchone()[0]
        migration = dict(
            connection.execute(
                "SELECT version,name,checksum,applied_at FROM database_migrations WHERE version=7"
            ).fetchone()
        )
        checks = {
            "discounted_total_amount_present": "discounted_total_amount" in version_columns,
            "required_tables_present": required_tables.issubset(tables),
            "required_indexes_present": required_indexes.issubset(indexes),
            "erp_discounted_total_remains_null": erp_non_null == 0,
        }
        if not all(checks.values()):
            raise RuntimeError(f"BHQ migration 結構驗證失敗：{checks}")
        return {"checks": checks, "migration": migration, "erp_non_null_count": erp_non_null}
    finally:
        connection.close()


def save_report(path: str | None, payload: dict) -> None:
    if not path:
        return
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="BHQ Excel 訂單匯入")
    parser.add_argument("--source", required=True, help="BHQ .xls 路徑")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="只解析及驗證")
    action.add_argument("--migrate-test", action="store_true", help="只遷移核准測試庫")
    action.add_argument("--import-test", action="store_true", help="匯入核准測試庫")
    action.add_argument("--migrate-formal", action="store_true", help="備份並遷移核准正式庫")
    action.add_argument("--import-formal", action="store_true", help="匯入核准正式庫")
    parser.add_argument("--db", help="測試或正式模式的固定資料庫路徑")
    parser.add_argument("--approved-sha256", help="正式模式核准來源 SHA-256")
    parser.add_argument(
        "--accept-existing-v7",
        action="store_true",
        help="明確核准以既有 v7 正式庫為新基準；僅供受保護正式 migration 階段",
    )
    parser.add_argument("--report", help="dry-run JSON 報告路徑")
    args = parser.parse_args()
    result = parse_bhq_xls(args.source)
    if args.migrate_test or args.import_test:
        if not args.db:
            parser.error("--migrate-test/--import-test 必須提供 --db")
        database = require_test_database(args.db)
        connection = connect(database)
        try:
            migrations = apply_migrations(connection, ROOT / "migrations")
            if args.migrate_test:
                print(json.dumps({"database": str(database), "migrations": migrations}, ensure_ascii=False, indent=2))
                return 0
            stats = import_bhq(connection, result)
            print(json.dumps({"database": str(database), "import": stats.as_dict()}, ensure_ascii=False, indent=2))
            return 0
        finally:
            connection.close()

    if args.migrate_formal:
        if not args.db:
            parser.error("--migrate-formal 必須提供 --db")
        database = require_formal_database(args.db)
        require_approved_source(result, args.approved_sha256)
        expected_migration = 7 if args.accept_existing_v7 else 6
        with database_write_lock(database, owner="bhq:migrate-formal"):
            preflight = preflight_formal(
                database,
                result,
                expected_migration=expected_migration,
                allow_existing_bhq=False,
            )
            backup = create_formal_backup(
                database,
                ROOT,
                backup_type="pre_update",
                notes="Immediately before BHQ migration 007 formal deployment",
            )
            connection = connect(database)
            try:
                require_formal_marker(connection)
                migrations = apply_migrations(connection, ROOT / "migrations")
            finally:
                connection.close()
            schema = validate_bhq_schema(database)
        payload = {
            "phase": "bhq_formal_migration",
            "accepted_existing_v7_baseline": bool(args.accept_existing_v7),
            "source_sha256": result["file_sha256"],
            "preflight": preflight,
            "backup": backup,
            "migrations": migrations,
            "schema_validation": schema,
        }
        save_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.import_formal:
        if not args.db:
            parser.error("--import-formal 必須提供 --db")
        database = require_formal_database(args.db)
        require_approved_source(result, args.approved_sha256)
        with database_write_lock(database, owner="bhq:import-formal"):
            preflight = preflight_formal(
                database, result, expected_migration=7, allow_existing_bhq=True
            )
            connection = connect(database)
            try:
                require_formal_marker(connection)
                codes = sorted(
                    {item["product_code"] for order in result["orders"] for item in order["items"]}
                )
                placeholders = ",".join("?" for _ in codes)
                existing = {
                    row[0]
                    for row in connection.execute(
                        f"SELECT gift_code FROM products WHERE gift_code IN ({placeholders})", codes
                    )
                }
                missing_products = sorted(set(codes) - existing)
                if preflight["bhq_versions"] == 0 and missing_products != ["705428"]:
                    raise RuntimeError(
                        f"正式首次匯入產品預檢不符：expected=['705428'], actual={missing_products}"
                    )
                if preflight["bhq_versions"] != 0 and missing_products:
                    raise RuntimeError(f"正式冪等重跑仍缺產品：{missing_products}")
                stats = import_bhq(connection, result)
            finally:
                connection.close()
        payload = {
            "phase": "bhq_formal_import",
            "source_sha256": result["file_sha256"],
            "preflight": preflight,
            "missing_products_before_import": missing_products,
            "import": stats.as_dict(),
        }
        save_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    report = args.report or str(
        ROOT
        / "reports"
        / f"BHQ_DRY_RUN_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    destination = write_dry_run_report(result, report)
    summary = dry_run_summary(result)
    summary["report"] = str(destination)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["anomaly_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
