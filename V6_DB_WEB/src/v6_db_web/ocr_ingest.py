from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .canonical import (
    content_hash,
    normalize_date,
    normalize_integer,
    normalize_order,
    normalize_order_no,
)
from .importer import ensure_order, insert_version, json_text
from .store_rules import store_id_for_order


TARGET_DEPARTMENT = "4001"
ORDER_NUMBER_PATTERN = re.compile(r"^GOU[0-9A-Z-]{6,20}$")
RESULT_CODES = {
    "new",
    "unchanged",
    "changed",
    "pending",
    "non_target_department",
    "voided",
    "failed",
}


def prepare_ocr_record(
    record: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    prepared = copy.deepcopy(record)
    warnings: list[str] = []

    image_name = str(prepared.get("imageName") or "").strip()
    image_order_no = normalize_order_no(Path(image_name).stem)
    raw_order_no = normalize_order_no(prepared.get("orderNo"))
    if ORDER_NUMBER_PATTERN.fullmatch(image_order_no):
        if raw_order_no != image_order_no:
            warnings.append(
                f"訂單編號依圖片檔名校正：{raw_order_no or '(空白)'} -> "
                f"{image_order_no}"
            )
        prepared["orderNo"] = image_order_no

    order_no = normalize_order_no(prepared.get("orderNo"))
    match = re.fullmatch(r"GOU(\d{2})(\d{2})[0-9A-Z-]*", order_no)
    if match:
        try:
            normalized_date = normalize_date(prepared.get("orderDate"))
            parsed_date = datetime.strptime(normalized_date, "%Y-%m-%d").date()
            expected_year = 2000 + int(match.group(1))
            expected_month = int(match.group(2))
            if (
                parsed_date.year != expected_year
                or parsed_date.month != expected_month
            ):
                warnings.append(
                    "訂貨日期與訂單編號年月不同；保留 ERP 畫面的訂貨日期："
                    f"{normalized_date}"
                )
        except ValueError:
            pass

    return prepared, warnings


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ValidationResult:
    result_code: str
    reason: str
    order: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)
    review_context: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.result_code == "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_code": self.result_code,
            "reason": self.reason,
            "valid_order": self.order is not None,
            "warnings": self.warnings,
            "review_context": self.review_context,
        }


@dataclass
class ReplaySummary:
    scan_job_id: int
    job_type: str
    date_from: str
    date_to: str
    source_file: str
    started_at: str
    finished_at: str = ""
    status: str = "running"
    total_count: int = 0
    success_count: int = 0
    unchanged_count: int = 0
    changed_count: int = 0
    pending_count: int = 0
    excluded_count: int = 0
    failed_count: int = 0

    def increment(self, result_code: str) -> None:
        self.total_count += 1
        if result_code == "new":
            self.success_count += 1
        elif result_code == "unchanged":
            self.unchanged_count += 1
        elif result_code == "changed":
            self.changed_count += 1
        elif result_code == "pending":
            self.pending_count += 1
        elif result_code in {"non_target_department", "voided"}:
            self.excluded_count += 1
        elif result_code == "failed":
            self.failed_count += 1
        else:
            raise ValueError(f"Unknown result code: {result_code}")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def validate_ocr_record(
    record: dict[str, Any],
    preparation_warnings: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    existing_order_date: str | None = None,
) -> ValidationResult:
    warnings = list(preparation_warnings or [])
    status_level = str(record.get("statusLevel") or "").strip().lower()
    error_reason = str(record.get("errorReason") or "").strip()
    if status_level in {"failed", "error"}:
        return ValidationResult(
            "failed", error_reason or "OCR 處理失敗", None, warnings
        )

    order_no = normalize_order_no(record.get("orderNo"))
    if not ORDER_NUMBER_PATTERN.fullmatch(order_no):
        return ValidationResult(
            "pending", "訂單編號辨識失敗", None, warnings
        )

    if record.get("imageIncomplete") is True:
        return ValidationResult("pending", "圖片不完整", None, warnings)

    department = str(record.get("departmentCode") or "").strip()
    if not department:
        return ValidationResult(
            "pending", "部門代號辨識失敗", None, warnings
        )

    evidence = record.get("dateEvidence")
    if not isinstance(evidence, dict):
        evidence = {}
    raw_selected_date = str(
        evidence.get("selectedDate") or record.get("orderDate") or ""
    )
    candidate_values = evidence.get("candidates")
    if not isinstance(candidate_values, list):
        candidate_values = [raw_selected_date] if raw_selected_date else []
    date_context = {
        "selected_date": raw_selected_date or None,
        "suggested_date": (
            evidence.get("suggestedDate") or raw_selected_date or None
        ),
        "candidates": list(
            dict.fromkeys(str(value) for value in candidate_values if value)
        ),
        "dedicated_candidates": evidence.get("dedicatedCandidates") or [],
        "main_region_candidates": evidence.get("mainRegionCandidates") or [],
        "candidate_details": evidence.get("candidateDetails") or [],
        "confidence": evidence.get("confidence") or "legacy_single",
        "original_database_date": existing_order_date,
        "query_date_from": date_from,
        "query_date_to": date_to,
        "order_number_yymm": evidence.get("orderNumberYYMM"),
        "anomaly_reasons": list(evidence.get("anomalyReasons") or []),
    }

    try:
        order = normalize_order(record)
    except ValueError as exc:
        date_context["anomaly_reasons"] = list(
            dict.fromkeys(
                [*date_context["anomaly_reasons"], f"訂貨日期格式無效：{exc}"]
            )
        )
        return ValidationResult(
            "pending", str(exc), None, warnings, date_context
        )

    if not evidence.get("selectedDate"):
        date_context["selected_date"] = order.get("order_date")
        date_context["suggested_date"] = order.get("order_date")
        if not date_context["candidates"] and order.get("order_date"):
            date_context["candidates"] = [order["order_date"]]

    if status_level == "pending":
        return ValidationResult(
            "pending",
            error_reason or "OCR 明確標記 pending",
            None,
            warnings,
            date_context,
        )

    if not order["category"]:
        return ValidationResult(
            "pending", "訂單分類辨識失敗", None, warnings
        )

    if order["is_voided"]:
        return ValidationResult(
            "voided", "作廢訂單，不納入正式統計", order, warnings
        )

    if department == TARGET_DEPARTMENT:
        date_reasons = list(date_context["anomaly_reasons"])
        if date_from is not None and date_to is not None:
            try:
                query_start = datetime.strptime(
                    normalize_date(date_from), "%Y-%m-%d"
                ).date()
                query_end = datetime.strptime(
                    normalize_date(date_to), "%Y-%m-%d"
                ).date()
                order_date = datetime.strptime(
                    order["order_date"], "%Y-%m-%d"
                ).date()
            except ValueError:
                date_reasons.append("訂貨日期無效，無法驗證查詢範圍")
            else:
                if not query_start <= order_date <= query_end:
                    date_reasons.append(
                        "訂貨日期超出本次查詢範圍："
                        f"{order['order_date']} 不在 {query_start.isoformat()} "
                        f"至 {query_end.isoformat()}"
                    )
        match = re.fullmatch(r"GOU([0-9]{2})([0-9]{2})[0-9A-Z-]*", order_no)
        if match:
            expected_month = int(match.group(2))
            if not 1 <= expected_month <= 12:
                date_reasons.append("訂單編號GOUYYMM月份無效")
            elif order["order_date"][:7] != f"20{match.group(1)}-{expected_month:02d}":
                date_reasons.append("訂貨日期年月與訂單編號GOUYYMM不一致")
        if (
            existing_order_date
            and existing_order_date != order["order_date"]
            and date_context["confidence"] != "trusted"
        ):
            date_reasons.append(
                "既有訂單日期與本次OCR不同，且日期候選沒有足夠一致證據"
            )
        if date_reasons:
            date_context["anomaly_reasons"] = list(dict.fromkeys(date_reasons))
            return ValidationResult(
                "pending",
                "；".join(date_context["anomaly_reasons"]),
                None,
                warnings,
                date_context,
            )
        if record.get("detailCodeColumnVisible") is False:
            return ValidationResult(
                "pending",
                "圖片不完整：訂貨明細欄位不可見",
                None,
                warnings,
            )
        if not order["details"]:
            return ValidationResult(
                "pending", "4001 訂單沒有訂貨明細", None, warnings
            )
        for index, item in enumerate(order["details"], start=1):
            expected = item["list_price"] * item["quantity"]
            if item["total_amount"] != expected:
                return ValidationResult(
                    "pending",
                    (
                        f"第 {index} 筆金額驗證不通過："
                        f"{item['list_price']} x {item['quantity']} != "
                        f"{item['total_amount']}"
                    ),
                    None,
                    warnings,
                )
        summary = record.get("summaryTotals")
        if isinstance(summary, dict):
            try:
                expected_summary = {
                    "quantity": sum(item["quantity"] for item in order["details"]),
                    "totalAmount": sum(
                        item["total_amount"] for item in order["details"]
                    ),
                    "discountAmount": sum(
                        item["discount_amount"] for item in order["details"]
                    ),
                }
                actual_summary = {}
                for key in expected_summary:
                    value = summary.get(key)
                    if value is not None and str(value).strip():
                        actual_summary[key] = normalize_integer(value, key)
            except ValueError as exc:
                return ValidationResult("pending", str(exc), None, warnings)
            mismatches = {
                key: (expected_summary[key], value)
                for key, value in actual_summary.items()
                if value != expected_summary[key]
            }
            if mismatches:
                return ValidationResult(
                    "pending",
                    (
                        "明細加總與畫面合計不符："
                        f"{mismatches}"
                    ),
                    None,
                    warnings,
                )
        return ValidationResult(
            "unchanged", "4001 訂單驗證完整", order, warnings
        )

    return ValidationResult(
        "non_target_department",
        f"非目標部門 {department}，保留掃描紀錄但不納入正式統計",
        order,
        warnings,
    )


def source_key(record: dict[str, Any]) -> tuple[str, str]:
    image_name = str(record.get("imageName") or "").strip()
    order_no = normalize_order_no(record.get("orderNo"))
    identity = image_name or order_no
    if not identity:
        identity = hashlib.sha256(json_text(record).encode("utf-8")).hexdigest()
    temporary_key = order_no or Path(image_name).stem or identity
    return identity, temporary_key


def resolve_image(
    json_path: Path,
    record: dict[str, Any],
) -> tuple[Path | None, str | None, str, str | None]:
    image_name = str(record.get("imageName") or "").strip()
    if not image_name:
        return None, None, "not_provided", None

    candidates: list[Path] = []
    source_path = str(record.get("sourcePath") or "").strip()
    if source_path:
        candidates.append(Path(source_path))
    ocr_root = json_path.parent.parent
    candidates.append(ocr_root / "input_images" / image_name)
    run_root = ocr_root.parent
    candidates.extend(run_root.glob(f"erp_capture/batches/*/screenshots/{image_name}"))

    for candidate in candidates:
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            project_root = next(
                (
                    ancestor.parent
                    for ancestor in json_path.parents
                    if ancestor.name.lower() == "runs"
                ),
                None,
            )
            try:
                if project_root is None:
                    raise ValueError
                relative_path = candidate.resolve().relative_to(
                    project_root.resolve()
                ).as_posix()
            except ValueError:
                try:
                    relative = candidate.resolve().relative_to(
                        run_root.resolve()
                    )
                    relative_path = (
                        Path("replay_sources") / run_root.name / relative
                    ).as_posix()
                except ValueError:
                    relative_path = (
                        Path("replay_sources") / run_root.name / image_name
                    ).as_posix()
            return candidate, relative_path, "available", digest
    return None, None, "missing", None


def create_scan_job(
    connection: sqlite3.Connection,
    job_type: str,
    date_from: str,
    date_to: str,
    source_file: Path,
    started_at: str,
) -> int:
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO scan_jobs(
                job_type, date_from, date_to, query_start_date, query_end_date,
                status, total_count, success_count, unchanged_count,
                changed_count, pending_count, excluded_count, failed_count,
                started_at, finished_at, source_name, source_file,
                summary_json, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'running', 0, 0, 0, 0, 0, 0, 0,
                      ?, NULL, 'OCR JSON replay', ?, NULL, NULL, ?, ?)
            """,
            (
                job_type,
                date_from,
                date_to,
                date_from,
                date_to,
                started_at,
                str(source_file),
                started_at,
                started_at,
            ),
        )
    return int(cursor.lastrowid)


def existing_order_state(
    connection: sqlite3.Connection,
    order_no: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, current_version_id, current_content_hash, store_id
        FROM orders WHERE order_no = ?
        """,
        (order_no,),
    ).fetchone()


def write_order(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    order: dict[str, Any],
    timestamp: str,
) -> tuple[int, int, str]:
    before = existing_order_state(connection, order["order_no"])
    order_id, inserted_order = ensure_order(
        connection, order, timestamp, timestamp, timestamp
    )
    version_id, inserted_version, _ = insert_version(
        connection,
        order_id,
        order,
        record,
        None,
        timestamp,
        timestamp,
        source_system="OCR JSON",
    )
    digest = content_hash(order)
    store_id = store_id_for_order(connection, order, timestamp)
    connection.execute(
        """
        UPDATE orders
        SET current_version_id = ?, current_content_hash = ?, order_date = ?,
            category = ?, department_code = ?, store_id = ?, is_voided = ?,
            last_seen_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            version_id,
            digest,
            order["order_date"],
            order["category"],
            order["department_code"],
            store_id,
            int(order["is_voided"]),
            timestamp,
            timestamp,
            order_id,
        ),
    )
    if inserted_order:
        result = "new"
    elif before["current_content_hash"] == digest:
        result = "unchanged"
    elif inserted_version:
        result = "changed"
    else:
        result = "unchanged"
    return order_id, version_id, result


def upsert_review_queue(
    connection: sqlite3.Connection,
    scan_item_id: int,
    record: dict[str, Any],
    temporary_key: str,
    reason: str,
    timestamp: str,
    resolved_order_no: str | None = None,
) -> int:
    order_no = (
        normalize_order_no(resolved_order_no)
        or normalize_order_no(record.get("orderNo"))
        or None
    )
    key = f"ocr:{order_no or temporary_key}"
    existing = connection.execute(
        """
        SELECT id
        FROM review_queue
        WHERE source_key = ?
           OR (
                ? IS NOT NULL
                AND order_no = ?
                AND status IN ('pending', 'retry_requested')
           )
        ORDER BY CASE WHEN source_key = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (key, order_no, order_no, key),
    ).fetchone()
    if existing:
        review_id = int(existing["id"])
        connection.execute(
            """
            UPDATE review_queue
            SET order_no = COALESCE(?, order_no), scan_item_id = ?,
                reason = ?, raw_payload_json = ?, last_seen_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                order_no,
                scan_item_id,
                reason,
                json_text(record),
                timestamp,
                timestamp,
                review_id,
            ),
        )
    else:
        cursor = connection.execute(
            """
            INSERT INTO review_queue(
                source_key, order_no, scan_item_id, status, reason,
                processing_note, raw_payload_json, first_seen_at, last_seen_at,
                resolved_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, NULL, ?, ?, ?, NULL, ?, ?)
            """,
            (
                key,
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
        review_id = int(cursor.lastrowid)
    return review_id


def stage_pending_image(
    connection: sqlite3.Connection,
    review_queue_id: int,
    image_path: Path | None,
    source_relative_path: str | None,
    digest: str | None,
    timestamp: str,
) -> str | None:
    """Keep a portable pending image beside the writable SQLite database."""
    if image_path is None or digest is None or not image_path.is_file():
        return None
    database_row = connection.execute("PRAGMA database_list").fetchone()
    if database_row is None or not database_row["file"]:
        return None
    data_dir = Path(str(database_row["file"])).resolve().parent
    destination_dir = data_dir / "images" / "pending_review"
    destination_dir.mkdir(parents=True, exist_ok=True)
    safe_order = re.sub(r"[^0-9A-Za-z_-]+", "_", image_path.stem).strip("_")
    safe_order = safe_order or f"review_{review_queue_id}"
    filename = f"{review_queue_id}_{safe_order}_{digest[:12]}{image_path.suffix.lower()}"
    destination = destination_dir / filename
    if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(image_path, temporary)
        copied_digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if copied_digest != digest:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"待確認圖片複製驗證失敗：{image_path}")
        os.replace(temporary, destination)
    relative_path = destination.relative_to(data_dir).as_posix()
    connection.execute(
        """
        INSERT INTO web_pending_images(
          review_queue_id, relative_path, sha256, source_relative_path,
          copied_at, image_status
        ) VALUES(?, ?, ?, ?, ?, 'available')
        ON CONFLICT(review_queue_id) DO UPDATE SET
          relative_path=excluded.relative_path,
          sha256=excluded.sha256,
          source_relative_path=excluded.source_relative_path,
          copied_at=excluded.copied_at,
          image_status='available'
        """,
        (
            review_queue_id,
            relative_path,
            digest,
            source_relative_path,
            timestamp,
        ),
    )
    return relative_path


def resolve_review_queue(
    connection: sqlite3.Connection,
    order_no: str,
    timestamp: str,
) -> None:
    review_ids = [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT id FROM review_queue
            WHERE order_no = ? AND status IN ('pending', 'retry_requested')
            """,
            (order_no,),
        )
    ]
    connection.execute(
        """
        UPDATE review_queue
        SET status = 'resolved',
            processing_note = COALESCE(
                processing_note,
                'OCR or manual correction confirmed as an official order'
            ),
            resolved_at = ?, last_seen_at = ?, updated_at = ?
        WHERE order_no = ? AND status IN ('pending', 'retry_requested')
        """,
        (timestamp, timestamp, timestamp, order_no),
    )
    retained_until = (
        datetime.fromisoformat(timestamp).astimezone(timezone.utc)
        + timedelta(days=7)
    ).isoformat()
    for review_id in review_ids:
        connection.execute(
            """
            UPDATE web_pending_images
            SET processed_at=?, retained_until=?, image_status='available'
            WHERE review_queue_id=?
            """,
            (timestamp, retained_until, review_id),
        )


def is_manually_excluded(
    connection: sqlite3.Connection,
    order_no: str,
) -> bool:
    if not order_no:
        return False
    return connection.execute(
        """
        SELECT 1
        FROM review_queue
        WHERE order_no = ? AND status = 'excluded'
        LIMIT 1
        """,
        (order_no,),
    ).fetchone() is not None


def attach_screenshot(
    connection: sqlite3.Connection,
    scan_item_id: int,
    digest: str | None,
    relative_path: str | None,
    image_status: str,
    image_size: int | None,
    version_id: int | None,
    pending: bool,
    timestamp: str,
) -> int | None:
    if image_status != "available" or not digest or not relative_path:
        return None
    existing = connection.execute(
        "SELECT id FROM screenshots WHERE sha256 = ?", (digest,)
    ).fetchone()
    if existing:
        screenshot_id = int(existing["id"])
    else:
        if version_id is not None and not pending:
            official = connection.execute(
                """
                SELECT id FROM screenshots
                WHERE order_version_id = ? AND image_role = 'official'
                """,
                (version_id,),
            ).fetchone()
            if official:
                screenshot_id = int(official["id"])
                connection.execute(
                    "UPDATE scan_items SET screenshot_id = ? WHERE id = ?",
                    (screenshot_id, scan_item_id),
                )
                return screenshot_id
        cursor = connection.execute(
            """
            INSERT INTO screenshots(
                sha256, relative_path, order_version_id, scan_item_id,
                image_role, file_size, captured_at, protected_until_review,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                relative_path,
                version_id,
                scan_item_id,
                "pending_review" if pending else "official",
                image_size,
                timestamp,
                int(pending),
                timestamp,
            ),
        )
        screenshot_id = int(cursor.lastrowid)
    connection.execute(
        "UPDATE scan_items SET screenshot_id = ? WHERE id = ?",
        (screenshot_id, scan_item_id),
    )
    return screenshot_id


def insert_scan_item(
    connection: sqlite3.Connection,
    scan_job_id: int,
    key: str,
    temporary_key: str,
    record: dict[str, Any],
    timestamp: str,
    image_hash: str | None,
    image_relative_path: str | None,
    image_status: str,
    resolved_order_no: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO scan_items(
            scan_job_id, source_key, image_name, temporary_key, order_no,
            order_id, order_version_id, status, result_code, screenshot_id,
            image_hash, image_relative_path, image_status,
            raw_ocr_payload_json, validation_json, reason, started_at,
            finished_at, processed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 'queued', 'failed', NULL,
                  ?, ?, ?, ?, '{}', NULL, ?, NULL, NULL, ?)
        """,
        (
            scan_job_id,
            key,
            str(record.get("imageName") or "") or None,
            temporary_key,
            (
                normalize_order_no(resolved_order_no)
                or normalize_order_no(record.get("orderNo"))
                or None
            ),
            image_hash,
            image_relative_path,
            image_status,
            json_text(record),
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def finish_scan_item(
    connection: sqlite3.Connection,
    scan_item_id: int,
    result_code: str,
    validation: ValidationResult,
    timestamp: str,
    order_id: int | None = None,
    version_id: int | None = None,
) -> None:
    if result_code not in RESULT_CODES:
        raise ValueError(f"Invalid result code: {result_code}")
    status = {
        "new": "imported",
        "unchanged": "imported",
        "changed": "imported",
        "pending": "pending_review",
        "non_target_department": "skipped",
        "voided": "skipped",
        "failed": "failed",
    }[result_code]
    connection.execute(
        """
        UPDATE scan_items
        SET order_no = COALESCE(?, order_no), order_id = ?,
            order_version_id = ?, status = ?, result_code = ?,
            validation_json = ?, reason = ?, finished_at = ?,
            processed_at = ?
        WHERE id = ?
        """,
        (
            validation.order["order_no"] if validation.order else None,
            order_id,
            version_id,
            status,
            result_code,
            json_text(validation.as_dict()),
            validation.reason,
            timestamp,
            timestamp,
            scan_item_id,
        ),
    )


def record_failed_item(
    connection: sqlite3.Connection,
    scan_job_id: int,
    key: str,
    temporary_key: str,
    record: dict[str, Any],
    reason: str,
    timestamp: str,
    image_hash: str | None,
    image_relative_path: str | None,
    image_status: str,
    image_path: Path | None = None,
    image_size: int | None = None,
) -> None:
    with connection:
        scan_item_id = insert_scan_item(
            connection,
            scan_job_id,
            key,
            temporary_key,
            record,
            timestamp,
            image_hash,
            image_relative_path,
            image_status,
        )
        validation = ValidationResult("failed", reason, None)
        finish_scan_item(
            connection, scan_item_id, "failed", validation, timestamp
        )
        review_queue_id = upsert_review_queue(
            connection,
            scan_item_id,
            record,
            temporary_key,
            reason,
            timestamp,
        )
        attach_screenshot(
            connection,
            scan_item_id,
            image_hash,
            image_relative_path,
            image_status,
            image_size,
            None,
            True,
            timestamp,
        )
        stage_pending_image(
            connection,
            review_queue_id,
            image_path,
            image_relative_path,
            image_hash,
            timestamp,
        )


def process_record(
    connection: sqlite3.Connection,
    scan_job_id: int,
    json_path: Path,
    record: dict[str, Any],
    date_from: str | None = None,
    date_to: str | None = None,
    fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    timestamp = utc_now()
    prepared_record, preparation_warnings = prepare_ocr_record(record)
    resolved_order_no = normalize_order_no(prepared_record.get("orderNo"))
    key, temporary_key = source_key(prepared_record)
    image_path, relative_path, image_status, image_hash = resolve_image(
        json_path, record
    )
    image_size = image_path.stat().st_size if image_path else None

    try:
        connection.execute("BEGIN IMMEDIATE")
        scan_item_id = insert_scan_item(
            connection,
            scan_job_id,
            key,
            temporary_key,
            record,
            timestamp,
            image_hash,
            relative_path,
            image_status,
            resolved_order_no,
        )
        if is_manually_excluded(connection, resolved_order_no):
            validation = ValidationResult(
                "non_target_department",
                "人工已確認不納入；保留掃描紀錄但不寫入正式訂單",
                None,
                preparation_warnings,
            )
        else:
            existing = connection.execute(
                "SELECT order_date FROM orders WHERE order_no=?",
                (resolved_order_no,),
            ).fetchone()
            validation = validate_ocr_record(
                prepared_record,
                preparation_warnings,
                date_from=date_from,
                date_to=date_to,
                existing_order_date=(
                    str(existing["order_date"]) if existing else None
                ),
            )
        result_code = validation.result_code
        order_id = None
        version_id = None

        if validation.order is not None:
            order_id, version_id, content_result = write_order(
                connection, record, validation.order, timestamp
            )
            if result_code not in {"non_target_department", "voided"}:
                result_code = content_result
        validation.result_code = result_code

        if fault_hook:
            fault_hook("after_order_write", record)

        finish_scan_item(
            connection,
            scan_item_id,
            result_code,
            validation,
            timestamp,
            order_id,
            version_id,
        )
        is_pending = result_code in {"pending", "failed"}
        if is_pending:
            pending_record = copy.deepcopy(record)
            if resolved_order_no:
                pending_record["rawOrderNo"] = pending_record.get("orderNo")
                pending_record["orderNo"] = resolved_order_no
            if validation.review_context:
                pending_record["reviewContext"] = validation.review_context
            review_queue_id = upsert_review_queue(
                connection,
                scan_item_id,
                pending_record,
                temporary_key,
                validation.reason,
                timestamp,
                resolved_order_no,
            )
            stage_pending_image(
                connection,
                review_queue_id,
                image_path,
                relative_path,
                image_hash,
                timestamp,
            )
        elif result_code in {"new", "unchanged", "changed"} and validation.order:
            resolve_review_queue(
                connection, validation.order["order_no"], timestamp
            )
        attach_screenshot(
            connection,
            scan_item_id,
            image_hash,
            relative_path,
            image_status,
            image_size,
            version_id,
            is_pending,
            timestamp,
        )
        connection.commit()
        return result_code
    except BaseException as exc:
        connection.rollback()
        if not isinstance(exc, Exception):
            raise
        reason = f"{type(exc).__name__}: {exc}"
        record_failed_item(
            connection,
            scan_job_id,
            key,
            temporary_key,
            record,
            reason,
            timestamp,
            image_hash,
            relative_path,
            image_status,
            image_path,
            image_size,
        )
        return "failed"


def finalize_job(
    connection: sqlite3.Connection,
    summary: ReplaySummary,
    status: str,
    error_message: str | None = None,
) -> None:
    summary.status = status
    summary.finished_at = utc_now()
    with connection:
        connection.execute(
            """
            UPDATE scan_jobs
            SET status = ?, total_count = ?, success_count = ?,
                unchanged_count = ?, changed_count = ?, pending_count = ?,
                excluded_count = ?, failed_count = ?, finished_at = ?,
                summary_json = ?, error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                summary.total_count,
                summary.success_count,
                summary.unchanged_count,
                summary.changed_count,
                summary.pending_count,
                summary.excluded_count,
                summary.failed_count,
                summary.finished_at,
                json_text(summary.as_dict()),
                error_message,
                summary.finished_at,
                summary.scan_job_id,
            ),
        )


def replay_ocr_json(
    connection: sqlite3.Connection,
    json_file: str | Path,
    date_from: str,
    date_to: str,
    job_type: str = "daily_7_days",
    fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
    progress_hook: (
        Callable[[int, int, dict[str, Any], str], None] | None
    ) = None,
) -> ReplaySummary:
    json_path = Path(json_file).resolve()
    payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
    records = payload.get("orders")
    if not isinstance(records, list):
        raise ValueError("OCR JSON does not contain an orders array")
    started_at = utc_now()
    scan_job_id = create_scan_job(
        connection, job_type, date_from, date_to, json_path, started_at
    )
    summary = ReplaySummary(
        scan_job_id=scan_job_id,
        job_type=job_type,
        date_from=date_from,
        date_to=date_to,
        source_file=str(json_path),
        started_at=started_at,
    )
    try:
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                record = {"raw_value": record, "statusLevel": "failed"}
            result_code = process_record(
                connection,
                scan_job_id,
                json_path,
                record,
                date_from,
                date_to,
                fault_hook,
            )
            summary.increment(result_code)
            if progress_hook:
                progress_hook(index, len(records), record, result_code)
        finalize_job(connection, summary, "completed")
    except BaseException as exc:
        finalize_job(
            connection,
            summary,
            "failed",
            f"{type(exc).__name__}: {exc}",
        )
        raise
    return summary
