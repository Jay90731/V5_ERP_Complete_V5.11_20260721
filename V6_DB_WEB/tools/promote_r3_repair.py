from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.database_lock import database_write_lock


EXPECTED_DATES = {
    "GOU26080424": "2026-08-21",
    "GOU26080425": "2026-08-22",
    "GOU26080426": "2026-08-22",
    "GOU26080427": "2026-08-23",
}
PENDING_ORDERS = ("GOU26080356", "GOU26080390")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        dates = {
            str(row["order_no"]): str(row["order_date"])
            for row in connection.execute(
                "SELECT order_no,order_date FROM orders WHERE order_no IN (?,?,?,?)",
                tuple(EXPECTED_DATES),
            )
        }
        missing_pending_images = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM review_queue rq
                WHERE rq.status='pending' AND NOT EXISTS(
                  SELECT 1 FROM web_pending_images wpi
                  WHERE wpi.review_queue_id=rq.id AND wpi.image_status='available'
                )
                """
            ).fetchone()[0]
        )
        pending_paths = [
            str(row["relative_path"])
            for row in connection.execute(
                """
                SELECT wpi.relative_path FROM web_pending_images wpi
                JOIN review_queue rq ON rq.id=wpi.review_queue_id
                WHERE rq.order_no IN (?,?) AND rq.status='pending'
                  AND wpi.image_status='available'
                """,
                PENDING_ORDERS,
            )
        ]
        counts = {
            "formal_orders_sr": int(
                connection.execute("SELECT COUNT(*) FROM official_orders").fetchone()[0]
            ),
            "pending": int(
                connection.execute(
                    "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
                ).fetchone()[0]
            ),
            "duplicate_orders": int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"
                ).fetchone()[0]
            ),
            "duplicate_versions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT order_id,content_hash FROM order_versions GROUP BY order_id,content_hash HAVING COUNT(*)>1)"
                ).fetchone()[0]
            ),
        }
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    data_root = path.parent
    portable_images = all((data_root / value).is_file() for value in pending_paths)
    checks = {
        "quick_check": quick == "ok",
        "foreign_keys": foreign_keys == 0,
        "formal_count": counts["formal_orders_sr"] == 4523,
        "pending_count": counts["pending"] == 482,
        "no_duplicates": counts["duplicate_orders"] == 0
        and counts["duplicate_versions"] == 0,
        "four_dates_correct": dates == EXPECTED_DATES,
        "pending_images_complete": missing_pending_images == 0,
        "two_return_pending_images_portable": len(pending_paths) == 2
        and portable_images,
    }
    return {
        "database": str(path),
        "sha256": sha256(path),
        "quick_check": quick,
        "foreign_key_errors": foreign_keys,
        "counts": counts,
        "dates": dates,
        "pending_paths": pending_paths,
        "pending_without_available_image": missing_pending_images,
        "checks": checks,
        "passed": all(checks.values()),
    }


def sqlite_backup(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.execute("PRAGMA busy_timeout=30000")
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    os.replace(temporary, destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repaired-db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    repaired = args.repaired_db.resolve()
    formal = project_root / "data" / "orders.db"
    if not repaired.is_file() or not formal.is_file() or repaired == formal:
        raise SystemExit("R3 repaired and formal databases must be different existing files")
    repaired_validation = validate(repaired)
    if not repaired_validation["passed"]:
        raise SystemExit(f"R3 repaired database validation failed: {repaired_validation}")

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    backup = project_root / "backups" / "formal" / f"orders_pre_r3_{timestamp}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    promoted_temp = formal.with_suffix(".db.r3.tmp")
    promoted_temp.unlink(missing_ok=True)

    with database_write_lock(formal, "promote verified R3 offline repair"):
        checkpoint_connection = sqlite3.connect(formal, timeout=30)
        try:
            checkpoint_connection.execute("PRAGMA busy_timeout=30000")
            checkpoint_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            checkpoint_connection.close()
        sqlite_backup(formal, backup)
        backup_validation = validate(backup)
        if backup_validation["quick_check"] != "ok" or backup_validation["foreign_key_errors"]:
            backup.unlink(missing_ok=True)
            raise RuntimeError("Pre-R3 backup validation failed")

        source_images = repaired.parent / "images" / "pending_review"
        target_images = formal.parent / "images" / "pending_review"
        target_images.mkdir(parents=True, exist_ok=True)
        copied_images = 0
        for source in source_images.glob("*"):
            if not source.is_file():
                continue
            target = target_images / source.name
            if target.is_file() and sha256(target) == sha256(source):
                continue
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(source, temporary)
            if sha256(temporary) != sha256(source):
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"Pending image copy verification failed: {source}")
            os.replace(temporary, target)
            copied_images += 1

        sqlite_backup(repaired, promoted_temp)
        promoted_validation = validate(promoted_temp)
        if not promoted_validation["passed"]:
            promoted_temp.unlink(missing_ok=True)
            raise RuntimeError("Promoted R3 snapshot validation failed")

        os.replace(promoted_temp, formal)
        formal.with_name(formal.name + "-wal").unlink(missing_ok=True)
        formal.with_name(formal.name + "-shm").unlink(missing_ok=True)
        final_connection = sqlite3.connect(formal)
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            relative_backup = backup.relative_to(project_root).as_posix()
            final_connection.execute(
                """
                INSERT INTO backup_records(
                  backup_type,relative_path,sha256,database_size,status,
                  created_at,verified_at,notes
                ) VALUES('pre_update',?,?,?,'verified',?,?,?)
                """,
                (
                    relative_backup,
                    sha256(backup),
                    backup.stat().st_size,
                    now,
                    now,
                    "Before R3 offline repair replay from the 121 returned screenshots",
                ),
            )
            final_connection.execute(
                """
                INSERT INTO audit_logs(
                  event_type,entity_type,entity_id,actor,details_json,created_at
                ) VALUES('r3_offline_repair_promoted','database','orders.db',
                         'r3_repair_tool',?,?)
                """,
                (
                    json.dumps(
                        {
                            "source_backup": "orders_20260825_161053_235715.db",
                            "query_date_from": "2026-08-19",
                            "query_date_to": "2026-08-25",
                            "replayed_images": 121,
                            "copied_pending_images": copied_images,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            final_connection.commit()
            final_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            final_connection.close()

    final_validation = validate(formal)
    report = {
        "status": "passed" if final_validation["passed"] else "failed",
        "promoted_at": datetime.now().astimezone().isoformat(),
        "immutable_return_database": str(repaired),
        "pre_r3_backup": str(backup),
        "pre_r3_backup_sha256": sha256(backup),
        "formal_database": str(formal),
        "repaired_validation": repaired_validation,
        "formal_validation": final_validation,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if final_validation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
