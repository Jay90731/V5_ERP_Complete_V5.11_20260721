from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def main() -> int:
    script_root = Path(__file__).resolve().parent
    root = script_root.parent if (script_root.parent / "src" / "v6_db_web").is_dir() else script_root
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=root / "data" / "orders.db")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "RESTORE":
        raise SystemExit("必須使用 --confirm RESTORE 才能還原")
    backup = args.backup.resolve()
    database = args.database.resolve()
    if not backup.is_file() or backup == database:
        raise SystemExit("備份檔案無效")
    source = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("備份 quick_check 未通過")
        if source.execute("PRAGMA foreign_key_check").fetchall():
            raise SystemExit("備份有外鍵錯誤")
        temporary = database.with_suffix(".db.restore.tmp")
        temporary.unlink(missing_ok=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.replace(temporary, database)
    print(f"RESTORE_OK database={database} source={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
