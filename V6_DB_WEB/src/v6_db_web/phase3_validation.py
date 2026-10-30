from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .store_rules import store_policy_metrics


RESULT_FIELDS = (
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


def read_run_state(path: str | Path) -> tuple[Path, dict[str, Any]]:
    candidate = Path(path).resolve()
    state_path = (
        candidate / "run_state.json" if candidate.is_dir() else candidate
    )
    if not state_path.is_file():
        raise FileNotFoundError(f"run_state.json not found: {state_path}")
    return state_path.parent, json.loads(
        state_path.read_text(encoding="utf-8-sig")
    )


def scan_job(
    connection: sqlite3.Connection, scan_job_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, job_type, date_from, date_to, status,
               total_count, success_count, unchanged_count, changed_count,
               pending_count, excluded_count, failed_count
        FROM scan_jobs WHERE id = ?
        """,
        (scan_job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"scan_job not found: {scan_job_id}")
    return dict(row)


def validate_phase3_runs(
    first_run: str | Path,
    second_run: str | Path,
) -> dict[str, Any]:
    first_dir, first_state = read_run_state(first_run)
    second_dir, second_state = read_run_state(second_run)
    first_database = Path(first_state["database_path"]).resolve()
    second_database = Path(second_state["database_path"]).resolve()
    if first_database != second_database:
        raise ValueError("The two Phase 3 runs used different databases")
    if first_database.name.lower() != "orders_test.db":
        raise ValueError("Phase 3 validation only permits orders_test.db")

    connection = sqlite3.connect(first_database)
    connection.row_factory = sqlite3.Row
    try:
        first_job = scan_job(connection, int(first_state["scan_job_id"]))
        second_job = scan_job(connection, int(second_state["scan_job_id"]))
        foreign_key_errors = [
            tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
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
        duplicate_images = scalar(
            connection,
            """
            SELECT COUNT(*) FROM (
              SELECT sha256 FROM screenshots
              GROUP BY sha256 HAVING COUNT(*) > 1
            )
            """,
        )
        store_metrics = store_policy_metrics(connection)
        pending_reviews = scalar(
            connection,
            "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'",
        )
        order_count = scalar(connection, "SELECT COUNT(*) FROM orders")
        version_count = scalar(
            connection, "SELECT COUNT(*) FROM order_versions"
        )
    finally:
        connection.close()

    first_counts = {key: first_job[key] for key in RESULT_FIELDS}
    second_counts = {key: second_job[key] for key in RESULT_FIELDS}
    first_summary = json.loads(
        Path(first_state["summary_path"]).read_text(encoding="utf-8-sig")
    )
    second_summary = json.loads(
        Path(second_state["summary_path"]).read_text(encoding="utf-8-sig")
    )
    root = first_database.parent.parent
    formal_database = root / "data" / "orders.db"

    checks = {
        "兩次流程皆完成": (
            first_state["status"] == "completed"
            and second_state["status"] == "completed"
        ),
        "SQLite與完成階段皆完成": all(
            state_stage["status"] == "completed"
            for state in (first_state, second_state)
            for state_stage in state["stages"]
            if state_stage["key"] in {"sqlite_write", "complete"}
        ),
        "查詢日期相同": (
            first_state["query_start"] == second_state["query_start"]
            and first_state["query_end"] == second_state["query_end"]
        ),
        "截圖數等於OCR數及scan_job總數": all(
            int(state["screenshot_count"])
            == int(state["ocr_count"])
            == int(job["total_count"])
            for state, job in (
                (first_state, first_job),
                (second_state, second_job),
            )
        ),
        "兩次分類計數一致": first_counts == second_counts,
        "兩次皆無假新增或假異動": (
            first_job["success_count"] == 0
            and first_job["changed_count"] == 0
            and second_job["success_count"] == 0
            and second_job["changed_count"] == 0
        ),
        "兩次皆無失敗": (
            first_job["failed_count"] == 0
            and second_job["failed_count"] == 0
        ),
        "批次摘要與scan_job一致": all(
            int(summary["total_count"]) == int(job["total_count"])
            and int(summary["pending_count"]) == int(job["pending_count"])
            and int(summary["failed_count"]) == int(job["failed_count"])
            for summary, job in (
                (first_summary, first_job),
                (second_summary, second_job),
            )
        ),
        "訂單編號無重複": duplicate_orders == 0,
        "內容版本無重複": duplicate_versions == 0,
        "圖片SHA256無重複": duplicate_images == 0,
        "正式訂單全部指向SR": store_metrics["official_orders_with_sr"]
        == store_metrics["official_orders"],
        "SR門市主檔只有一筆": store_metrics["sr_store_count"] == 1
        and store_metrics["store_count"] == 1,
        "排除資料不套用門市": store_metrics["non_official_with_store"] == 0,
        "外鍵錯誤為0": not foreign_key_errors,
        "SQLite quick_check為ok": quick_check == "ok",
        "未建立正式orders.db": not formal_database.exists(),
    }
    return {
        "phase": "V6 第三階段：實際ERP每日7天流程接入SQLite",
        "first_run": {
            "run_dir": str(first_dir),
            "state": first_state,
            "scan_job": first_job,
        },
        "second_run": {
            "run_dir": str(second_dir),
            "state": second_state,
            "scan_job": second_job,
        },
        "database": {
            "path": str(first_database),
            "orders": order_count,
            "order_versions": version_count,
            "pending_reviews": pending_reviews,
            **store_metrics,
            "duplicate_orders": duplicate_orders,
            "duplicate_versions": duplicate_versions,
            "duplicate_images": duplicate_images,
            "foreign_key_errors": foreign_key_errors,
            "quick_check": quick_check,
            "formal_database_exists": formal_database.exists(),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def markdown_report(report: dict[str, Any]) -> str:
    job = report["first_run"]["scan_job"]
    database = report["database"]
    lines = [
        "# V6 第三階段 SQLite 驗證報告",
        "",
        f"- 驗證結果：{'通過' if report['passed'] else '失敗'}",
        f"- 每次訂單數：{job['total_count']}",
        f"- 未變更：{job['unchanged_count']}",
        f"- 待確認：{job['pending_count']}",
        f"- 排除：{job['excluded_count']}",
        f"- 失敗：{job['failed_count']}",
        f"- 訂單版本：{database['order_versions']}",
        f"- SQLite quick_check：{database['quick_check']}",
        "",
        "## 驗證項目",
        "",
    ]
    for name, passed in report["checks"].items():
        lines.append(f"- [{'x' if passed else ' '}] {name}")
    lines.append("")
    return "\n".join(lines)


def write_phase3_report(
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
