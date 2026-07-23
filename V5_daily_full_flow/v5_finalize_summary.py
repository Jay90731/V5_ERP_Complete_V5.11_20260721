from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-orders", required=True)
    parser.add_argument("--validation", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--query-start", required=True)
    parser.add_argument("--query-end", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--excel", required=True)
    parser.add_argument("--run-mode", required=True)
    args = parser.parse_args()

    current = json.loads(Path(args.current_orders).read_text(encoding="utf-8-sig"))
    validation = json.loads(Path(args.validation).read_text(encoding="utf-8-sig"))
    summary = current.get("summary") or {}
    statuses: dict[str, int] = {}
    department_4001 = 0
    non_4001 = 0
    unknown_department = 0
    for row in current.get("orders") or []:
        status = row.get("status") or ""
        statuses[status] = statuses.get(status, 0) + 1
        order = row.get("order") or {}
        department = str(order.get("department_code") or "")
        if department == "4001":
            department_4001 += 1
        elif department:
            non_4001 += 1
        else:
            unknown_department += 1

    finished = datetime.now().astimezone()
    started = datetime.fromisoformat(args.started_at)
    result = {
        "version": "V5",
        "status": "completed",
        "run_mode": args.run_mode,
        "query_start": args.query_start,
        "query_end": args.query_end,
        "total_orders": len(current.get("orders") or []),
        "department_4001": department_4001,
        "non_4001": non_4001,
        "unknown_department": unknown_department,
        "new": int(summary.get("new_count") or 0),
        "unchanged": int(summary.get("unchanged_count") or 0),
        "changed": int(summary.get("changed_count") or 0),
        "pending": int(summary.get("pending_count") or 0),
        "failed": int(summary.get("failed_count") or 0),
        "ocr_pending": int(validation.get("pending") or 0),
        "duration_seconds": round((finished - started).total_seconds(), 2),
        "started_at": args.started_at,
        "finished_at": finished.isoformat(timespec="seconds"),
        "excel_path": str(Path(args.excel).resolve()),
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "daily_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "V5 每日完整流程摘要",
        f"狀態：完成",
        f"查詢日期：{result['query_start']} 至 {result['query_end']}",
        f"總訂單數：{result['total_orders']}",
        f"4001數量：{result['department_4001']}",
        f"非4001數量：{result['non_4001']}",
        f"新增：{result['new']}",
        f"未變更：{result['unchanged']}",
        f"已變更：{result['changed']}",
        f"待確認：{result['pending']}",
        f"失敗：{result['failed']}",
        f"執行時間：{result['duration_seconds']}秒",
        f"Excel：{result['excel_path']}",
    ]
    (output_dir / "daily_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
