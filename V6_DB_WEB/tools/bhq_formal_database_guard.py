from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


FORMAL_PROJECT_ROOT = Path(r"C:\Users\auntstella\Desktop\V6_DB_WEB").resolve()
FORMAL_DATABASE_PATH = (FORMAL_PROJECT_ROOT / "data" / "orders.db").resolve()
REQUIRED_COLUMNS = {
    "orders": {"id", "order_no", "current_version_id", "current_content_hash"},
    "order_versions": {
        "id",
        "order_id",
        "source_system",
        "discounted_total_amount",
    },
    "order_items": {"id", "order_version_id", "line_no", "gift_code", "quantity"},
    "web_order_item_metadata": {"order_item_id", "product_name", "created_at"},
    "bhq_import_runs": {
        "id",
        "source_filename",
        "source_file_sha256",
        "sheet_name",
        "started_at",
        "finished_at",
        "source_row_count",
        "source_order_count",
        "inserted_order_count",
        "inserted_version_count",
        "inserted_item_count",
        "skipped_order_count",
        "anomaly_count",
        "result",
        "error_summary",
        "summary_json",
        "created_at",
        "updated_at",
    },
    "bhq_store_aliases": {
        "source_store_name",
        "store_id",
        "created_at",
        "updated_at",
    },
    "bhq_order_version_metadata": {
        "order_version_id",
        "import_run_id",
        "raw_store_name",
        "original_total_amount",
        "source_rows_json",
        "created_at",
    },
    "database_migrations": {"version", "name", "checksum", "applied_at"},
    "system_settings": {"setting_key", "setting_value"},
}
REQUIRED_INDEXES = {
    "idx_bhq_import_file",
    "idx_bhq_version_import",
    "idx_order_versions_source_discounted",
}


def validate(project_root: Path, config_path: Path) -> dict[str, object]:
    root = project_root.resolve()
    if root != FORMAL_PROJECT_ROOT:
        raise ValueError(
            f"只允許固定正式專案 {FORMAL_PROJECT_ROOT}；拒絕R3、DUAL_R*及其他路徑：{root}"
        )
    config = json.loads(config_path.resolve().read_text(encoding="utf-8"))
    configured_database = Path(str(config.get("database") or "")).resolve()
    if configured_database != FORMAL_DATABASE_PATH:
        raise ValueError(
            f"正式設定只允許 {FORMAL_DATABASE_PATH}；收到 {configured_database}"
        )
    if not root.is_dir():
        raise FileNotFoundError(f"固定正式專案不存在：{root}")
    if not FORMAL_DATABASE_PATH.parent.is_dir():
        raise FileNotFoundError(
            f"固定 data 資料夾不存在，拒絕自動建立：{FORMAL_DATABASE_PATH.parent}"
        )
    if not FORMAL_DATABASE_PATH.is_file():
        raise FileNotFoundError(
            f"固定 orders.db 不存在，拒絕自動建立：{FORMAL_DATABASE_PATH}"
        )
    if FORMAL_DATABASE_PATH.stat().st_size <= 0:
        raise RuntimeError(f"固定 orders.db 是0位元組：{FORMAL_DATABASE_PATH}")

    try:
        connection = sqlite3.connect(
            f"{FORMAL_DATABASE_PATH.as_uri()}?mode=rw",
            uri=True,
            timeout=30,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
            missing_columns = {}
            for table, required in REQUIRED_COLUMNS.items():
                if table not in tables:
                    continue
                actual = {
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                missing = sorted(required - actual)
                if missing:
                    missing_columns[table] = missing
            missing_indexes = sorted(REQUIRED_INDEXES - indexes)
            migration = (
                connection.execute(
                    "SELECT MAX(version) FROM database_migrations"
                ).fetchone()[0]
                if "database_migrations" in tables
                else None
            )
            marker_row = (
                connection.execute(
                    "SELECT setting_value FROM system_settings WHERE setting_key='database_mode'"
                ).fetchone()
                if "system_settings" in tables
                else None
            )
            marker = str(marker_row[0]) if marker_row else ""
        finally:
            connection.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise RuntimeError(f"既有SQLite無法以mode=rw安全驗證：{exc}") from exc

    result = {
        "connection_mode": "rw_existing_query_only",
        "database_size": FORMAL_DATABASE_PATH.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "migration": migration,
        "database_mode": marker,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
    }
    if integrity != "ok":
        raise RuntimeError(f"integrity_check失敗：{result}")
    if migration is None or int(migration) < 7:
        raise RuntimeError(f"migration低於v7：{result}")
    if missing_tables or missing_columns or missing_indexes:
        raise RuntimeError(f"BHQ結構不完整：{result}")
    if marker != "formal":
        raise RuntimeError(f"正式資料庫識別標記錯誤：{result}")
    if foreign_keys:
        raise RuntimeError(f"foreign_key_check失敗：{result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="無寫入驗證固定BHQ正式資料庫；不建立任何資料夾或SQLite"
    )
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    checks = validate(args.project_root, args.config)
    print(
        json.dumps(
            {
                "status": "valid_existing_formal_database",
                "project_root": str(args.project_root.resolve()),
                "database": str(FORMAL_DATABASE_PATH),
                "checks": checks,
                "side_effects": {
                    "created_database": False,
                    "created_data_folder": False,
                    "applied_migration": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
