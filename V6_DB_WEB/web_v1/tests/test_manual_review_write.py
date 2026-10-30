from __future__ import annotations

import json
import gc
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wedding_web.write_service import (  # noqa: E402
    DatabaseWriteLockError,
    ManualReviewService,
    database_write_lock,
    ensure_manual_schema,
)
from wedding_web.repository import Repository  # noqa: E402
from wedding_web.server import Handler, WeddingOrderServer  # noqa: E402


CORE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE stores(
  id INTEGER PRIMARY KEY, store_code TEXT NOT NULL UNIQUE, store_name TEXT NOT NULL,
  active INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE orders(
  id INTEGER PRIMARY KEY, order_no TEXT NOT NULL UNIQUE, current_version_id INTEGER,
  current_content_hash TEXT NOT NULL, order_date TEXT NOT NULL, category TEXT NOT NULL,
  department_code TEXT NOT NULL, store_id INTEGER, is_voided INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'confirmed', first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(store_id) REFERENCES stores(id),
  FOREIGN KEY(current_version_id) REFERENCES order_versions(id)
);
CREATE TABLE order_versions(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
  content_hash TEXT NOT NULL, order_date TEXT NOT NULL, category TEXT NOT NULL,
  department_code TEXT NOT NULL, is_voided INTEGER NOT NULL DEFAULT 0,
  detail_count INTEGER NOT NULL, total_quantity INTEGER NOT NULL,
  total_list_amount INTEGER NOT NULL, total_discount_amount INTEGER NOT NULL,
  source_system TEXT NOT NULL, source_version_id TEXT, source_payload_json TEXT NOT NULL,
  effective_at TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
  UNIQUE(order_id,version_no), UNIQUE(order_id,content_hash)
);
CREATE TABLE products(
  id INTEGER PRIMARY KEY, gift_code TEXT NOT NULL UNIQUE, product_name TEXT,
  active INTEGER NOT NULL, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE order_items(
  id INTEGER PRIMARY KEY, order_version_id INTEGER NOT NULL, line_no INTEGER NOT NULL,
  gift_code TEXT NOT NULL, product_id INTEGER, list_price INTEGER NOT NULL,
  quantity INTEGER NOT NULL, total_amount INTEGER NOT NULL, discount_amount INTEGER NOT NULL,
  item_hash TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(order_version_id) REFERENCES order_versions(id) ON DELETE CASCADE,
  FOREIGN KEY(product_id) REFERENCES products(id), UNIQUE(order_version_id,line_no)
);
CREATE TABLE scan_jobs(
  id INTEGER PRIMARY KEY, job_type TEXT, status TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE scan_items(
  id INTEGER PRIMARY KEY, scan_job_id INTEGER NOT NULL, source_key TEXT NOT NULL,
  image_name TEXT, temporary_key TEXT, order_no TEXT, order_id INTEGER,
  order_version_id INTEGER, status TEXT NOT NULL, result_code TEXT NOT NULL,
  screenshot_id INTEGER, image_hash TEXT, image_relative_path TEXT, image_status TEXT,
  raw_ocr_payload_json TEXT NOT NULL, validation_json TEXT NOT NULL, reason TEXT,
  started_at TEXT, finished_at TEXT, processed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE review_queue(
  id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, order_no TEXT,
  scan_item_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL,
  processing_note TEXT, raw_payload_json TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, resolved_at TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, FOREIGN KEY(scan_item_id) REFERENCES scan_items(id)
);
CREATE TABLE screenshots(
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, relative_path TEXT UNIQUE,
  order_version_id INTEGER, scan_item_id INTEGER, image_role TEXT, file_size INTEGER,
  captured_at TEXT, protected_until_review INTEGER, created_at TEXT, image_status TEXT
);
CREATE TABLE audit_logs(
  id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, entity_type TEXT NOT NULL,
  entity_id TEXT, actor TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE backup_records(
  id INTEGER PRIMARY KEY, backup_type TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE,
  sha256 TEXT, database_size INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL,
  verified_at TEXT, restored_at TEXT, notes TEXT
);
CREATE TABLE image_cleanup_runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  retention_days INTEGER, deleted_count INTEGER, missing_count INTEGER,
  protected_count INTEGER, failed_count INTEGER, status TEXT, details_json TEXT
);
CREATE TABLE web_schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE web_order_metadata(
  order_id INTEGER PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
  customer_name TEXT, customer_source TEXT, updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def order_payload(order_no: str = "GOU26080001", quantity: int = 2, *, multi: bool = False):
    items = [
        {
            "gift_code": "700001",
            "product_name": "悅藏禮盒",
            "quantity": quantity,
            "list_price": 500,
            "total_amount": 500 * quantity,
            "discount_amount": 450 * quantity,
        }
    ]
    if multi:
        items.append(
            {
                "gift_code": "700002",
                "product_name": "星願禮盒",
                "quantity": 3,
                "list_price": 600,
                "total_amount": 1800,
                "discount_amount": 1650,
            }
        )
    return {
        "order_no": order_no,
        "order_date": "2026-08-20",
        "category": "婚約",
        "department_code": "4001",
        "customer_name": "王小姐",
        "store_code": "SR",
        "items": items,
    }


class ManualReviewWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data" / "images" / "pending_review").mkdir(parents=True)
        self.database = self.root / "data" / "orders.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(CORE_SCHEMA)
        connection.execute(
            "INSERT INTO stores VALUES(1,'SR','SR',1,?,?, 'active')", (now(), now())
        )
        ensure_manual_schema(connection)
        connection.commit()
        connection.close()
        self.add_pending(1, "GOU26080001")

    def tearDown(self):
        # Embedded Python on Windows can release the HTTP test's SQLite handle
        # a fraction later than server_close(). Retry cleanup so the standalone
        # self-test does not report a false failure on WinError 32.
        for attempt in range(10):
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if attempt == 9:
                    raise
                gc.collect()
                time.sleep(0.1)

    def add_pending(self, identity: int, order_no: str) -> None:
        timestamp = now()
        image = self.root / "data" / "images" / "pending_review" / f"{identity}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nmanual-review-fixture")
        raw = {
            "orderNo": order_no,
            "orderDate": "2026.08.20",
            "category": "婚約",
            "departmentCode": "4001",
            "details": [],
        }
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT OR IGNORE INTO scan_jobs VALUES(1,'daily_7d','completed',?,?)",
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO scan_items(
              id,scan_job_id,source_key,image_name,order_no,status,result_code,
              image_relative_path,image_status,raw_ocr_payload_json,validation_json,
              reason,created_at
            ) VALUES(?,1,?,?,?,'pending_review','pending',?,'available',?,'{}','待確認',?)
            """,
            (
                identity, f"fixture:{identity}", image.name, order_no,
                f"images/pending_review/{image.name}", json.dumps(raw), timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_queue(
              id,source_key,order_no,scan_item_id,status,reason,processing_note,
              raw_payload_json,first_seen_at,last_seen_at,resolved_at,created_at,updated_at
            ) VALUES(?,?,?,?, 'pending','待確認',NULL,?,?,?,NULL,?,?)
            """,
            (
                identity, f"fixture:{identity}", order_no, identity,
                json.dumps(raw), timestamp, timestamp, timestamp, timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO web_pending_images(
              review_queue_id,relative_path,sha256,source_relative_path,copied_at,
              processed_at,retained_until,image_status
            ) VALUES(?,?,?,NULL,?,NULL,NULL,'available')
            """,
            (identity, f"images/pending_review/{image.name}", "A" * 64, timestamp),
        )
        connection.commit()
        connection.close()

    def scalar(self, sql: str, params=()):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, params).fetchone()[0]
        finally:
            connection.close()

    def test_new_multi_item_order_is_written_and_review_resolved(self):
        result = ManualReviewService(self.database).process(
            1, "confirm", order_payload(multi=True), "人工核對完成"
        )
        self.assertEqual(result["result"], "new")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM orders"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_versions"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_items"), 2)
        self.assertEqual(self.scalar("SELECT status FROM review_queue WHERE id=1"), "resolved")
        self.assertEqual(self.scalar("SELECT store_id FROM orders"), 1)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM web_order_item_metadata"), 2
        )

    def test_duplicate_identical_order_does_not_add_order_or_version(self):
        service = ManualReviewService(self.database)
        service.process(1, "confirm", order_payload(), "first")
        self.add_pending(2, "GOU26080001")
        result = service.process(2, "confirm", order_payload(), "same")
        self.assertEqual(result["result"], "unchanged")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM orders"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_versions"), 1)

    def test_existing_order_change_adds_version_and_preserves_old_items(self):
        service = ManualReviewService(self.database)
        service.process(1, "confirm", order_payload(quantity=2), "first")
        first_version = self.scalar("SELECT current_version_id FROM orders")
        self.add_pending(2, "GOU26080001")
        result = service.process(2, "confirm", order_payload(quantity=4), "changed")
        second_version = self.scalar("SELECT current_version_id FROM orders")
        self.assertEqual(result["result"], "changed")
        self.assertNotEqual(first_version, second_version)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_versions"), 2)
        self.assertEqual(
            self.scalar(
                "SELECT quantity FROM order_items WHERE order_version_id=?", (first_version,)
            ),
            2,
        )
        self.assertEqual(
            self.scalar(
                "SELECT quantity FROM order_items WHERE order_version_id=?", (second_version,)
            ),
            4,
        )

    def test_exclude_does_not_create_or_archive_order(self):
        result = ManualReviewService(self.database).process(1, "exclude", None, "不納入")
        self.assertEqual(result["result"], "excluded")
        self.assertEqual(self.scalar("SELECT status FROM review_queue WHERE id=1"), "excluded")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM orders"), 0)

    def test_hold_keeps_review_pending(self):
        result = ManualReviewService(self.database).process(1, "hold", None, "等主管確認")
        self.assertEqual(result["result"], "held")
        self.assertEqual(self.scalar("SELECT status FROM review_queue WHERE id=1"), "pending")
        self.assertEqual(
            self.scalar("SELECT processing_note FROM review_queue WHERE id=1"),
            "等主管確認",
        )

    def test_failure_rolls_back_order_version_items_review_and_audit(self):
        def fail(phase: str):
            if phase == "after_order_write":
                raise RuntimeError("injected failure")

        with self.assertRaises(RuntimeError):
            ManualReviewService(self.database, fault_injector=fail).process(
                1, "confirm", order_payload(), "rollback"
            )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM orders"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_versions"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM order_items"), 0)
        self.assertEqual(self.scalar("SELECT status FROM review_queue WHERE id=1"), "pending")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_logs"), 0)

    def test_each_write_creates_verified_backup_and_audit_values(self):
        result = ManualReviewService(self.database).process(
            1, "confirm", order_payload(), "backup test"
        )
        backup = self.root / result["backup"]
        self.assertTrue(backup.is_file())
        connection = sqlite3.connect(backup)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            connection.close()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM backup_records"), 1)
        details = json.loads(self.scalar("SELECT details_json FROM audit_logs LIMIT 1"))
        self.assertIn("original_value", details)
        self.assertIn("modified_value", details)
        self.assertEqual(details["source"], "website_manual_review")

    def test_write_lock_rejects_concurrent_manual_action(self):
        service = ManualReviewService(self.database)
        errors = []

        def attempt_write():
            try:
                service.process(1, "hold", None, "busy")
            except Exception as error:
                errors.append(error)

        with database_write_lock(self.database, "test holder"):
            thread = threading.Thread(target=attempt_write)
            thread.start()
            thread.join(timeout=3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DatabaseWriteLockError)
        self.assertEqual(self.scalar("SELECT status FROM review_queue WHERE id=1"), "pending")

    def test_processed_image_is_retained_for_at_least_seven_days(self):
        ManualReviewService(self.database).process(1, "exclude", None, "retain image")
        retained = self.scalar(
            "SELECT retained_until FROM web_pending_images WHERE review_queue_id=1"
        )
        self.assertGreater(
            datetime.fromisoformat(retained),
            datetime.now(timezone.utc) + __import__("datetime").timedelta(days=6),
        )
        self.assertTrue(
            (self.root / "data" / "images" / "pending_review" / "1.png").is_file()
        )

    def test_processed_review_image_is_deleted_only_after_retention(self):
        ManualReviewService(self.database).process(1, "exclude", None, "cleanup")
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE web_pending_images SET retained_until='2020-01-01T00:00:00+00:00' WHERE review_queue_id=1"
        )
        connection.commit()
        connection.close()
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "cleanup_success_images.py"),
                "--database",
                str(self.database),
                "--image-root",
                str(self.root),
                "--retention-days",
                "7",
                "--apply",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8", errors="replace")
        )
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["processed_review_image_count"], 1)
        self.assertEqual(
            self.scalar(
                "SELECT image_status FROM web_pending_images WHERE review_queue_id=1"
            ),
            "deleted",
        )
        self.assertFalse(
            (self.root / "data" / "images" / "pending_review" / "1.png").exists()
        )

    def test_database_reopen_preserves_manual_write(self):
        ManualReviewService(self.database).process(1, "confirm", order_payload(), "persist")
        reopened = sqlite3.connect(self.database)
        try:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(
                reopened.execute("SELECT COUNT(*) FROM web_manual_review_events").fetchone()[0],
                1,
            )
            self.assertEqual(reopened.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            reopened.close()

    def test_http_confirm_action_writes_and_returns_updated_metrics(self):
        service = ManualReviewService(self.database)
        server = WeddingOrderServer(
            ("127.0.0.1", 0), Handler, Repository(self.database), ROOT / "static", service
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "action": "confirm",
                    "order": order_payload(),
                    "note": "HTTP test",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/reviews/1/actions",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Wedding-Write": "1",
                },
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["result"], "new")
            self.assertEqual(payload["metrics"]["formal_orders"], 1)
            self.assertEqual(payload["metrics"]["pending"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
