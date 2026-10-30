from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .store_rules import store_policy_metrics


COUNTER_FIELDS = (
    "total_count",
    "success_count",
    "unchanged_count",
    "changed_count",
    "pending_count",
    "excluded_count",
    "failed_count",
)


def scalar(
    connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()
) -> int:
    return int(connection.execute(sql, parameters).fetchone()[0])


def job_record(
    connection: sqlite3.Connection, scan_job_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, job_type, date_from, date_to, status,
               total_count, success_count, unchanged_count, changed_count,
               pending_count, excluded_count, failed_count
        FROM scan_jobs
        WHERE id = ?
        """,
        (scan_job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"scan_job not found: {scan_job_id}")
    return dict(row)


def validate_phase2_replay(
    connection: sqlite3.Connection,
    first_job_id: int,
    second_job_id: int,
) -> dict[str, Any]:
    first = job_record(connection, first_job_id)
    second = job_record(connection, second_job_id)
    first_counts = {key: first[key] for key in COUNTER_FIELDS}
    second_counts = {key: second[key] for key in COUNTER_FIELDS}
    item_counts = {
        job_id: scalar(
            connection,
            "SELECT COUNT(*) FROM scan_items WHERE scan_job_id = ?",
            (job_id,),
        )
        for job_id in (first_job_id, second_job_id)
    }
    pending_items = scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM scan_items
        WHERE scan_job_id IN (?, ?) AND result_code = 'pending'
        """,
        (first_job_id, second_job_id),
    )
    duplicate_orders = scalar(
        connection,
        """
        SELECT COUNT(*) FROM (
            SELECT order_no FROM orders
            GROUP BY order_no HAVING COUNT(*) > 1
        )
        """,
    )
    duplicate_versions = scalar(
        connection,
        """
        SELECT COUNT(*) FROM (
            SELECT order_id, content_hash FROM order_versions
            GROUP BY order_id, content_hash HAVING COUNT(*) > 1
        )
        """,
    )
    duplicate_screenshot_hashes = scalar(
        connection,
        """
        SELECT COUNT(*) FROM (
            SELECT sha256 FROM screenshots
            GROUP BY sha256 HAVING COUNT(*) > 1
        )
        """,
    )
    unresolved_review_count = scalar(
        connection,
        "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'",
    )
    store_metrics = store_policy_metrics(connection)
    foreign_key_errors = [
        tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
    ]
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]

    checks = {
        "兩次批次皆完成": (
            first["status"] == "completed"
            and second["status"] == "completed"
        ),
        "工作類型皆為每日7天": (
            first["job_type"] == "daily_7_days"
            and second["job_type"] == "daily_7_days"
        ),
        "查詢日期條件相同": (
            first["date_from"] == second["date_from"]
            and first["date_to"] == second["date_to"]
        ),
        "兩次結果計數一致": first_counts == second_counts,
        "每批scan_items等於總筆數": (
            item_counts[first_job_id] == first["total_count"]
            and item_counts[second_job_id] == second["total_count"]
        ),
        "兩次皆無新增或假異動": (
            first["success_count"] == 0
            and first["changed_count"] == 0
            and second["success_count"] == 0
            and second["changed_count"] == 0
        ),
        "單筆失敗為0": (
            first["failed_count"] == 0 and second["failed_count"] == 0
        ),
        "待確認跨批次保留": (
            unresolved_review_count >= first["pending_count"]
            and pending_items
            == first["pending_count"] + second["pending_count"]
        ),
        "訂單編號無重複": duplicate_orders == 0,
        "內容版本無重複": duplicate_versions == 0,
        "圖片SHA256無重複": duplicate_screenshot_hashes == 0,
        "正式訂單全部指向SR": store_metrics["official_orders_with_sr"]
        == store_metrics["official_orders"],
        "SR門市主檔只有一筆": store_metrics["sr_store_count"] == 1
        and store_metrics["store_count"] == 1,
        "排除資料不套用門市": store_metrics["non_official_with_store"] == 0,
        "外鍵錯誤為0": not foreign_key_errors,
        "SQLite quick_check為ok": quick_check == "ok",
    }
    return {
        "phase": "V6 第二階段：每日7天OCR資料寫入SQLite",
        "first_job": first,
        "second_job": second,
        "database": {
            "orders": scalar(connection, "SELECT COUNT(*) FROM orders"),
            "order_versions": scalar(
                connection, "SELECT COUNT(*) FROM order_versions"
            ),
            "current_order_items": scalar(
                connection,
                """
                SELECT COUNT(*) FROM order_items i
                JOIN orders o ON o.current_version_id = i.order_version_id
                """,
            ),
            "scan_items_first": item_counts[first_job_id],
            "scan_items_second": item_counts[second_job_id],
            "review_queue_pending": unresolved_review_count,
            "screenshots": scalar(
                connection, "SELECT COUNT(*) FROM screenshots"
            ),
            **store_metrics,
            "quick_check": quick_check,
            "foreign_key_errors": foreign_key_errors,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def markdown_report(report: dict[str, Any]) -> str:
    first = report["first_job"]
    database = report["database"]
    lines = [
        "# V6 第二階段資料庫驗證報告",
        "",
        f"- 驗收結果：{'通過' if report['passed'] else '失敗'}",
        f"- 查詢區間：{first['date_from']} 至 {first['date_to']}",
        f"- 每次 OCR 訂單數：{first['total_count']}",
        f"- 4001 未變更：{first['unchanged_count']}",
        f"- 待確認：{first['pending_count']}",
        f"- 非目標部門／作廢：{first['excluded_count']}",
        f"- 失敗：{first['failed_count']}",
        f"- SQLite quick_check：{database['quick_check']}",
        "",
        "## 驗收項目",
        "",
    ]
    for name, passed in report["checks"].items():
        lines.append(f"- [{'x' if passed else ' '}] {name}")
    lines.extend(
        [
            "",
            "## 資料庫筆數",
            "",
            f"- orders：{database['orders']}",
            f"- order_versions：{database['order_versions']}",
            f"- 目前版本 order_items：{database['current_order_items']}",
            f"- screenshots：{database['screenshots']}",
            f"- 未處理 review_queue：{database['review_queue_pending']}",
            "",
        ]
    )
    return "\n".join(lines)


def write_phase2_report(
    path: str | Path, report: dict[str, Any]
) -> tuple[Path, Path]:
    json_path = Path(path).resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return json_path, markdown_path
