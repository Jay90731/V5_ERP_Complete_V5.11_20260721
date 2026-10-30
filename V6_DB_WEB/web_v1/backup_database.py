from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    script_root = Path(__file__).resolve().parent
    root = script_root.parent if (script_root.parent / "src" / "v6_db_web").is_dir() else script_root
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=root / "data" / "orders.db")
    parser.add_argument("--destination", type=Path, default=root / "backups" / "formal")
    args = parser.parse_args()
    source = args.database.resolve()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = destination / f"orders_{stamp}.db"
    temporary = output.with_suffix(".db.tmp")
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    target_connection = sqlite3.connect(temporary)
    try:
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
        raise SystemExit("備份完整性驗證失敗")
    os.replace(temporary, output)
    report = {
        "status": "passed",
        "database": str(output),
        "sha256": sha256(output),
        "bytes": output.stat().st_size,
        "quick_check": quick,
        "foreign_key_errors": foreign_keys,
    }
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
