from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def scalar(connection: sqlite3.Connection, query: str, params=()) -> int:
    return int(connection.execute(query, params).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--json-report", type=Path, required=True)
    parser.add_argument("--markdown-report", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    checks = {
        "orders": scalar(connection, "SELECT COUNT(*) FROM orders"),
        "order_versions": scalar(connection, "SELECT COUNT(*) FROM order_versions"),
        "order_items": scalar(connection, "SELECT COUNT(*) FROM order_items"),
        "review_queue_total": scalar(connection, "SELECT COUNT(*) FROM review_queue"),
        "review_queue_pending": scalar(connection, "SELECT COUNT(*) FROM review_queue WHERE status='pending'"),
        "review_queue_resolved": scalar(connection, "SELECT COUNT(*) FROM review_queue WHERE status='resolved'"),
        "review_queue_excluded": scalar(connection, "SELECT COUNT(*) FROM review_queue WHERE status='excluded'"),
        "excluded_scan_items": scalar(connection, "SELECT COUNT(*) FROM scan_items WHERE status='skipped'"),
        "non_target_scan_items": scalar(connection, "SELECT COUNT(*) FROM scan_items WHERE result_code='non_target_department'"),
        "voided_scan_items": scalar(connection, "SELECT COUNT(*) FROM scan_items WHERE result_code='voided'"),
        "duplicate_order_no": scalar(connection, "SELECT COUNT(*) FROM (SELECT order_no FROM orders GROUP BY order_no HAVING COUNT(*)>1)"),
        "duplicate_order_version": scalar(connection, "SELECT COUNT(*) FROM (SELECT order_id,content_hash FROM order_versions GROUP BY order_id,content_hash HAVING COUNT(*)>1)"),
        "orders_without_current_version": scalar(connection, "SELECT COUNT(*) FROM orders WHERE current_version_id IS NULL"),
        "formal_orders": scalar(connection, "SELECT COUNT(*) FROM orders WHERE department_code='4001' AND is_voided=0 AND status='confirmed' AND EXISTS(SELECT 1 FROM order_items WHERE order_version_id=orders.current_version_id)"),
        "formal_orders_sr": scalar(connection, "SELECT COUNT(*) FROM orders o JOIN stores s ON s.id=o.store_id WHERE o.department_code='4001' AND o.is_voided=0 AND o.status='confirmed' AND s.store_code='SR' AND EXISTS(SELECT 1 FROM order_items WHERE order_version_id=o.current_version_id)"),
        "bhq_orders": scalar(connection, "SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system='BHQ'"),
        "bhq_items": scalar(connection, "SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id JOIN order_items i ON i.order_version_id=v.id WHERE v.source_system='BHQ'"),
        "stores_total": scalar(connection, "SELECT COUNT(*) FROM stores"),
        "formal_missing_items": scalar(connection, "SELECT COUNT(*) FROM orders o WHERE o.department_code='4001' AND o.is_voided=0 AND o.status='confirmed' AND NOT EXISTS(SELECT 1 FROM order_items WHERE order_version_id=o.current_version_id)"),
        "sr_store_rows": scalar(connection, "SELECT COUNT(*) FROM stores WHERE store_code='SR'"),
        "pending_images": scalar(connection, "SELECT COUNT(*) FROM web_pending_images"),
        "pending_images_available": scalar(connection, "SELECT COUNT(*) FROM web_pending_images WHERE image_status='available'"),
        "pending_without_available_image": scalar(connection, "SELECT COUNT(*) FROM review_queue rq WHERE rq.status='pending' AND NOT EXISTS(SELECT 1 FROM web_pending_images wpi WHERE wpi.review_queue_id=rq.id AND wpi.image_status='available')"),
        "processed_image_retention_missing": scalar(connection, "SELECT COUNT(*) FROM web_pending_images wpi JOIN review_queue rq ON rq.id=wpi.review_queue_id WHERE rq.status IN('resolved','excluded') AND wpi.image_status='available' AND (wpi.processed_at IS NULL OR wpi.retained_until IS NULL)"),
        "manual_schema_version": scalar(connection, "SELECT COUNT(*) FROM web_schema_migrations WHERE version=2 AND name='web_v1_manual_review_writeback'"),
        "manual_review_events": scalar(connection, "SELECT COUNT(*) FROM web_manual_review_events"),
        "verified_backups": scalar(connection, "SELECT COUNT(*) FROM backup_records WHERE status='verified'"),
        "open_month_gaps": scalar(connection, "SELECT COUNT(*) FROM history_month_gaps WHERE status='open'"),
        "completed_history_jobs": scalar(connection, "SELECT COUNT(*) FROM scan_jobs WHERE job_type='history_2_years' AND status='completed'"),
        "daily_7_days_jobs": scalar(connection, "SELECT COUNT(*) FROM scan_jobs WHERE job_type='daily_7_days'"),
        "monthly_6_months_jobs": scalar(connection, "SELECT COUNT(*) FROM scan_jobs WHERE job_type='monthly_6_months'"),
        "running_scan_jobs": scalar(connection, "SELECT COUNT(*) FROM scan_jobs WHERE status='running'"),
        "GOU26080250": scalar(connection, "SELECT COUNT(*) FROM orders WHERE order_no='GOU26080250'"),
        "GOU26080219": scalar(connection, "SELECT COUNT(*) FROM orders WHERE order_no='GOU26080219'"),
        "GOU26070611": scalar(connection, "SELECT COUNT(*) FROM orders WHERE order_no='GOU26070611'"),
    }
    connection.close()
    passed = (
        quick == "ok"
        and foreign_keys == 0
        and checks["duplicate_order_no"] == 0
        and checks["duplicate_order_version"] == 0
        and checks["orders_without_current_version"] == 0
        and checks["formal_orders"] == checks["formal_orders_sr"]
        and checks["formal_missing_items"] == 0
        and checks["sr_store_rows"] == 1
        and checks["pending_without_available_image"] == 0
        and checks["processed_image_retention_missing"] == 0
        and checks["manual_schema_version"] == 1
        and checks["running_scan_jobs"] == 0
    )
    report = {
        "status": "passed" if passed else "failed",
        "validated_at": datetime.now().astimezone().isoformat(),
        "database": str(database),
        "quick_check": quick,
        "foreign_key_errors": foreign_keys,
        "counts": checks,
    }
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 婚約訂單網站 V1 資料庫驗證報告",
        "",
        f"- 結果：**{report['status']}**",
        f"- SQLite quick_check：`{quick}`",
        f"- 外鍵錯誤：{foreign_keys}",
        f"- 訂單／版本／明細：{checks['orders']} / {checks['order_versions']} / {checks['order_items']}",
        f"- 正式 SR 訂單：{checks['formal_orders_sr']} / {checks['formal_orders']}",
        f"- BHQ 訂單／明細：{checks['bhq_orders']} / {checks['bhq_items']}；全部門市主檔：{checks['stores_total']}",
        f"- 待確認：{checks['review_queue_pending']}（總表 {checks['review_queue_total']}，另含 resolved {checks['review_queue_resolved']}、excluded {checks['review_queue_excluded']}）",
        f"- 排除掃描：{checks['excluded_scan_items']}（非 4001：{checks['non_target_scan_items']}、作廢：{checks['voided_scan_items']}）",
        f"- 訂單編號重複：{checks['duplicate_order_no']}",
        f"- 訂單版本重複：{checks['duplicate_order_version']}",
        f"- 缺少目前版本的訂單：{checks['orders_without_current_version']}",
        f"- 正式訂單缺少明細：{checks['formal_missing_items']}",
        f"- 待確認圖片：{checks['pending_images']}（可開啟 {checks['pending_images_available']}，待確認缺圖 {checks['pending_without_available_image']}）",
        f"- 人工寫回 schema：{'已啟用' if checks['manual_schema_version'] == 1 else '缺少'}",
        f"- 人工處理事件：{checks['manual_review_events']}；已驗證備份紀錄：{checks['verified_backups']}",
        f"- 已處理圖片保留設定缺失：{checks['processed_image_retention_missing']}",
        f"- 月初缺口紀錄：{checks['open_month_gaps']}（僅記錄、不重掃）",
        f"- 完成歷史月份：{checks['completed_history_jobs']}",
        f"- 日常批次紀錄（7 天／6 個月／執行中）：{checks['daily_7_days_jobs']} / {checks['monthly_6_months_jobs']} / {checks['running_scan_jobs']}",
        f"- 人工確認訂單 GOU26080250／GOU26080219／GOU26070611：{checks['GOU26080250']} / {checks['GOU26080219']} / {checks['GOU26070611']}",
    ]
    args.markdown_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
