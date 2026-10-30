from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from v6_db_web.database_lock import database_write_lock  # noqa: E402
from v6_db_web.db import checkpoint, connect  # noqa: E402
from v6_db_web.migrations import apply_migrations  # noqa: E402
from v6_db_web.ocr_ingest import replay_ocr_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline R4 six-month repair replay")
    parser.add_argument("--db", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    database = Path(args.db).resolve()
    report = Path(args.report).resolve()
    with database_write_lock(database, owner="R4 offline repair replay"):
        connection = connect(database)
        try:
            apply_migrations(connection, ROOT / "migrations")
            summary = replay_ocr_json(
                connection,
                args.input,
                args.date_from,
                args.date_to,
                job_type="monthly_6_months",
            )
            checkpoint(connection)
        finally:
            connection.close()
    output = summary.as_dict()
    output["database"] = str(database)
    output["quick_check"] = sqlite3.connect(database).execute(
        "PRAGMA quick_check"
    ).fetchone()[0]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
