from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


CONFIRMED_DATES = {
    "GOU26080034": "2026-08-03",
    "GOU26080170": "2026-08-07",
    "GOU26080244": "2026-08-11",
    "GOU26080424": "2026-08-21",
    "GOU26080426": "2026-08-22",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def backup_sqlite(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.execute("PRAGMA wal_checkpoint(FULL)")
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    check = sqlite3.connect(temporary)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite backup quick_check failed")
        if check.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("SQLite backup foreign_key_check failed")
    finally:
        check.close()
    os.replace(temporary, destination)


def validate_database(database: Path, data_dir: Path) -> dict:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        scalar = lambda sql: int(connection.execute(sql).fetchone()[0])
        dates = {
            row["order_no"]: row["order_date"]
            for row in connection.execute(
                "SELECT order_no,order_date FROM orders WHERE order_no IN (%s)"
                % ",".join("?" for _ in CONFIRMED_DATES),
                tuple(CONFIRMED_DATES),
            )
        }
        missing_paths = []
        for row in connection.execute(
            """
            SELECT wpi.relative_path FROM web_pending_images wpi
            JOIN review_queue rq ON rq.id=wpi.review_queue_id
            WHERE rq.status='pending' AND wpi.image_status='available'
            """
        ):
            if not (data_dir / row[0]).is_file():
                missing_paths.append(row[0])
        counts = {
            "orders": scalar("SELECT COUNT(*) FROM orders"),
            "versions": scalar("SELECT COUNT(*) FROM order_versions"),
            "items": scalar("SELECT COUNT(*) FROM order_items"),
            "formal_sr": scalar(
                """
                SELECT COUNT(*) FROM orders o JOIN stores s ON s.id=o.store_id
                WHERE o.status='confirmed' AND o.department_code='4001'
                  AND o.is_voided=0 AND s.store_code='SR'
                  AND EXISTS(SELECT 1 FROM order_items i WHERE i.order_version_id=o.current_version_id)
                """
            ),
            "pending": scalar("SELECT COUNT(*) FROM review_queue WHERE status='pending'"),
            "duplicate_orders": scalar(
                "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"
            ),
            "duplicate_versions": scalar(
                "SELECT COUNT(*) FROM (SELECT order_id,content_hash FROM order_versions GROUP BY order_id,content_hash HAVING COUNT(*)>1)"
            ),
            "running_jobs": scalar("SELECT COUNT(*) FROM scan_jobs WHERE status='running'"),
            "retention_missing": scalar(
                """
                SELECT COUNT(*) FROM web_pending_images wpi
                JOIN review_queue rq ON rq.id=wpi.review_queue_id
                WHERE rq.status IN('resolved','excluded') AND wpi.image_status='available'
                  AND (wpi.processed_at IS NULL OR wpi.retained_until IS NULL)
                """
            ),
        }
    finally:
        connection.close()
    passed = (
        quick == "ok"
        and foreign == 0
        and counts["duplicate_orders"] == 0
        and counts["duplicate_versions"] == 0
        and counts["running_jobs"] == 0
        and counts["retention_missing"] == 0
        and not missing_paths
        and dates == CONFIRMED_DATES
    )
    return {
        "passed": passed,
        "quick_check": quick,
        "foreign_key_errors": foreign,
        "counts": counts,
        "confirmed_dates": dates,
        "pending_missing_files": missing_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote verified R4 repair")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--repair-db", required=True)
    parser.add_argument("--repair-images", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    project = Path(args.project_root).resolve()
    repair_db = Path(args.repair_db).resolve()
    repair_images = Path(args.repair_images).resolve()
    formal_db = project / "data" / "orders.db"
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    backup = project / "backups" / "formal" / f"orders_before_r4_{stamp}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)

    precheck = validate_database(repair_db, repair_db.parent)
    if not precheck["passed"]:
        raise RuntimeError(f"R4 repair database failed precheck: {precheck}")
    backup_sqlite(formal_db, backup)
    backup_sha = sha256_file(backup)

    destination_images = formal_db.parent / "images"
    copied = 0
    for source in repair_images.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(repair_images)
        destination = destination_images / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or sha256_file(destination) != sha256_file(source):
            shutil.copy2(source, destination)
            if sha256_file(destination) != sha256_file(source):
                raise RuntimeError(f"Image copy verification failed: {source}")
            copied += 1

    connection = sqlite3.connect(repair_db)
    try:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO backup_records(
              backup_type,relative_path,sha256,database_size,status,
              created_at,verified_at,notes
            ) VALUES('pre_update',?,?,?,'verified',?,?,?)
            """,
            (
                backup.relative_to(project).as_posix(), backup_sha,
                backup.stat().st_size, timestamp, timestamp,
                "Before R4 repaired database promotion",
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at)
            VALUES('r4_repair_promoted','database',NULL,'r4_offline_repair',?,?)
            """,
            (
                json.dumps(
                    {"backup": backup.relative_to(project).as_posix(), "backup_sha256": backup_sha},
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    temporary = formal_db.with_suffix(".db.r4tmp")
    backup_sqlite(repair_db, temporary)
    os.replace(temporary, formal_db)
    formal_db.with_name(formal_db.name + "-wal").unlink(missing_ok=True)
    formal_db.with_name(formal_db.name + "-shm").unlink(missing_ok=True)
    postcheck = validate_database(formal_db, formal_db.parent)
    if not postcheck["passed"]:
        raise RuntimeError(f"Promoted R4 database failed validation: {postcheck}")
    report = {
        "passed": True,
        "formal_database": str(formal_db),
        "formal_sha256": sha256_file(formal_db),
        "backup": str(backup),
        "backup_sha256": backup_sha,
        "images_copied": copied,
        "precheck": precheck,
        "postcheck": postcheck,
    }
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {'precheck','postcheck'}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
