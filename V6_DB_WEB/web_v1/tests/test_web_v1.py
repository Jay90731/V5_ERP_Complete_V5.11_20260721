from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent if (ROOT.parent / "src" / "v6_db_web").is_dir() else ROOT
DATABASE = PROJECT_ROOT / "data" / "orders.db"
sys.path.insert(0, str(ROOT))

from wedding_web.repository import Repository  # noqa: E402
from wedding_web.server import Handler, WeddingOrderServer  # noqa: E402


class WebRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = DATABASE
        cls.repository = Repository(cls.database)
        cls.expected_health = cls.repository.health()

    def test_health_and_formal_counts(self):
        health = self.repository.health()
        self.assertEqual(health["quick_check"], "ok")
        self.assertGreater(health["formal_orders"], 0)
        self.assertGreaterEqual(health["pending"], 0)
        self.assertTrue(health["manual_write_enabled"])

    def test_orders_are_paginated(self):
        result = self.repository.orders(page=1, page_size=20)
        self.assertGreaterEqual(result["total"], result["order_count"])
        self.assertEqual(result["order_count"], self.expected_health["formal_orders"])
        self.assertEqual(len(result["items"]), 20)
        self.assertGreater(result["pages"], 1)

    def test_order_number_and_product_search(self):
        first = self.repository.orders(page_size=10)["items"][0]
        by_order = self.repository.orders(keyword=first["order_no"], page_size=10)
        self.assertGreaterEqual(by_order["total"], 1)
        code = first["product_code"]
        by_product = self.repository.orders(keyword=code, page_size=10)
        self.assertGreaterEqual(by_product["total"], 1)

    def test_date_month_and_category_filters(self):
        filters = self.repository.filters()
        month = filters["months"][0]
        category = filters["categories"][0]
        monthly = self.repository.orders(month=month, page_size=10)
        categorized = self.repository.orders(category=category, page_size=10)
        self.assertTrue(all(item["order_date"].startswith(month) for item in monthly["items"]))
        self.assertTrue(all(item["category"] == category for item in categorized["items"]))

    def test_detail_preserves_all_items(self):
        result = self.repository.orders(source_system="BHQ", page_size=100)
        counts = {}
        for item in result["items"]:
            counts[item["order_no"]] = counts.get(item["order_no"], 0) + 1
        order_no = next(number for number, count in counts.items() if count > 1)
        expected = self.repository.orders(
            source_system="BHQ", order_no=order_no, page_size=100
        )["total"]
        detail = self.repository.order_detail(order_no)
        self.assertEqual(len(detail["items"]), expected)

    def test_pending_reviews_and_images(self):
        result = self.repository.reviews(status="pending", page_size=20)
        self.assertEqual(result["total"], self.expected_health["pending"])
        self.assertTrue(all(item["has_image"] for item in result["items"]))
        image = self.repository.review_image(result["items"][0]["id"])
        self.assertIsNotNone(image)
        self.assertTrue(image.is_file())

    def test_pending_review_detail_prefills_editable_order(self):
        review = self.repository.reviews(status="pending", page_size=10)["items"][0]
        detail = self.repository.review_detail(review["id"])
        self.assertEqual(detail["status"], "pending")
        self.assertTrue(detail["has_image"])
        self.assertTrue(detail["form"]["order_no"].startswith("GOU"))
        self.assertEqual(detail["form"]["store_code"], "SR")
        self.assertIsInstance(detail["form"]["items"], list)

    def test_statistics_and_gap_records(self):
        result = self.repository.statistics()
        self.assertEqual(
            result["overall"]["order_count"], self.expected_health["formal_orders"]
        )
        self.assertEqual(len(result["gaps"]), 21)
        self.assertGreater(len(result["categories"]), 0)

    def test_repository_connection_is_query_only(self):
        with self.repository.connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM orders")


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = Repository(DATABASE)
        cls.server = WeddingOrderServer(
            ("127.0.0.1", 0), Handler, repository, ROOT / "static"
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def get(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read(), response.headers.get_content_type()

    def test_index_and_health_api(self):
        status, html, content_type = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn("婚約訂單管理".encode("utf-8"), html)
        self.assertIn("確認並寫入正式訂單".encode("utf-8"), html)
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        expected = Repository(DATABASE).health()
        self.assertEqual(json.loads(body)["formal_orders"], expected["formal_orders"])

    def test_orders_reviews_statistics_api(self):
        for path in (
            "/api/orders?page=1&page_size=10",
            "/api/reviews?status=pending&page=1&page_size=10",
            "/api/statistics",
        ):
            status, body, content_type = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            self.assertTrue(json.loads(body))

    def test_pending_image_api(self):
        review = Repository(DATABASE).reviews(page_size=10)["items"][0]
        status, body, content_type = self.get(f"/api/review-images/{review['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_pending_review_detail_api(self):
        review = Repository(DATABASE).reviews(page_size=10)["items"][0]
        status, body, content_type = self.get(f"/api/reviews/{review['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body)["form"]["store_code"], "SR")


class ImageRetentionTests(unittest.TestCase):
    def test_only_old_success_image_is_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "orders.db"
            image_root = root / "images"
            (image_root / "runs").mkdir(parents=True)
            old_success = image_root / "runs" / "old_success.png"
            recent_success = image_root / "runs" / "recent_success.png"
            pending = image_root / "runs" / "pending.png"
            for path in (old_success, recent_success, pending):
                path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            recent = datetime.now(timezone.utc).isoformat()
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE scan_items(
                  id INTEGER PRIMARY KEY, status TEXT, result_code TEXT,
                  order_no TEXT, screenshot_id INTEGER, image_status TEXT
                );
                CREATE TABLE screenshots(
                  id INTEGER PRIMARY KEY, relative_path TEXT, captured_at TEXT,
                  created_at TEXT, protected_until_review INTEGER,
                  image_status TEXT CHECK(image_status IN('available','missing','archived','deleted')),
                  scan_item_id INTEGER
                );
                CREATE TABLE review_queue(
                  id INTEGER PRIMARY KEY, scan_item_id INTEGER,
                  order_no TEXT, status TEXT
                );
                CREATE TABLE image_cleanup_runs(
                  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
                  retention_days INTEGER, deleted_count INTEGER, missing_count INTEGER,
                  protected_count INTEGER, failed_count INTEGER, status TEXT,
                  details_json TEXT
                );
                CREATE TABLE audit_logs(
                  id INTEGER PRIMARY KEY, event_type TEXT, entity_type TEXT,
                  entity_id TEXT, actor TEXT, details_json TEXT, created_at TEXT
                );
                """
            )
            connection.executemany(
                "INSERT INTO screenshots VALUES(?,?,?,?,?,?,?)",
                [
                    (1, "runs/old_success.png", old, old, 0, "available", 1),
                    (2, "runs/recent_success.png", recent, recent, 0, "available", 2),
                    (3, "runs/pending.png", old, old, 1, "available", 3),
                ],
            )
            connection.executemany(
                "INSERT INTO scan_items VALUES(?,?,?,?,?,?)",
                [
                    (1, "imported", "new", "GOU1", 1, "available"),
                    (2, "imported", "new", "GOU2", 2, "available"),
                    (3, "pending_review", "pending", "GOU3", 3, "available"),
                ],
            )
            connection.execute("INSERT INTO review_queue VALUES(1,3,'GOU3','pending')")
            connection.commit()
            connection.close()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "cleanup_success_images.py"),
                    "--database",
                    str(database),
                    "--image-root",
                    str(image_root),
                    "--retention-days",
                    "7",
                    "--apply",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stderr.decode("utf-8", errors="replace"),
            )
            payload = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(payload["eligible_count"], 1)
            self.assertFalse(old_success.exists())
            self.assertTrue(recent_success.exists())
            self.assertTrue(pending.exists())


if __name__ == "__main__":
    unittest.main()
