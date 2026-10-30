from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import normalize_order_no
from .importer import json_text
from .ocr_ingest import (
    ORDER_NUMBER_PATTERN,
    prepare_ocr_record,
    validate_ocr_record,
    write_order,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ReviewActionSummary:
    total: int = 0
    corrected: int = 0
    excluded: int = 0
    unchanged: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def _review_row(
    connection: sqlite3.Connection,
    action: dict[str, Any],
) -> sqlite3.Row | None:
    review_id = action.get("reviewId")
    if review_id is not None:
        return connection.execute(
            "SELECT * FROM review_queue WHERE id = ?", (int(review_id),)
        ).fetchone()
    source_key = str(action.get("sourceKey") or "").strip()
    if source_key:
        row = connection.execute(
            "SELECT * FROM review_queue WHERE source_key = ?", (source_key,)
        ).fetchone()
        if row is not None:
            return row
    order_no = normalize_order_no(action.get("orderNo"))
    if order_no:
        return connection.execute(
            """
            SELECT * FROM review_queue
            WHERE order_no = ?
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'retry_requested' THEN 1 ELSE 2 END,
                     id DESC
            LIMIT 1
            """,
            (order_no,),
        ).fetchone()
    return None


def _latest_scan_item(
    connection: sqlite3.Connection,
    order_no: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM scan_items
        WHERE order_no = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (order_no,),
    ).fetchone()


def _ensure_excluded_review(
    connection: sqlite3.Connection,
    action: dict[str, Any],
    order_no: str,
    timestamp: str,
) -> int:
    row = _review_row(connection, action)
    scan_item = _latest_scan_item(connection, order_no)
    note = str(action.get("note") or "人工確認不納入").strip()
    raw_payload = (
        str(row["raw_payload_json"])
        if row is not None
        else str(scan_item["raw_ocr_payload_json"])
        if scan_item is not None
        else json_text({"orderNo": order_no})
    )
    scan_item_id = (
        int(row["scan_item_id"])
        if row is not None and row["scan_item_id"] is not None
        else int(scan_item["id"])
        if scan_item is not None
        else None
    )
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO review_queue(
                source_key, order_no, scan_item_id, status, reason,
                processing_note, raw_payload_json, first_seen_at, last_seen_at,
                resolved_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'excluded', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"manual:{order_no}", order_no, scan_item_id, note, note,
                raw_payload, timestamp, timestamp, timestamp, timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)
    connection.execute(
        """
        UPDATE review_queue
        SET order_no = ?, scan_item_id = COALESCE(scan_item_id, ?),
            status = 'excluded', reason = ?, processing_note = ?,
            last_seen_at = ?, resolved_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            order_no, scan_item_id, note, note, timestamp, timestamp,
            timestamp, int(row["id"]),
        ),
    )
    return int(row["id"])


def _exclude_order(
    connection: sqlite3.Connection,
    action: dict[str, Any],
    timestamp: str,
) -> str:
    order_no = normalize_order_no(action.get("orderNo"))
    if not ORDER_NUMBER_PATTERN.fullmatch(order_no):
        raise ValueError("人工排除需要有效的 GOU 訂單編號")
    review_id = _ensure_excluded_review(
        connection, action, order_no, timestamp
    )
    connection.execute(
        """
        UPDATE orders
        SET status = 'archived', store_id = NULL, updated_at = ?
        WHERE order_no = ?
        """,
        (timestamp, order_no),
    )
    connection.execute(
        """
        UPDATE screenshots
        SET protected_until_review = 0
        WHERE scan_item_id = (
            SELECT scan_item_id FROM review_queue WHERE id = ?
        )
        """,
        (review_id,),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
            event_type, entity_type, entity_id, actor, details_json, created_at
        ) VALUES ('manual_review_excluded', 'order', ?, 'manual_review', ?, ?)
        """,
        (
            order_no,
            json_text({"review_id": review_id, "note": action.get("note")}),
            timestamp,
        ),
    )
    return "excluded"


def _promote_screenshot(
    connection: sqlite3.Connection,
    review_id: int,
    version_id: int,
) -> None:
    screenshot = connection.execute(
        """
        SELECT s.id
        FROM screenshots s
        JOIN review_queue r ON r.scan_item_id = s.scan_item_id
        WHERE r.id = ?
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (review_id,),
    ).fetchone()
    if screenshot is None:
        return
    existing = connection.execute(
        """
        SELECT id FROM screenshots
        WHERE order_version_id = ? AND image_role = 'official'
        """,
        (version_id,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            UPDATE screenshots
            SET order_version_id = ?, image_role = 'official',
                protected_until_review = 0
            WHERE id = ?
            """,
            (version_id, int(screenshot["id"])),
        )
    else:
        connection.execute(
            """
            UPDATE screenshots SET protected_until_review = 0 WHERE id = ?
            """,
            (int(screenshot["id"]),),
        )


def _correct_order(
    connection: sqlite3.Connection,
    action: dict[str, Any],
    timestamp: str,
) -> str:
    record = action.get("correctedOrder")
    if not isinstance(record, dict):
        raise ValueError("人工修正缺少 correctedOrder")
    corrected = dict(record)
    corrected["statusLevel"] = "success"
    corrected["errorReason"] = ""
    prepared, warnings = prepare_ocr_record(corrected)
    validation = validate_ocr_record(prepared, warnings)
    if validation.order is None or validation.result_code != "unchanged":
        raise ValueError(f"人工修正仍未通過驗證：{validation.reason}")
    if validation.order["department_code"] != "4001":
        raise ValueError("人工修正寫回只接受正式部門 4001 訂單")

    order_no = validation.order["order_no"]
    row = _review_row(connection, action)
    if row is None:
        row = _review_row(connection, {"orderNo": order_no})
    if row is None:
        raise ValueError(f"找不到待確認紀錄：{order_no}")
    order_id, version_id, result = write_order(
        connection, corrected, validation.order, timestamp
    )
    connection.execute(
        """
        UPDATE review_queue
        SET order_no = ?, status = 'resolved', processing_note = ?,
            raw_payload_json = ?, last_seen_at = ?, resolved_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            order_no,
            str(action.get("note") or "人工修正後驗證通過"),
            json_text(corrected), timestamp, timestamp, timestamp,
            int(row["id"]),
        ),
    )
    _promote_screenshot(connection, int(row["id"]), version_id)
    connection.execute(
        """
        INSERT INTO audit_logs(
            event_type, entity_type, entity_id, actor, details_json, created_at
        ) VALUES ('manual_review_corrected', 'order', ?, 'manual_review', ?, ?)
        """,
        (
            order_no,
            json_text(
                {
                    "review_id": int(row["id"]),
                    "order_id": order_id,
                    "version_id": version_id,
                    "write_result": result,
                }
            ),
            timestamp,
        ),
    )
    return result


def apply_review_actions(
    connection: sqlite3.Connection,
    source: str | Path | dict[str, Any] | list[dict[str, Any]],
) -> ReviewActionSummary:
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    else:
        payload = source
    actions = payload.get("actions") if isinstance(payload, dict) else payload
    if not isinstance(actions, list):
        raise ValueError("人工處理檔必須包含 actions 陣列")

    summary = ReviewActionSummary()
    for action in actions:
        summary.total += 1
        if not isinstance(action, dict):
            summary.failed += 1
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = utc_now()
            action_type = str(action.get("action") or "").strip().lower()
            if action_type == "exclude":
                _exclude_order(connection, action, timestamp)
                summary.excluded += 1
            elif action_type == "correct":
                result = _correct_order(connection, action, timestamp)
                summary.corrected += 1
                if result == "unchanged":
                    summary.unchanged += 1
            else:
                raise ValueError(f"不支援的人工處理動作：{action_type}")
            connection.commit()
        except Exception:
            connection.rollback()
            summary.failed += 1
            raise
    return summary
