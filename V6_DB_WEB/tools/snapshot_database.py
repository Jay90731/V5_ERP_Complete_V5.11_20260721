from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.database_lock import database_write_lock


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if not source.is_file() or source.name.lower() != "orders.db":
        raise SystemExit("來源必須是既有的 orders.db")
    if destination.name.lower() != "orders.db" or source == destination:
        raise SystemExit("封裝快照目的檔必須是另一個 orders.db")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".db.snapshot.tmp")
    temporary.unlink(missing_ok=True)

    with database_write_lock(source, "package database snapshot"):
        source_connection = sqlite3.connect(source, timeout=30)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.execute("PRAGMA busy_timeout=30000")
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()

    check = sqlite3.connect(temporary)
    try:
        quick = check.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = len(check.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        check.close()
    if quick != "ok" or foreign_keys:
        temporary.unlink(missing_ok=True)
        raise SystemExit(
            f"資料庫快照驗證失敗：quick_check={quick}, foreign_keys={foreign_keys}"
        )
    temporary.replace(destination)
    print(
        json.dumps(
            {
                "database": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": file_sha256(destination),
                "quick_check": quick,
                "foreign_key_errors": foreign_keys,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
