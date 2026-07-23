from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TARGET_DEPARTMENT = "4001"
CHANGE_LOG_HEADERS = [
    "run_at",
    "order_no",
    "change_type",
    "changed_fields",
    "previous_department",
    "current_department",
    "previous_version_id",
    "current_version_id",
]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_order_no(value: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(value).upper())


def normalize_date(value: Any) -> str | None:
    text = normalize_text(value)
    digits = re.sub(r"\D", "", text)
    candidates = [text]
    if len(digits) == 8:
        candidates.append(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
    for candidate in candidates:
        normalized = re.sub(r"[./]", "-", candidate)
        try:
            return datetime.strptime(normalized, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_order_date(value: Any, order_no: str) -> tuple[str | None, str | None]:
    normalized = normalize_date(value)
    match = re.fullmatch(r"GOU(\d{2})(\d{2})[0-9A-Z-]*", order_no or "")
    if not normalized or not match:
        return normalized, None
    expected_year = 2000 + int(match.group(1))
    expected_month = int(match.group(2))
    if expected_month < 1 or expected_month > 12:
        return normalized, None
    parsed = datetime.strptime(normalized, "%Y-%m-%d")
    if parsed.year == expected_year and parsed.month == expected_month:
        return normalized, None
    try:
        corrected = parsed.replace(year=expected_year, month=expected_month).strftime("%Y-%m-%d")
    except ValueError:
        return None, f"訂單日期無法依訂單編號校正：{normalized}"
    return corrected, f"訂單日期依訂單編號年月校正：{normalized} -> {corrected}"


def normalize_number(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "").replace("$", "").replace("NT", "")
    text = re.sub(r"\s+", "", text).strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        number = -number
    if number == number.to_integral_value():
        return int(number)
    return format(number.normalize(), "f")


def source_order_no(raw: dict[str, Any]) -> tuple[str, str | None]:
    structured = normalize_order_no(raw.get("orderNo"))
    image_stem = Path(normalize_text(raw.get("imageName"))).stem.upper()
    if re.fullmatch(r"GOU[0-9A-Z-]{6,20}", image_stem):
        warning = None
        if structured and structured != image_stem:
            warning = f"訂單編號以截圖檔名校正：{structured} -> {image_stem}"
        return image_stem, warning
    return structured, None


def normalize_detail(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    detail = {
        "gift_code": normalize_text(raw.get("giftCode")),
        "list_price": normalize_number(raw.get("listPrice")),
        "quantity": normalize_number(raw.get("quantity")),
        "total_amount": normalize_number(raw.get("totalAmount")),
        "discount_amount": normalize_number(raw.get("discountAmount")),
    }
    missing = [key for key, value in detail.items() if value in (None, "")]
    return detail, missing


def detail_sort_key(detail: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(detail[key]) for key in (
        "gift_code",
        "list_price",
        "quantity",
        "total_amount",
        "discount_amount",
    ))


def comparison_payload(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": order["category"],
        "order_date": order["order_date"],
        "department_code": order["department_code"],
        "detail_count": order["detail_count"],
        "details": order["details"],
    }


def version_id(order: dict[str, Any]) -> str:
    encoded = json.dumps(
        comparison_payload(order),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_order(raw: dict[str, Any], target_department: str = TARGET_DEPARTMENT) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    order_no, order_warning = source_order_no(raw)
    if order_warning:
        warnings.append(order_warning)
    category = normalize_text(raw.get("category"))
    order_date, date_warning = normalize_order_date(raw.get("orderDate"), order_no)
    if date_warning:
        warnings.append(date_warning)
    department = normalize_text(raw.get("departmentCode"))
    source_status = normalize_text(raw.get("statusLevel")).lower()
    source_error = normalize_text(raw.get("errorReason"))
    voided = bool(raw.get("voided"))

    if source_status != "success":
        errors.append(source_error or "OCR失敗或欄位不完整")
    if not order_no:
        errors.append("訂單編號缺失")
    if not category:
        errors.append("訂單分類缺失")
    if not order_date:
        errors.append("訂單日期缺失或格式錯誤")
    if not department:
        errors.append("部門代號缺失")

    details: list[dict[str, Any]] = []
    raw_details = raw.get("details") or []
    if not raw_details:
        # The current OCR pipeline intentionally skips detail parsing for
        # successful 4100 orders and for voided orders. Keep their main record
        # in history so a later department change to 4001 is classified as a
        # change instead of a new order.
        if source_status == "success" and ((department and department != target_department) or voided):
            warnings.append("依現行OCR規則未解析明細，主檔仍可建立歷史基準")
        else:
            errors.append("訂貨明細缺失")
    for index, raw_detail in enumerate(raw_details, start=1):
        detail, missing = normalize_detail(raw_detail)
        if missing:
            errors.append(f"第{index}筆明細欄位缺失：{','.join(missing)}")
        details.append(detail)
    details.sort(key=detail_sort_key)

    order = {
        "order_no": order_no,
        "category": category,
        "order_date": order_date,
        "department_code": department,
        "detail_count": len(details),
        "details": details,
        "voided": voided,
        "excel_eligible": bool(
            not errors
            and department == target_department
            and not voided
        ),
        "source": {
            "image_name": normalize_text(raw.get("imageName")),
            "source_path": normalize_text(raw.get("sourcePath")),
            "image_mtime": raw.get("imageMtime"),
            "raw_order_no": normalize_text(raw.get("orderNo")),
            "raw_order_date": normalize_text(raw.get("orderDate")),
            "status": source_status or "unknown",
            "error": source_error,
            "warnings": warnings,
        },
    }
    return order, list(dict.fromkeys(errors))


def changed_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    labels = {
        "category": "訂單分類",
        "order_date": "訂單日期",
        "department_code": "部門代號",
        "detail_count": "明細列數",
        "details": "完整訂貨明細",
    }
    return [
        label
        for key, label in labels.items()
        if previous.get(key) != current.get(key)
    ]


def empty_history(target_department: str, run_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target_department": target_department,
        "baseline_created_at": run_at,
        "updated_at": run_at,
        "orders": {},
    }


def select_current_records(
    raw_orders: list[dict[str, Any]],
    target_department: str,
) -> tuple[list[dict[str, Any]], int]:
    groups: dict[str, list[tuple[dict[str, Any], list[str]]]] = defaultdict(list)
    unkeyed: list[tuple[dict[str, Any], list[str]]] = []
    source_failed_count = 0

    for raw in raw_orders:
        if normalize_text(raw.get("statusLevel")).lower() == "failed":
            source_failed_count += 1
        normalized, errors = normalize_order(raw, target_department)
        if normalized["order_no"]:
            groups[normalized["order_no"]].append((normalized, errors))
        else:
            unkeyed.append((normalized, errors))

    records: list[dict[str, Any]] = []
    for order_no in sorted(groups):
        candidates = groups[order_no]
        valid = [(order, errors) for order, errors in candidates if not errors]
        signatures: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for order, _ in valid:
            signatures[version_id(order)].append(order)

        source_images = sorted({order["source"]["image_name"] for order, _ in candidates})
        if len(signatures) == 1:
            chosen = max(
                next(iter(signatures.values())),
                key=lambda item: item["source"].get("image_mtime") or 0,
            )
            records.append({
                "order_no": order_no,
                "valid": True,
                "order": chosen,
                "reason": "",
                "source_images": source_images,
                "duplicate_source_count": len(candidates),
            })
        elif len(signatures) > 1:
            records.append({
                "order_no": order_no,
                "valid": False,
                "order": None,
                "reason": "同一訂單編號出現多個不同的完整版本，需人工確認",
                "source_images": source_images,
                "duplicate_source_count": len(candidates),
            })
        else:
            reasons = []
            for _, errors in candidates:
                reasons.extend(errors)
            records.append({
                "order_no": order_no,
                "valid": False,
                "order": candidates[0][0],
                "reason": "；".join(dict.fromkeys(reasons)) or "OCR失敗或欄位不完整",
                "source_images": source_images,
                "duplicate_source_count": len(candidates),
            })

    for index, (order, errors) in enumerate(unkeyed, start=1):
        records.append({
            "order_no": None,
            "pending_key": f"UNKEYED-{index:04d}",
            "valid": False,
            "order": order,
            "reason": "；".join(dict.fromkeys(errors)) or "訂單編號缺失",
            "source_images": [order["source"]["image_name"]],
            "duplicate_source_count": 1,
        })
    return records, source_failed_count


def process_orders(
    payload: dict[str, Any],
    history: dict[str, Any] | None,
    initialize_baseline: bool,
    run_at: str,
    source_image_pattern: str | None = None,
    target_department: str = TARGET_DEPARTMENT,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]], dict[str, Any]]:
    all_orders = list(payload.get("orders") or [])
    if source_image_pattern:
        regex = re.compile(source_image_pattern, re.IGNORECASE)
        raw_orders = [order for order in all_orders if regex.search(normalize_text(order.get("imageName")))]
    else:
        raw_orders = all_orders

    if initialize_baseline:
        if history and history.get("orders"):
            raise ValueError("BASELINE 已存在，不能再次初始化")
        new_history = empty_history(target_department, run_at)
    else:
        if not history or not isinstance(history.get("orders"), dict):
            raise ValueError("找不到 order_history.json，第一次執行必須使用 --initialize-baseline")
        new_history = copy.deepcopy(history)
        new_history["updated_at"] = run_at

    records, source_failed_count = select_current_records(raw_orders, target_department)
    current_entries: list[dict[str, Any]] = []
    change_events: list[dict[str, str]] = []
    counts = {
        "baseline": 0,
        "new": 0,
        "unchanged": 0,
        "changed": 0,
        "pending": 0,
        "failed": 0,
    }

    seen_current_order_numbers: set[str] = set()
    for record in records:
        order_no = record.get("order_no")
        if order_no:
            seen_current_order_numbers.add(order_no)
        if not record["valid"]:
            counts["pending"] += 1
            current_entries.append({
                "order_no": order_no,
                "status": "待確認",
                "reason": record["reason"],
                "source_images": record["source_images"],
                "order": record.get("order"),
            })
            continue

        order = record["order"]
        current_version_id = version_id(order)
        previous_entry = new_history["orders"].get(order_no)
        previous_version_id = previous_entry.get("current_version_id", "") if previous_entry else ""
        previous_order = previous_entry.get("current_version") if previous_entry else None

        if initialize_baseline:
            status = "基準建立"
            counts["baseline"] += 1
            version_status = "基準建立"
        elif not previous_entry:
            status = "新增"
            counts["new"] += 1
            version_status = "新增"
        elif previous_version_id == current_version_id:
            status = "未變更"
            counts["unchanged"] += 1
            version_status = ""
        else:
            status = "已變更"
            counts["changed"] += 1
            version_status = "已變更"

        if status != "未變更":
            version_record = {
                "version_id": current_version_id,
                "effective_at": run_at,
                "change_type": version_status,
                "order": copy.deepcopy(order),
            }
            if previous_entry:
                previous_entry["current_version_id"] = current_version_id
                previous_entry["current_version"] = copy.deepcopy(order)
                previous_entry.setdefault("versions", []).append(version_record)
                previous_entry["updated_at"] = run_at
            else:
                new_history["orders"][order_no] = {
                    "current_version_id": current_version_id,
                    "current_version": copy.deepcopy(order),
                    "versions": [version_record],
                    "created_at": run_at,
                    "updated_at": run_at,
                }

        changed = changed_fields(previous_order, order) if previous_order else []
        if status in {"新增", "已變更"}:
            change_events.append({
                "run_at": run_at,
                "order_no": order_no,
                "change_type": status,
                "changed_fields": ";".join(changed if changed else ["整張訂單"]),
                "previous_department": previous_order.get("department_code", "") if previous_order else "",
                "current_department": order["department_code"],
                "previous_version_id": previous_version_id,
                "current_version_id": current_version_id,
            })

        current_entries.append({
            "order_no": order_no,
            "status": status,
            "reason": "",
            "source_images": record["source_images"],
            "duplicate_source_count": record["duplicate_source_count"],
            "version_id": current_version_id,
            "order": order,
        })

    history_order_numbers = set(new_history["orders"])
    not_seen = sorted(history_order_numbers - seen_current_order_numbers)
    summary = {
        "run_at": run_at,
        "initialize_baseline": initialize_baseline,
        "target_department": target_department,
        "input_order_rows": len(all_orders),
        "selected_order_rows": len(raw_orders),
        "current_unique_records": len(records),
        "baseline_count": counts["baseline"],
        "new_count": counts["new"],
        "unchanged_count": counts["unchanged"],
        "changed_count": counts["changed"],
        "pending_count": counts["pending"],
        "failed_count": counts["failed"],
        "source_failed_count": source_failed_count,
        "history_order_count": len(new_history["orders"]),
        "not_seen_history_count": len(not_seen),
        "not_seen_history_orders": not_seen,
    }
    current_document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": run_at,
        "target_department": target_department,
        "summary": summary,
        "orders": current_entries,
    }
    new_history["updated_at"] = run_at
    return current_document, new_history, change_events, summary


def read_change_log(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_change_log(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHANGE_LOG_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def run_excel_builder(
    node: Path,
    builder: Path,
    history_path: Path,
    current_path: Path,
    output_path: Path,
    preview_path: Path | None,
) -> None:
    executable = Path(sys.executable) if builder.suffix.lower() == ".py" else node
    command = [str(executable), str(builder), str(history_path), str(output_path)]
    if builder.suffix.lower() == ".py":
        command.append(str(current_path))
        if preview_path:
            command.append(str(preview_path))
    elif preview_path:
        command.append(str(preview_path))
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Excel builder failed "
            f"(exit={completed.returncode}).\n"
            f"stdout:\n{completed.stdout.strip()}\n"
            f"stderr:\n{completed.stderr.strip()}"
        )
    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("current_orders.xlsx 產生失敗或檔案過小")


def transactional_commit(
    state_dir: Path,
    staged_files: dict[Path, Path],
    backup_dir: Path,
) -> None:
    originals: dict[Path, Path | None] = {}
    backup_dir.mkdir(parents=True, exist_ok=True)
    for staged, destination in staged_files.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup_path = backup_dir / destination.name
            shutil.copy2(destination, backup_path)
            originals[destination] = backup_path
        else:
            originals[destination] = None

    committed: list[Path] = []
    try:
        for staged, destination in staged_files.items():
            os.replace(staged, destination)
            committed.append(destination)
    except Exception:
        for destination in reversed(committed):
            backup_path = originals[destination]
            if backup_path and backup_path.exists():
                shutil.copy2(backup_path, destination)
            elif destination.exists():
                destination.unlink()
        raise


def execute_update(args: argparse.Namespace) -> dict[str, Any]:
    if not args.batch_complete:
        raise ValueError("整批截圖及OCR尚未標記完成，拒絕更新歷史")

    input_path = Path(args.input).resolve()
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    history_path = state_dir / "order_history.json"
    current_path = state_dir / "current_orders.json"
    change_log_path = state_dir / "change_log.csv"
    excel_path = state_dir / "current_orders.xlsx"
    pending_review_path = state_dir / "pending_review.json"
    history = read_json(history_path) if history_path.exists() else None
    payload = read_json(input_path)
    run_at = args.run_at or datetime.now().astimezone().isoformat(timespec="seconds")

    current_document, new_history, events, summary = process_orders(
        payload=payload,
        history=history,
        initialize_baseline=args.initialize_baseline,
        run_at=run_at,
        source_image_pattern=args.source_image_pattern,
        target_department=args.target_department,
    )
    existing_events = read_change_log(change_log_path)
    all_events = existing_events + events

    staging_dir = state_dir / ".staging" / uuid.uuid4().hex
    staging_dir.mkdir(parents=True, exist_ok=False)
    staged_current = staging_dir / "current_orders.json"
    staged_history = staging_dir / "order_history.json"
    staged_change_log = staging_dir / "change_log.csv"
    staged_excel = staging_dir / "current_orders.xlsx"
    staged_snapshot = staging_dir / "snapshot.json"
    staged_summary = staging_dir / "run_summary.json"
    staged_pending_review = staging_dir / "pending_review.json"

    try:
        write_json(staged_current, current_document)
        write_json(staged_history, new_history)
        write_change_log(staged_change_log, all_events)
        write_json(staged_snapshot, current_document)
        write_json(staged_summary, summary)
        review_source = Path(args.pending_review).resolve() if args.pending_review else pending_review_path
        review_document = read_json(review_source) if review_source.exists() else {
            "schema_version": 1,
            "updated_at": run_at,
            "records": {},
        }
        write_json(staged_pending_review, review_document)

        if args.skip_excel:
            staged_excel.write_bytes(b"TEST-ONLY-XLSX-SKIPPED")
        else:
            if not args.excel_builder:
                raise ValueError("未提供 Excel builder，無法安全產生 current_orders.xlsx")
            run_excel_builder(
                Path(args.node) if args.node else Path(sys.executable),
                Path(args.excel_builder),
                staged_history,
                staged_current,
                staged_excel,
                Path(args.preview) if args.preview else None,
            )

        snapshot_date = datetime.fromisoformat(run_at).date().isoformat()
        snapshot_path = state_dir / "history_snapshots" / f"{snapshot_date}.json"
        summary_stamp = re.sub(r"[^0-9]", "", run_at)[:14]
        summary_path = state_dir / "run_summaries" / f"{summary_stamp}.json"
        backup_stamp = re.sub(r"[^0-9]", "", run_at)[:14]
        backup_dir = state_dir / "backups" / backup_stamp
        transactional_commit(
            state_dir,
            {
                staged_current: current_path,
                staged_history: history_path,
                staged_change_log: change_log_path,
                staged_excel: excel_path,
                staged_snapshot: snapshot_path,
                staged_summary: summary_path,
                staged_pending_review: pending_review_path,
            },
            backup_dir,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="婚約訂單每日增量與異動比較引擎")
    parser.add_argument("--input", required=True, help="本次完整 OCR orders.json")
    parser.add_argument("--state-dir", required=True, help="正式狀態與輸出資料夾")
    parser.add_argument("--batch-complete", action="store_true", help="確認整批截圖與 OCR 已完成")
    parser.add_argument("--initialize-baseline", action="store_true", help="建立第一份六個月 BASELINE")
    parser.add_argument("--source-image-pattern", help="只處理符合檔名規則的來源，例如 ^GOU.*\\.png$")
    parser.add_argument("--target-department", default=TARGET_DEPARTMENT)
    parser.add_argument("--run-at", help="測試用固定時間，ISO 8601")
    parser.add_argument("--node", help="Node.js 執行檔；Python Excel builder 不使用此參數")
    parser.add_argument("--excel-builder", help="current_orders.xlsx builder")
    parser.add_argument("--pending-review", help="待確認人工處理的下一版 pending_review.json")
    parser.add_argument("--preview", help="選填：Excel 預覽 PNG 路徑")
    parser.add_argument("--skip-excel", action="store_true", help="僅供自動測試")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        summary = execute_update(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
