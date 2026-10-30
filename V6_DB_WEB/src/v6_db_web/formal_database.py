from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import checkpoint, connect
from .importer import import_v5, read_json, resolve_v5_state
from .migrations import apply_migrations
from .canonical import normalize_order
from .store_rules import store_policy_metrics
from .validation import database_counts, validate_v5_import


FORMAL_CONFIRMATION = "CREATE-FORMAL-ORDERS-DB"
RESTORE_CONFIRMATION = "RESTORE-FORMAL-ORDERS-DB"
EXPECTED_BASELINE = {
    "orders": 1243,
    "order_versions": 1243,
    "order_items": 976,
    "multi_item_orders": 102,
    "pending_reviews": 2,
    "duplicate_orders": 0,
    "missing_detail_orders": 399,
    "official_orders": 450,
    "non_null_store_ids": 450,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def formal_database_path(root: Path) -> Path:
    return (root / "data" / "orders.db").resolve()


def test_database_path(root: Path) -> Path:
    return (root / "data" / "orders_test.db").resolve()


def require_formal_path(path: str | Path, root: Path) -> Path:
    resolved = Path(path).resolve()
    expected = formal_database_path(root)
    if resolved != expected:
        raise ValueError(
            f"Formal mode only permits {expected}; received {resolved}"
        )
    if resolved == test_database_path(root):
        raise ValueError("Formal mode cannot use orders_test.db")
    return resolved


def reject_formal_path(path: str | Path, root: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == formal_database_path(root) or resolved.name.lower() == "orders.db":
        raise ValueError(
            "Test/general commands cannot write orders.db; use a formal-* command"
        )
    return resolved


def database_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT setting_value FROM system_settings WHERE setting_key = 'database_mode'"
    ).fetchone()
    return str(row[0]) if row else ""


def require_formal_marker(connection: sqlite3.Connection) -> None:
    mode = database_mode(connection)
    if mode != "formal":
        raise RuntimeError(
            f"Database is not marked as formal (database_mode={mode!r})"
        )


def initialize_formal_database(
    database: str | Path,
    migrations: str | Path,
    root: Path,
    confirmation: str,
) -> dict[str, Any]:
    destination = require_formal_path(database, root)
    if confirmation != FORMAL_CONFIRMATION:
        raise PermissionError("Formal database initialization was not confirmed")
    if destination.exists():
        raise FileExistsError(
            f"Formal database already exists and will not be overwritten: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.initializing"
    )
    connection = connect(temporary)
    try:
        migration_result = apply_migrations(connection, migrations)
        now = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """
                INSERT INTO system_settings(
                    setting_key, setting_value, value_type, description, updated_at
                ) VALUES (?, ?, 'text', ?, ?)
                """,
                [
                    (
                        "database_mode",
                        "formal",
                        "Hard safety marker for the production SQLite database",
                        now,
                    ),
                    (
                        "formal_database_initialized_at",
                        now,
                        "Formal database initialization timestamp",
                        now,
                    ),
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        checkpoint(connection)
    finally:
        connection.close()

    if quick_check != "ok" or foreign_keys:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"New formal database failed validation: quick={quick_check}, fk={foreign_keys}"
        )
    os.replace(temporary, destination)
    for relative in (
        "data/images/official",
        "data/images/pending_review",
        "data/images/archive",
        "backups/formal",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return {
        "database": str(destination),
        "created_from_migrations": True,
        "migrations": migration_result,
        "quick_check": quick_check,
        "foreign_key_errors": foreign_keys,
        "database_mode": "formal",
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_database_file(path: Path, require_marker: bool = True) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        mode = ""
        if require_marker:
            row = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key='database_mode'"
            ).fetchone()
            mode = str(row[0]) if row else ""
    finally:
        connection.close()
    return {
        "quick_check": quick_check,
        "foreign_key_errors": foreign_keys,
        "database_mode": mode,
        "valid": quick_check == "ok" and foreign_keys == 0 and (
            not require_marker or mode == "formal"
        ),
    }


def create_formal_backup(
    database: str | Path,
    root: Path,
    backup_type: str = "manual",
    notes: str = "",
) -> dict[str, Any]:
    source = require_formal_path(database, root)
    if not source.is_file():
        raise FileNotFoundError(f"Formal database does not exist: {source}")
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = root / "backups" / "formal"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"orders_{timestamp}.db"

    source_connection = connect(source)
    try:
        require_formal_marker(source_connection)
        checkpoint(source_connection)
        backup_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()
        validation = validate_database_file(destination)
        if not validation["valid"]:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Backup validation failed: {validation}")
        digest = file_sha256(destination)
        relative = destination.relative_to(root).as_posix()
        now = utc_now()
        source_connection.execute(
            """
            INSERT INTO backup_records(
                backup_type, relative_path, sha256, database_size, status,
                created_at, verified_at, restored_at, notes
            ) VALUES (?, ?, ?, ?, 'verified', ?, ?, NULL, ?)
            """,
            (
                backup_type,
                relative,
                digest,
                destination.stat().st_size,
                now,
                now,
                notes,
            ),
        )
        source_connection.commit()
        checkpoint(source_connection)
    finally:
        source_connection.close()
    return {
        "backup": str(destination),
        "relative_path": relative,
        "sha256": digest,
        "size": destination.stat().st_size,
        **validation,
    }


def source_baseline(source: str | Path) -> dict[str, int]:
    state = resolve_v5_state(source)
    history = read_json(state / "order_history.json")
    current = read_json(state / "current_orders.json")
    entries = list(history["orders"].values())
    details = [entry["current_version"].get("details") or [] for entry in entries]
    normalized = [normalize_order(entry["current_version"]) for entry in entries]
    official_orders = sum(
        order["department_code"] == "4001"
        and not order["is_voided"]
        and bool(order["details"])
        for order in normalized
    )
    return {
        "orders": len(entries),
        "order_versions": sum(len(entry.get("versions") or []) for entry in entries),
        "order_items": sum(len(items) for items in details),
        "multi_item_orders": sum(len(items) > 1 for items in details),
        "pending_reviews": sum(
            record.get("status") == "待確認" for record in current.get("orders") or []
        ),
        "duplicate_orders": len(entries)
        - len({entry["current_version"].get("order_no") for entry in entries}),
        "missing_detail_orders": sum(not items for items in details),
        "official_orders": official_orders,
        "non_null_store_ids": official_orders,
    }


def formal_database_metrics(connection: sqlite3.Connection) -> dict[str, int | str]:
    return {
        "orders": connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "order_versions": connection.execute(
            "SELECT COUNT(*) FROM order_versions"
        ).fetchone()[0],
        "order_items": connection.execute("SELECT COUNT(*) FROM order_items").fetchone()[0],
        "multi_item_orders": connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT o.id FROM orders o
                JOIN order_items i ON i.order_version_id=o.current_version_id
                GROUP BY o.id HAVING COUNT(i.id)>1
            )
            """
        ).fetchone()[0],
        "pending_reviews": connection.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
        ).fetchone()[0],
        "duplicate_orders": connection.execute(
            "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"
        ).fetchone()[0],
        "duplicate_versions": connection.execute(
            "SELECT COUNT(*) FROM (SELECT order_id,content_hash FROM order_versions GROUP BY order_id,content_hash HAVING COUNT(*)>1)"
        ).fetchone()[0],
        "missing_detail_orders": connection.execute(
            """
            SELECT COUNT(*) FROM orders o JOIN order_versions v
            ON v.id=o.current_version_id WHERE v.detail_count=0
            """
        ).fetchone()[0],
        **store_policy_metrics(connection),
        "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        "foreign_key_errors": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
    }


def validate_formal_database(
    database: str | Path, source: str | Path, root: Path
) -> dict[str, Any]:
    path = require_formal_path(database, root)
    connection = connect(path)
    try:
        require_formal_marker(connection)
        source_validation = validate_v5_import(connection, source)
        actual_source = source_baseline(source)
        metrics = formal_database_metrics(connection)
        checks = {
            "formal_path_is_exact": path == formal_database_path(root),
            "formal_marker_present": database_mode(connection) == "formal",
            "source_matches_documented_baseline": actual_source == EXPECTED_BASELINE,
            "orders_match_source": metrics["orders"] == actual_source["orders"],
            "versions_match_source": metrics["order_versions"] == actual_source["order_versions"],
            "items_match_source": metrics["order_items"] == actual_source["order_items"],
            "multi_items_match_source": metrics["multi_item_orders"] == actual_source["multi_item_orders"],
            "pending_match_source": metrics["pending_reviews"] == actual_source["pending_reviews"],
            "missing_details_preserved": metrics["missing_detail_orders"] == actual_source["missing_detail_orders"],
            "no_duplicate_orders": metrics["duplicate_orders"] == 0,
            "no_duplicate_versions": metrics["duplicate_versions"] == 0,
            "one_sr_store_only": metrics["sr_store_count"] == 1
            and metrics["store_count"] == 1,
            "all_official_orders_use_sr": metrics["official_orders_with_sr"]
            == metrics["official_orders"],
            "non_official_orders_have_no_store": metrics["non_official_with_store"]
            == 0,
            "quick_check_ok": metrics["quick_check"] == "ok",
            "foreign_keys_valid": metrics["foreign_key_errors"] == 0,
            "v5_field_validation_passed": source_validation["passed"],
        }
        return {
            "database": str(path),
            "expected_baseline": EXPECTED_BASELINE,
            "actual_source_baseline": actual_source,
            "database_metrics": metrics,
            "table_counts": database_counts(connection),
            "checks": checks,
            "passed": all(checks.values()),
            "source_validation": source_validation,
        }
    finally:
        checkpoint(connection)
        connection.close()


def import_formal_v5(
    database: str | Path, source: str | Path, root: Path
) -> dict[str, Any]:
    path = require_formal_path(database, root)
    backup = create_formal_backup(
        path, root, backup_type="pre_update", notes="Before V5.10 baseline import"
    )
    connection = connect(path)
    try:
        require_formal_marker(connection)
        stats = import_v5(connection, source)
        checkpoint(connection)
    finally:
        connection.close()
    validation = validate_formal_database(path, source, root)
    return {"backup": backup, "import": stats.as_dict(), "validation": validation}


def apply_formal_store_migration(
    database: str | Path,
    migrations: str | Path,
    root: Path,
) -> dict[str, Any]:
    path = require_formal_path(database, root)
    connection = connect(path)
    try:
        require_formal_marker(connection)
        before = formal_database_metrics(connection)
        departments_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT order_no, department_code FROM orders ORDER BY order_no"
            )
        ]
    finally:
        connection.close()

    backup = create_formal_backup(
        path,
        root,
        backup_type="pre_update",
        notes="Before migration 005 SR-only official store policy",
    )

    connection = connect(path)
    try:
        require_formal_marker(connection)
        migration_result = apply_migrations(connection, migrations)
        after = formal_database_metrics(connection)
        departments_after = [
            tuple(row)
            for row in connection.execute(
                "SELECT order_no, department_code FROM orders ORDER BY order_no"
            )
        ]
        checks = {
            "orders_unchanged": after["orders"] == before["orders"],
            "versions_unchanged": after["order_versions"]
            == before["order_versions"],
            "items_unchanged": after["order_items"] == before["order_items"],
            "department_codes_unchanged": departments_after
            == departments_before,
            "one_sr_store_only": after["sr_store_count"] == 1
            and after["store_count"] == 1,
            "all_official_orders_use_sr": after["official_orders_with_sr"]
            == after["official_orders"],
            "non_official_orders_have_no_store": after["non_official_with_store"]
            == 0,
            "quick_check_ok": after["quick_check"] == "ok",
            "foreign_keys_valid": after["foreign_key_errors"] == 0,
        }
        details = {
            "backup": backup["relative_path"],
            "before": before,
            "after": after,
            "checks": checks,
            "migrations": migration_result,
        }
        connection.execute(
            """
            INSERT INTO audit_logs(
                event_type, entity_type, entity_id, actor,
                details_json, created_at
            ) VALUES (
                'formal_sr_store_update', 'database', 'orders.db',
                'formal_migration_tool', ?, ?
            )
            """,
            (json.dumps(details, ensure_ascii=False, sort_keys=True), utc_now()),
        )
        connection.commit()
        checkpoint(connection)
    finally:
        connection.close()

    if not all(checks.values()):
        raise RuntimeError(f"Formal SR store migration validation failed: {checks}")
    return {
        "database": str(path),
        "backup": backup,
        "migrations": migration_result,
        "before": before,
        "after": after,
        "checks": checks,
        "passed": True,
    }


def restore_formal_database(
    database: str | Path,
    backup: str | Path,
    root: Path,
    confirmation: str,
) -> dict[str, Any]:
    destination = require_formal_path(database, root)
    if confirmation != RESTORE_CONFIRMATION:
        raise PermissionError("Formal database restore was not confirmed")
    source = Path(backup).resolve()
    allowed_root = (root / "backups" / "formal").resolve()
    if allowed_root not in source.parents or not source.is_file():
        raise ValueError(f"Restore source must be a formal backup under {allowed_root}")
    source_validation = validate_database_file(source)
    if not source_validation["valid"]:
        raise RuntimeError(f"Restore source is invalid: {source_validation}")

    pre_restore = create_formal_backup(
        destination, root, backup_type="pre_update", notes="Before formal restore"
    )
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.restoring"
    )
    shutil.copy2(source, temporary)
    copied_validation = validate_database_file(temporary)
    if not copied_validation["valid"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Copied restore database is invalid: {copied_validation}")
    os.replace(temporary, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination) + suffix)
        for attempt in range(8):
            try:
                sidecar.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.05 * (attempt + 1))

    connection = connect(destination)
    try:
        require_formal_marker(connection)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO backup_records(
                backup_type, relative_path, sha256, database_size, status,
                created_at, verified_at, restored_at, notes
            ) VALUES ('manual', ?, ?, ?, 'restored', ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                status='restored', restored_at=excluded.restored_at,
                notes=excluded.notes
            """,
            (
                source.relative_to(root).as_posix(),
                file_sha256(source),
                source.stat().st_size,
                now,
                now,
                now,
                f"Restored; pre-restore backup={pre_restore['relative_path']}",
            ),
        )
        connection.commit()
        checkpoint(connection)
    finally:
        connection.close()
    return {
        "database": str(destination),
        "restored_from": str(source),
        "pre_restore_backup": pre_restore,
        "validation": validate_database_file(destination),
    }


def write_formal_report(path: str | Path, report: dict[str, Any]) -> tuple[Path, Path]:
    json_path = Path(path).resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = json_path.with_suffix(".md")
    validation = report.get("validation", report)
    metrics = validation.get("database_metrics", {})
    checks = validation.get("checks", {})
    lines = [
        "# V6 第四階段正式資料庫驗證報告",
        "",
        f"- 結果：{'通過' if validation.get('passed') else '失敗'}",
        f"- 訂單：{metrics.get('orders', 0)}",
        f"- 訂單版本：{metrics.get('order_versions', 0)}",
        f"- 明細：{metrics.get('order_items', 0)}",
        f"- 多品項訂單：{metrics.get('multi_item_orders', 0)}",
        f"- 原始缺少明細：{metrics.get('missing_detail_orders', 0)}",
        f"- 待確認：{metrics.get('pending_reviews', 0)}",
        f"- quick_check：{metrics.get('quick_check', '')}",
        f"- 外鍵錯誤：{metrics.get('foreign_key_errors', '')}",
        "",
        "## 檢查項目",
        "",
    ]
    lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in checks.items())
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
