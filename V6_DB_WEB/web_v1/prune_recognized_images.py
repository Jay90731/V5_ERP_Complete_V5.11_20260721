from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--verified-backup-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    package_root = args.package_root.resolve()
    backup_root = args.verified_backup_root.resolve()
    database = package_root / "data" / "orders.db"
    backup_database = backup_root / "data" / "orders.db"
    checkpoint = package_root / "history_scan" / "history_checkpoint.json"
    backup_checkpoint = backup_root / "history_scan" / "history_checkpoint.json"
    for required in (database, backup_database, checkpoint, backup_checkpoint):
        if not required.is_file():
            raise SystemExit(f"找不到必要檔案：{required}")
    if package_root == backup_root or is_inside(backup_root, package_root):
        raise SystemExit("備份資料夾不可位於要清理的原回傳包內")
    if sha256(database) != sha256(backup_database):
        raise SystemExit("備份 orders.db 與原回傳資料不一致，拒絕刪圖")
    if sha256(checkpoint) != sha256(backup_checkpoint):
        raise SystemExit("備份 checkpoint 與原回傳資料不一致，拒絕刪圖")

    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    pending_rows = connection.execute(
        """
        SELECT DISTINCT si.image_name, si.image_relative_path, si.order_no,
                        si.image_hash
        FROM scan_items si
        LEFT JOIN review_queue rq ON rq.scan_item_id=si.id
          OR (rq.order_no IS NOT NULL AND rq.order_no=si.order_no)
        WHERE si.status='pending_review'
           OR si.result_code='pending'
           OR rq.status='pending'
        """
    ).fetchall()
    pending_review_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
        ).fetchone()[0]
    )
    connection.close()

    pending_names = {str(row["image_name"] or "").casefold() for row in pending_rows}
    pending_stems = {
        Path(str(row["image_name"] or "")).stem.casefold() for row in pending_rows
    }
    pending_orders = {str(row["order_no"] or "").casefold() for row in pending_rows}
    pending_relative = {
        str(row["image_relative_path"] or "").replace("\\", "/").casefold()
        for row in pending_rows
    }
    pending_hashes = {str(row["image_hash"] or "").upper() for row in pending_rows}
    pending_names.discard("")
    pending_stems.discard("")
    pending_orders.discard("")
    pending_relative.discard("")
    pending_hashes.discard("")

    candidates: list[Path] = []
    for relative_root in ("runs", "data/images"):
        root = package_root / relative_root
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*.png") if path.is_file())

    protected: list[Path] = []
    deletable: list[Path] = []
    hash_checks = 0
    for image in candidates:
        if not is_inside(image, package_root):
            raise SystemExit(f"圖片超出回傳資料夾，拒絕處理：{image}")
        relative = image.relative_to(package_root).as_posix().casefold()
        name = image.name.casefold()
        stem = image.stem.casefold()
        preserve = (
            relative in pending_relative
            or name in pending_names
            or stem in pending_stems
            or stem in pending_orders
        )
        if not preserve and pending_hashes:
            image_hash = sha256(image)
            hash_checks += 1
            preserve = image_hash in pending_hashes
        (protected if preserve else deletable).append(image)

    deleted_bytes = sum(path.stat().st_size for path in deletable)
    if args.apply:
        for image in deletable:
            image.unlink()

    result = {
        "status": "applied" if args.apply else "dry_run",
        "processed_at": datetime.now().astimezone().isoformat(),
        "package_root": str(package_root),
        "verified_backup_root": str(backup_root),
        "orders_db_sha256": sha256(database),
        "checkpoint_sha256": sha256(checkpoint),
        "pending_review_rows": pending_review_count,
        "pending_scan_image_records": len(pending_rows),
        "png_candidates": len(candidates),
        "protected_png": len(protected),
        "deleted_png": len(deletable) if args.apply else 0,
        "would_delete_png": len(deletable),
        "deleted_bytes": deleted_bytes if args.apply else 0,
        "would_delete_bytes": deleted_bytes,
        "hash_checks": hash_checks,
        "database_modified": False,
        "backup_modified": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
