from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = (
    SCRIPT_ROOT.parent
    if (SCRIPT_ROOT.parent / "src" / "v6_db_web").is_dir()
    else SCRIPT_ROOT
)
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from v6_db_web.database_lock import database_write_lock


SUCCESS_CODES = {"new", "changed", "unchanged"}


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def run_cleanup(database: Path, image_root: Path, retention_days: int, apply: bool) -> int:
    if retention_days < 1:
        raise SystemExit("retention-days 必須至少為 1")
    database = database.resolve()
    image_root = image_root.resolve()
    if not database.is_file() or not image_root.is_dir():
        raise SystemExit("找不到資料庫或圖片根目錄")

    with database_write_lock(database, "seven day image cleanup"):
        return _run_locked(database, image_root, retention_days, apply)


def _run_locked(database: Path, image_root: Path, retention_days: int, apply: bool) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    started = datetime.now().astimezone().isoformat()
    deleted: list[int] = []
    missing: list[int] = []
    review_deleted: list[int] = []
    review_missing: list[int] = []
    protected = failed = 0
    rows = connection.execute(
        """
        SELECT s.id, s.relative_path, s.captured_at, s.created_at,
               s.protected_until_review, s.image_status,
               si.id AS scan_item_id, si.result_code, si.order_no,
               EXISTS(
                 SELECT 1 FROM review_queue rq
                 WHERE rq.status='pending'
                   AND (rq.scan_item_id=si.id OR (
                     rq.order_no IS NOT NULL AND rq.order_no=si.order_no
                   ))
               ) AS has_pending_review
        FROM screenshots s
        LEFT JOIN scan_items si ON si.id=s.scan_item_id
        WHERE s.image_status='available'
        """
    ).fetchall()
    for row in rows:
        if (
            row["protected_until_review"]
            or row["has_pending_review"]
            or row["result_code"] not in SUCCESS_CODES
        ):
            protected += 1
            continue
        raw_date = row["captured_at"] or row["created_at"]
        try:
            captured = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            failed += 1
            continue
        if captured.astimezone(timezone.utc) > cutoff:
            continue
        candidate = image_root / str(row["relative_path"])
        if not inside(candidate, image_root):
            failed += 1
            continue
        if not candidate.exists():
            missing.append(int(row["id"]))
            continue
        if apply:
            candidate.unlink()
        deleted.append(int(row["id"]))

    pending_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(web_pending_images)")
    }
    if {"processed_at", "retained_until", "image_status"}.issubset(pending_columns):
        review_images = connection.execute(
            """
            SELECT wpi.review_queue_id, wpi.relative_path, wpi.retained_until
            FROM web_pending_images wpi
            JOIN review_queue rq ON rq.id=wpi.review_queue_id
            WHERE wpi.image_status='available'
              AND rq.status IN('resolved','excluded')
            """
        ).fetchall()
        now = datetime.now(timezone.utc)
        for row in review_images:
            try:
                retained_until = datetime.fromisoformat(
                    str(row["retained_until"] or "").replace("Z", "+00:00")
                )
                if retained_until.tzinfo is None:
                    retained_until = retained_until.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                protected += 1
                continue
            if retained_until.astimezone(timezone.utc) > now:
                protected += 1
                continue
            candidate = database.parent / str(row["relative_path"])
            if not inside(candidate, database.parent):
                failed += 1
                continue
            review_id = int(row["review_queue_id"])
            if not candidate.exists():
                review_missing.append(review_id)
                continue
            if apply:
                candidate.unlink()
            review_deleted.append(review_id)

    details = {
        "dry_run": not apply,
        "cutoff": cutoff.isoformat(),
        "eligible_count": len(deleted),
        "processed_review_image_count": len(review_deleted),
        "missing_count": len(missing),
        "processed_review_missing_count": len(review_missing),
        "protected_count": protected,
        "failed_count": failed,
    }
    if apply:
        finished = datetime.now().astimezone().isoformat()
        with connection:
            if deleted:
                connection.executemany(
                    "UPDATE screenshots SET image_status='deleted' WHERE id=?",
                    [(item,) for item in deleted],
                )
                connection.executemany(
                    """
                    UPDATE scan_items SET image_status='not_provided'
                    WHERE screenshot_id=?
                    """,
                    [(item,) for item in deleted],
                )
            if missing:
                connection.executemany(
                    "UPDATE screenshots SET image_status='missing' WHERE id=?",
                    [(item,) for item in missing],
                )
            if review_deleted:
                connection.executemany(
                    "UPDATE web_pending_images SET image_status='deleted' WHERE review_queue_id=?",
                    [(item,) for item in review_deleted],
                )
            if review_missing:
                connection.executemany(
                    "UPDATE web_pending_images SET image_status='missing' WHERE review_queue_id=?",
                    [(item,) for item in review_missing],
                )
            connection.execute(
                """
                INSERT INTO image_cleanup_runs(
                  started_at, finished_at, retention_days, deleted_count,
                  missing_count, protected_count, failed_count, status, details_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started,
                    finished,
                    retention_days,
                    len(deleted) + len(review_deleted),
                    len(missing) + len(review_missing),
                    protected,
                    failed,
                    "completed" if failed == 0 else "completed_with_errors",
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_logs(
                  event_type, entity_type, entity_id, actor, details_json, created_at
                ) VALUES('image_retention_cleanup', 'screenshots', NULL,
                         'cleanup_success_images.py', ?, ?)
                """,
                (json.dumps(details, ensure_ascii=False), finished),
            )
    connection.close()
    print(json.dumps(details, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return run_cleanup(
        args.database, args.image_root, args.retention_days, args.apply
    )


if __name__ == "__main__":
    raise SystemExit(main())
