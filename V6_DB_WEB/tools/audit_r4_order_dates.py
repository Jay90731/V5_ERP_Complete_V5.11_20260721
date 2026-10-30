from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from v6_db_web.database_lock import database_write_lock  # noqa: E402
from v6_db_web.db import connect  # noqa: E402
from v6_db_web.ocr_ingest import stage_pending_image  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def image_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matching_image_index(order_numbers: set[str], roots: list[Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    exact_pattern = re.compile(r"GOU[0-9A-Z-]{6,20}", re.IGNORECASE)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.png"):
            matches = {value.upper() for value in exact_pattern.findall(path.stem)}
            for order_no in matches & order_numbers:
                if order_no not in result or path.stem.upper() == order_no:
                    result[order_no] = path
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="R4 full-database GOUYYMM date audit")
    parser.add_argument("--db", required=True)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--report-csv", required=True)
    parser.add_argument("--search-root", action="append", default=[])
    args = parser.parse_args()
    database = Path(args.db).resolve()
    report_json = Path(args.report_json).resolve()
    report_csv = Path(args.report_csv).resolve()

    readonly = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    rows = readonly.execute(
        """
        SELECT id,order_no,order_date,current_version_id
        FROM orders
        WHERE status='confirmed' AND department_code='4001' AND is_voided=0
        ORDER BY order_no
        """
    ).fetchall()
    readonly.close()
    anomalies = []
    for row in rows:
        match = re.fullmatch(r"GOU([0-9]{2})([0-9]{2})[0-9A-Z-]*", row["order_no"])
        if not match:
            continue
        expected = f"20{match.group(1)}-{match.group(2)}"
        if row["order_date"][:7] != expected:
            anomalies.append({**dict(row), "expected_year_month": expected})

    roots = [Path(value).resolve() for value in args.search_root]
    image_index = matching_image_index(
        {row["order_no"] for row in anomalies}, roots
    )
    timestamp = now()
    output_rows = []
    with database_write_lock(database, owner="R4 full date quality audit"):
        connection = connect(database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for anomaly in anomalies:
                order_no = anomaly["order_no"]
                image = image_index.get(order_no)
                has_image = bool(image and image.is_file())
                reason = (
                    "正式訂單訂貨年月與訂單編號GOUYYMM不一致；"
                    f"資料庫日期={anomaly['order_date']}；"
                    f"GOUYYMM={anomaly['expected_year_month']}；"
                    + ("請對照原圖人工確認" if has_image else "缺原圖，需人工查ERP")
                )
                desired_status = "pending" if has_image else "retry_requested"
                existing = connection.execute(
                    """
                    SELECT id,status FROM review_queue
                    WHERE source_key=? OR (order_no=? AND status IN('pending','retry_requested'))
                    ORDER BY CASE WHEN source_key=? THEN 0 ELSE 1 END LIMIT 1
                    """,
                    (f"date_quality:{order_no}", order_no, f"date_quality:{order_no}"),
                ).fetchone()
                payload = {
                    "orderNo": order_no,
                    "orderDate": anomaly["order_date"],
                    "reviewContext": {
                        "selected_date": anomaly["order_date"],
                        "suggested_date": anomaly["expected_year_month"] + "-??",
                        "candidates": [anomaly["order_date"]],
                        "original_database_date": anomaly["order_date"],
                        "query_date_from": None,
                        "query_date_to": None,
                        "order_number_yymm": anomaly["expected_year_month"],
                        "anomaly_reasons": [reason],
                    },
                }
                if existing:
                    review_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE review_queue SET reason=?,processing_note=?,
                          raw_payload_json=?,last_seen_at=?,updated_at=? WHERE id=?
                        """,
                        (reason, reason, json.dumps(payload, ensure_ascii=False), timestamp, timestamp, review_id),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO review_queue(
                          source_key,order_no,scan_item_id,status,reason,processing_note,
                          raw_payload_json,first_seen_at,last_seen_at,resolved_at,created_at,updated_at
                        ) VALUES(?,?,NULL,?,?,?,?,?,?,NULL,?,?)
                        """,
                        (
                            f"date_quality:{order_no}", order_no, desired_status,
                            reason, reason, json.dumps(payload, ensure_ascii=False),
                            timestamp, timestamp, timestamp, timestamp,
                        ),
                    )
                    review_id = int(cursor.lastrowid)
                relative_image = None
                if has_image and image is not None:
                    digest = image_hash(image)
                    relative_image = stage_pending_image(
                        connection, review_id, image, str(image), digest, timestamp
                    )
                connection.execute(
                    """
                    INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at)
                    VALUES('date_quality_flagged','order',?,'r4_offline_audit',?,?)
                    """,
                    (
                        str(anomaly["id"]),
                        json.dumps(
                            {
                                "order_no": order_no,
                                "database_date": anomaly["order_date"],
                                "order_number_yymm": anomaly["expected_year_month"],
                                "review_status": desired_status,
                                "image": relative_image,
                                "automatic_date_change": False,
                            },
                            ensure_ascii=False,
                        ),
                        timestamp,
                    ),
                )
                output_rows.append(
                    {
                        "order_no": order_no,
                        "database_date": anomaly["order_date"],
                        "order_number_yymm": anomaly["expected_year_month"],
                        "status": desired_status,
                        "image_available": has_image,
                        "image_relative_path": relative_image or "",
                        "reason": reason,
                    }
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    report_csv.parent.mkdir(parents=True, exist_ok=True)
    with report_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]) if output_rows else ["order_no"])
        writer.writeheader()
        writer.writerows(output_rows)
    report = {
        "passed": True,
        "checked_official_orders": len(rows),
        "anomaly_count": len(output_rows),
        "with_image": sum(row["image_available"] for row in output_rows),
        "without_image": sum(not row["image_available"] for row in output_rows),
        "automatic_date_changes": 0,
        "rows": output_rows,
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
