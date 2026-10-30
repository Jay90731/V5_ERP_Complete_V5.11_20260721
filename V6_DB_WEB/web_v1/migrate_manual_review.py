from __future__ import annotations

import argparse
import json
from pathlib import Path

from wedding_web.write_service import ManualReviewService


def main() -> int:
    script_root = Path(__file__).resolve().parent
    root = script_root.parent if (script_root.parent / "src" / "v6_db_web").is_dir() else script_root
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=root / "data" / "orders.db"
    )
    args = parser.parse_args()
    database = args.database.resolve()
    if database != (root / "data" / "orders.db").resolve():
        raise SystemExit("人工校正 migration 只允許唯一正式 data/orders.db")
    if not database.is_file():
        raise SystemExit(f"找不到網站資料庫：{database}")
    result = ManualReviewService(database).ensure_ready()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
