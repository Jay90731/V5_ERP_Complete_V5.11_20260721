from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WEB_ROOT.parent
sys.path.insert(0, str(WEB_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from v6_db_web.database_lock import DatabaseWriteLockError, database_write_lock  # noqa: E402
from v6_db_web.db import connect  # noqa: E402
from v6_db_web.migrations import apply_migrations  # noqa: E402
from v6_db_web.ocr_ingest import replay_ocr_json  # noqa: E402
from wedding_web.repository import Repository  # noqa: E402
from wedding_web.write_service import ManualReviewService  # noqa: E402


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def scan_record(order_no: str) -> dict:
    return {
        "imageName": f"{order_no}.png",
        "departmentCode": "4001",
        "orderNo": order_no,
        "category": "婚約",
        "orderDate": "2026.08.25",
        "details": [
            {
                "giftCode": "720001",
                "listPrice": 800,
                "quantity": 2,
                "totalAmount": 1600,
                "discountAmount": 1400,
            }
        ],
        "summaryTotals": {
            "quantity": 2,
            "totalAmount": 1600,
            "discountAmount": 1400,
        },
        "detailCodeColumnVisible": True,
        "voided": False,
        "statusLevel": "success",
        "errorReason": "",
    }


class UnifiedDatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "orders.db"
        connection = connect(self.database)
        apply_migrations(connection, PROJECT_ROOT / "migrations")
        connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def replay(self, record: dict, name: str):
        path = self.root / name
        path.write_text(
            json.dumps({"version": 2, "orders": [record]}, ensure_ascii=False),
            encoding="utf-8",
        )
        connection = connect(self.database)
        try:
            return replay_ocr_json(
                connection,
                path,
                "2026-08-19",
                "2026-08-25",
                job_type="daily_7_days",
            )
        finally:
            connection.close()

    def add_pending(self, review_id: int, order_no: str) -> None:
        value = timestamp()
        image_dir = self.root / "data" / "images" / "pending_review"
        image_dir.mkdir(parents=True, exist_ok=True)
        image = image_dir / f"{review_id}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nunified-test")
        connection = connect(self.database)
        try:
            connection.execute(
                "INSERT INTO scan_jobs(job_type,status,date_from,date_to,started_at,created_at,updated_at) VALUES('daily_7_days','completed','2026-08-19','2026-08-25',?,?,?)",
                (value, value, value),
            )
            job_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO scan_items(
                  scan_job_id,source_key,image_name,order_no,status,result_code,
                  image_relative_path,image_status,raw_ocr_payload_json,
                  validation_json,reason,created_at
                ) VALUES(?,?,?,?, 'pending_review','pending',?,'available','{}','{}','待確認',?)
                """,
                (job_id, f"unified:{review_id}", image.name, order_no, f"images/pending_review/{image.name}", value),
            )
            scan_item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """
                INSERT INTO review_queue(
                  id,source_key,order_no,scan_item_id,status,reason,raw_payload_json,
                  first_seen_at,last_seen_at,created_at,updated_at
                ) VALUES(?,?,?,?, 'pending','待確認','{}',?,?,?,?)
                """,
                (review_id, f"unified:{review_id}", order_no, scan_item_id, value, value, value, value),
            )
            connection.execute(
                """
                INSERT INTO web_pending_images(
                  review_queue_id,relative_path,sha256,copied_at,image_status
                ) VALUES(?,?,?,?, 'available')
                """,
                (review_id, f"images/pending_review/{image.name}", "A" * 64, value),
            )
            connection.commit()
        finally:
            connection.close()

    def test_scan_write_is_immediately_visible_to_website(self):
        summary = self.replay(scan_record("GOU26080041"), "scan.json")
        self.assertEqual(summary.success_count, 1)
        result = Repository(self.database).orders(keyword="GOU26080041")
        self.assertEqual(result["total"], 1)

    def test_manual_write_is_unchanged_when_scanner_reads_same_order(self):
        order_no = "GOU26080042"
        self.add_pending(1, order_no)
        payload = {
            "order_no": order_no,
            "order_date": "2026-08-25",
            "category": "婚約",
            "department_code": "4001",
            "customer_name": "人工確認客戶",
            "store_code": "SR",
            "items": [
                {
                    "gift_code": "720001",
                    "product_name": "人工品名",
                    "list_price": 800,
                    "quantity": 2,
                    "total_amount": 1600,
                    "discount_amount": 1400,
                }
            ],
        }
        ManualReviewService(self.database).process(1, "confirm", payload, "整合測試")
        summary = self.replay(scan_record(order_no), "same_scan.json")
        self.assertEqual(summary.unchanged_count, 1)
        connection = sqlite3.connect(self.database)
        try:
            versions = connection.execute(
                """
                SELECT COUNT(*) FROM order_versions v
                JOIN orders o ON o.id=v.order_id WHERE o.order_no=?
                """,
                (order_no,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(versions, 1)

    def test_scanner_and_website_use_the_same_write_lock(self):
        self.add_pending(1, "GOU26090003")
        errors = []

        def attempt_write():
            try:
                ManualReviewService(self.database).process(1, "hold", None, "busy")
            except Exception as error:
                errors.append(error)

        with database_write_lock(self.database, "scanner test"):
            thread = threading.Thread(target=attempt_write)
            thread.start()
            thread.join(timeout=3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DatabaseWriteLockError)


class UnifiedProductionDatabaseInvariants(unittest.TestCase):
    def test_only_orders_db_is_active_and_manual_order_is_unique(self):
        database = PROJECT_ROOT / "data" / "orders.db"
        self.assertTrue(database.is_file())
        self.assertFalse((PROJECT_ROOT / "web_v1" / "data" / "orders_web.db").exists())
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE order_no='GOU26080250'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
