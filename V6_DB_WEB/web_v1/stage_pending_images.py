from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_name(value: str, fallback: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in "-_.")
    return cleaned or fallback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    source_root = args.source_root.resolve()
    destination = args.destination.resolve()
    if not database.is_file() or not source_root.is_dir():
        raise SystemExit("找不到網站資料庫或 R6 回傳資料夾")
    destination.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT rq.id review_id, rq.order_no, rq.source_key,
               si.image_name, si.image_relative_path, si.image_hash
        FROM review_queue rq
        LEFT JOIN scan_items si ON si.id=rq.scan_item_id
        WHERE rq.status='pending'
        ORDER BY rq.id
        """
    ).fetchall()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS web_pending_images (
          review_queue_id INTEGER PRIMARY KEY REFERENCES review_queue(id) ON DELETE CASCADE,
          relative_path TEXT NOT NULL UNIQUE,
          sha256 TEXT NOT NULL,
          source_relative_path TEXT,
          copied_at TEXT NOT NULL,
          processed_at TEXT,
          retained_until TEXT,
          image_status TEXT NOT NULL DEFAULT 'available'
            CHECK(image_status IN('available','missing','deleted'))
        );
        """
    )

    copied = reused = missing = 0
    now = datetime.now().astimezone().isoformat()
    for row in rows:
        image_name = str(row["image_name"] or "")
        relative = str(row["image_relative_path"] or "").replace("\\", "/")
        candidates: list[Path] = []
        if relative:
            candidates.append(source_root / relative)
        if image_name:
            candidates.extend((source_root / "runs").rglob(image_name))
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            missing += 1
            continue
        digest = sha256(source)
        order_label = str(row["order_no"] or row["source_key"] or row["review_id"])
        filename = safe_name(
            f"{row['review_id']}_{order_label}_{source.name}",
            f"review_{row['review_id']}.png",
        )
        target = destination / filename
        if target.exists() and sha256(target) == digest:
            reused += 1
        else:
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(source, temporary)
            if sha256(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"待確認圖片複製驗證失敗：{source}")
            os.replace(temporary, target)
            copied += 1
        relative_target = target.relative_to(database.parent).as_posix()
        connection.execute(
            """
            INSERT INTO web_pending_images(
              review_queue_id, relative_path, sha256, source_relative_path, copied_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(review_queue_id) DO UPDATE SET
              relative_path=excluded.relative_path,
              sha256=excluded.sha256,
              source_relative_path=excluded.source_relative_path,
              copied_at=excluded.copied_at,
              image_status='available'
            """,
            (int(row["review_id"]), relative_target, digest, relative, now),
        )
    with connection:
        connection.execute(
            """
            UPDATE screenshots SET image_status='archived'
            WHERE image_status='available'
              AND id IN (
                SELECT s.id FROM screenshots s
                LEFT JOIN scan_items si ON si.id=s.scan_item_id
                WHERE COALESCE(si.status,'') <> 'pending_review'
                  AND COALESCE(si.result_code,'') <> 'pending'
                  AND NOT EXISTS(
                    SELECT 1 FROM review_queue rq
                    WHERE rq.status='pending'
                      AND (rq.scan_item_id=si.id OR (
                        rq.order_no IS NOT NULL AND rq.order_no=si.order_no
                      ))
                  )
              )
            """
        )
        connection.execute(
            """
            UPDATE scan_items SET image_status='not_provided'
            WHERE image_status='available'
              AND status <> 'pending_review' AND result_code <> 'pending'
              AND NOT EXISTS(
                SELECT 1 FROM review_queue rq
                WHERE rq.status='pending'
                  AND (rq.scan_item_id=scan_items.id OR (
                    rq.order_no IS NOT NULL AND rq.order_no=scan_items.order_no
                  ))
              )
            """
        )
        details = {
            "pending_reviews": len(rows),
            "copied": copied,
            "reused": reused,
            "missing": missing,
            "successful_images": "archived_after_verified_backup",
        }
        connection.execute(
            """
            INSERT INTO audit_logs(
              event_type, entity_type, entity_id, actor, details_json, created_at
            ) VALUES('web_v1_image_staging', 'screenshots', NULL,
                     'stage_pending_images.py', ?, ?)
            """,
            (json.dumps(details, ensure_ascii=False), now),
        )
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    protected_count = int(connection.execute("SELECT COUNT(*) FROM web_pending_images").fetchone()[0])
    connection.close()
    report = {
        "status": "passed" if not missing and quick == "ok" and not foreign_keys else "warning",
        "pending_reviews": len(rows),
        "protected_images": protected_count,
        "copied": copied,
        "reused": reused,
        "missing": missing,
        "quick_check": quick,
        "foreign_key_errors": foreign_keys,
        "destination": str(destination),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
