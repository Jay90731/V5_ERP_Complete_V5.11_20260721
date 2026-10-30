from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web_v1"
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

from wedding_web.write_service import ManualReviewService  # noqa: E402


CONFIRMED_DATES = {
    "GOU26080034": "2026-08-03",
    "GOU26080170": "2026-08-07",
    "GOU26080244": "2026-08-11",
    "GOU26080424": "2026-08-21",
    "GOU26080426": "2026-08-22",
}


def item_payload(item: dict) -> dict:
    return {
        "gift_code": item.get("giftCode") or item.get("gift_code") or "",
        "product_name": item.get("productName") or item.get("product_name") or "",
        "list_price": item.get("listPrice", item.get("list_price")),
        "quantity": item.get("quantity"),
        "total_amount": item.get("totalAmount", item.get("total_amount")),
        "discount_amount": item.get(
            "discountAmount", item.get("discount_amount")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply five audited R4 dates")
    parser.add_argument("--db", required=True)
    parser.add_argument("--ocr-json", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    database = Path(args.db).resolve()
    records = json.loads(
        Path(args.ocr_json).read_text(encoding="utf-8-sig")
    )["orders"]
    by_name = {
        Path(str(record.get("imageName") or "")).stem.upper(): record
        for record in records
    }
    service = ManualReviewService(database)
    service.ensure_ready()
    results = []
    for order_no, confirmed_date in CONFIRMED_DATES.items():
        record = by_name.get(order_no)
        if record is None:
            raise RuntimeError(f"Missing OCR record for {order_no}")
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                """
                SELECT id FROM review_queue
                WHERE order_no=? AND status IN('pending','retry_requested')
                ORDER BY id DESC LIMIT 1
                """,
                (order_no,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError(f"Missing pending review for {order_no}")
        payload = {
            "order_no": order_no,
            "order_date": confirmed_date,
            "category": record.get("category") or "其他",
            "department_code": "4001",
            "customer_name": record.get("customerName") or "",
            "store_code": "SR",
            "items": [item_payload(item) for item in record.get("details") or []],
        }
        if not payload["items"]:
            raise RuntimeError(f"No confirmed detail items for {order_no}")
        result = service.process(
            int(row[0]),
            "confirm",
            payload,
            "R4：已人工核對ERP原始截圖並確認訂貨日期",
        )
        results.append({**result, "confirmed_date": confirmed_date})

    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps({"passed": True, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"passed": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
