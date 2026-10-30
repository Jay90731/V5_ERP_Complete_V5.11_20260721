from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import content_hash, item_hash, normalize_order, normalize_order_no
from .store_rules import store_id_for_order


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class ImportStats:
    source_order_count: int = 0
    source_version_count: int = 0
    source_detail_rows: int = 0
    source_multi_item_orders: int = 0
    inserted_orders: int = 0
    existing_orders: int = 0
    inserted_versions: int = 0
    existing_versions: int = 0
    inserted_items: int = 0
    pending_seen: int = 0
    pending_inserted: int = 0
    pending_updated: int = 0
    pending_scan_items: int = 0
    scan_job_id: int | None = None
    source_path: str = ""
    imported_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def resolve_v5_state(source: str | Path) -> Path:
    root = Path(source).resolve()
    candidates = [
        root / "order_incremental_engine" / "state",
        root / "state",
        root,
    ]
    for candidate in candidates:
        if (candidate / "order_history.json").exists():
            return candidate
    raise FileNotFoundError(
        f"Cannot find order_history.json under V5 source: {root}"
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def totals(order: dict[str, Any]) -> tuple[int, int, int]:
    return (
        sum(item["quantity"] for item in order["details"]),
        sum(item["total_amount"] for item in order["details"]),
        sum(item["discount_amount"] for item in order["details"]),
    )


def ensure_product(
    connection: sqlite3.Connection,
    gift_code: str,
    timestamp: str,
    source: str = "V5.10 JSON",
) -> int:
    connection.execute(
        """
        INSERT INTO products(gift_code, product_name, active, source, created_at, updated_at)
        VALUES (?, NULL, 1, ?, ?, ?)
        ON CONFLICT(gift_code) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (gift_code, source, timestamp, timestamp),
    )
    row = connection.execute(
        "SELECT id FROM products WHERE gift_code = ?", (gift_code,)
    ).fetchone()
    return int(row["id"])


def insert_version(
    connection: sqlite3.Connection,
    order_id: int,
    order: dict[str, Any],
    source_payload: dict[str, Any],
    source_version_id: str | None,
    effective_at: str,
    timestamp: str,
    source_system: str = "V5.10 JSON",
) -> tuple[int, bool, int]:
    digest = content_hash(order)
    existing = connection.execute(
        "SELECT id FROM order_versions WHERE order_id = ? AND content_hash = ?",
        (order_id, digest),
    ).fetchone()
    if existing:
        return int(existing["id"]), False, 0

    next_version = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS value FROM order_versions WHERE order_id = ?",
        (order_id,),
    ).fetchone()["value"]
    total_quantity, total_amount, total_discount = totals(order)
    cursor = connection.execute(
        """
        INSERT INTO order_versions(
            order_id, version_no, content_hash, order_date, category,
            department_code, is_voided, detail_count, total_quantity,
            total_list_amount, total_discount_amount, source_system,
            source_version_id, source_payload_json, effective_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            next_version,
            digest,
            order["order_date"],
            order["category"],
            order["department_code"],
            int(order["is_voided"]),
            len(order["details"]),
            total_quantity,
            total_amount,
            total_discount,
            source_system,
            source_version_id,
            json_text(source_payload),
            effective_at,
            timestamp,
        ),
    )
    version_id = int(cursor.lastrowid)
    for line_no, item in enumerate(order["details"], start=1):
        product_id = ensure_product(
            connection, item["gift_code"], timestamp, source=source_system
        )
        connection.execute(
            """
            INSERT INTO order_items(
                order_version_id, line_no, gift_code, product_id, list_price,
                quantity, total_amount, discount_amount, item_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                line_no,
                item["gift_code"],
                product_id,
                item["list_price"],
                item["quantity"],
                item["total_amount"],
                item["discount_amount"],
                item_hash(item),
                timestamp,
            ),
        )
    return version_id, True, len(order["details"])


def ensure_order(
    connection: sqlite3.Connection,
    order: dict[str, Any],
    first_seen_at: str,
    last_seen_at: str,
    timestamp: str,
) -> tuple[int, bool]:
    row = connection.execute(
        "SELECT id FROM orders WHERE order_no = ?", (order["order_no"],)
    ).fetchone()
    if row:
        return int(row["id"]), False
    cursor = connection.execute(
        """
        INSERT INTO orders(
            order_no, current_version_id, current_content_hash, order_date,
            category, department_code, store_id, is_voided, status,
            first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, 'confirmed', ?, ?, ?, ?)
        """,
        (
            order["order_no"],
            content_hash(order),
            order["order_date"],
            order["category"],
            order["department_code"],
            int(order["is_voided"]),
            first_seen_at,
            last_seen_at,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid), True


def import_history_order(
    connection: sqlite3.Connection,
    order_no: str,
    entry: dict[str, Any],
    stats: ImportStats,
) -> None:
    timestamp = stats.imported_at
    current_payload = entry["current_version"]
    current_order = normalize_order(current_payload)
    if current_order["order_no"] != normalize_order_no(order_no):
        raise ValueError(
            f"History key/order_no mismatch: {order_no} != {current_order['order_no']}"
        )

    first_seen_at = entry.get("created_at") or timestamp
    last_seen_at = entry.get("updated_at") or timestamp
    order_id, inserted = ensure_order(
        connection, current_order, first_seen_at, last_seen_at, timestamp
    )
    if inserted:
        stats.inserted_orders += 1
    else:
        stats.existing_orders += 1

    source_versions = list(entry.get("versions") or [])
    for source_version in source_versions:
        payload = source_version.get("order")
        if not payload:
            continue
        normalized = normalize_order(payload)
        _, created, item_count = insert_version(
            connection,
            order_id,
            normalized,
            payload,
            source_version.get("version_id"),
            source_version.get("effective_at") or first_seen_at,
            timestamp,
        )
        if created:
            stats.inserted_versions += 1
            stats.inserted_items += item_count
        else:
            stats.existing_versions += 1

    current_version_id, created, item_count = insert_version(
        connection,
        order_id,
        current_order,
        current_payload,
        entry.get("current_version_id"),
        last_seen_at,
        timestamp,
    )
    if created:
        stats.inserted_versions += 1
        stats.inserted_items += item_count
    elif not source_versions:
        stats.existing_versions += 1

    store_id = store_id_for_order(connection, current_order, timestamp)

    connection.execute(
        """
        UPDATE orders
        SET current_version_id = ?, current_content_hash = ?, order_date = ?,
            category = ?, department_code = ?, store_id = ?, is_voided = ?,
            last_seen_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            current_version_id,
            content_hash(current_order),
            current_order["order_date"],
            current_order["category"],
            current_order["department_code"],
            store_id,
            int(current_order["is_voided"]),
            last_seen_at,
            timestamp,
            order_id,
        ),
    )


def pending_source_key(record: dict[str, Any]) -> tuple[str, str | None]:
    order_no = normalize_order_no(record.get("order_no"))
    if not order_no and isinstance(record.get("order"), dict):
        order_no = normalize_order_no(record["order"].get("order_no"))
    images = record.get("source_images") or []
    image_name = str(images[0]) if images else ""
    identity = order_no or image_name
    if not identity:
        identity = hashlib.sha256(json_text(record).encode("utf-8")).hexdigest()
    return f"v5:{identity}", order_no or None


def upsert_pending(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    stats: ImportStats,
    scan_job_id: int,
) -> None:
    source_key, order_no = pending_source_key(record)
    reason = str(record.get("reason") or "V5.10 待確認")
    timestamp = stats.imported_at
    cursor = connection.execute(
        """
        INSERT INTO scan_items(
            scan_job_id, source_key, image_name, temporary_key, order_no,
            order_id, order_version_id, status, result_code, screenshot_id,
            image_hash, image_relative_path, image_status,
            raw_ocr_payload_json, validation_json, reason, started_at,
            finished_at, processed_at, created_at
        ) VALUES (?, ?, ?, NULL, ?, NULL, NULL, 'pending_review', 'pending',
                  NULL, NULL, NULL, 'not_provided', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_job_id,
            source_key,
            Path(str((record.get("source_images") or [""])[0])).name or None,
            order_no,
            json_text(record),
            json_text({"valid": False, "reason": reason}),
            reason,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    scan_item_id = int(cursor.lastrowid)
    stats.pending_scan_items += 1
    existing = connection.execute(
        "SELECT id FROM review_queue WHERE source_key = ?", (source_key,)
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE review_queue
            SET order_no = COALESCE(?, order_no), reason = ?,
                scan_item_id = ?, raw_payload_json = ?,
                last_seen_at = ?, updated_at = ?
            WHERE source_key = ?
            """,
            (
                order_no,
                reason,
                scan_item_id,
                json_text(record),
                timestamp,
                timestamp,
                source_key,
            ),
        )
        stats.pending_updated += 1
    else:
        connection.execute(
            """
            INSERT INTO review_queue(
                source_key, order_no, scan_item_id, status, reason,
                processing_note, raw_payload_json, first_seen_at, last_seen_at,
                resolved_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, NULL, ?, ?, ?, NULL, ?, ?)
            """,
            (
                source_key,
                order_no,
                scan_item_id,
                reason,
                json_text(record),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        stats.pending_inserted += 1
    stats.pending_seen += 1


def import_v5(
    connection: sqlite3.Connection,
    source: str | Path,
) -> ImportStats:
    state_dir = resolve_v5_state(source)
    history_path = state_dir / "order_history.json"
    current_path = state_dir / "current_orders.json"
    history = read_json(history_path)
    current = read_json(current_path) if current_path.exists() else {"orders": []}
    history_orders = history.get("orders")
    if not isinstance(history_orders, dict):
        raise ValueError("V5 order_history.json does not contain an orders object")

    stats = ImportStats(source_path=str(state_dir))
    stats.source_order_count = len(history_orders)
    stats.source_version_count = sum(
        len(entry.get("versions") or []) for entry in history_orders.values()
    )
    for entry in history_orders.values():
        details = entry["current_version"].get("details") or []
        stats.source_detail_rows += len(details)
        if len(details) > 1:
            stats.source_multi_item_orders += 1

    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """
            INSERT INTO scan_jobs(
                job_type, query_start_date, query_end_date, status, started_at,
                finished_at, source_name, summary_json, error_message,
                created_at, updated_at
            ) VALUES ('v5_import', NULL, NULL, 'running', ?, NULL, ?, NULL, NULL, ?, ?)
            """,
            (
                stats.imported_at,
                str(state_dir),
                stats.imported_at,
                stats.imported_at,
            ),
        )
        stats.scan_job_id = int(cursor.lastrowid)

        for order_no, entry in history_orders.items():
            import_history_order(connection, order_no, entry, stats)

        for record in current.get("orders") or []:
            if record.get("status") == "待確認":
                upsert_pending(
                    connection, record, stats, int(stats.scan_job_id)
                )

        summary = stats.as_dict()
        connection.execute(
            """
            UPDATE scan_jobs
            SET status = 'completed', finished_at = ?, summary_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                stats.imported_at,
                json_text(summary),
                stats.imported_at,
                stats.scan_job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_logs(
                event_type, entity_type, entity_id, actor, details_json, created_at
            ) VALUES ('v5_import', 'scan_job', ?, 'system', ?, ?)
            """,
            (
                str(stats.scan_job_id),
                json_text(summary),
                stats.imported_at,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return stats
