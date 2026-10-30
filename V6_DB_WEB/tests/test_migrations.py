from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.db import connect
from v6_db_web.migrations import apply_migrations


EXPECTED_TABLES = {
    "orders",
    "order_versions",
    "order_items",
    "scan_jobs",
    "scan_items",
    "review_queue",
    "screenshots",
    "products",
    "stores",
    "store_department_mappings",
    "audit_logs",
    "system_settings",
    "database_migrations",
    "backup_records",
    "web_pending_images",
    "web_manual_review_events",
    "web_order_version_metadata",
    "web_order_item_metadata",
    "bhq_import_runs",
    "bhq_store_aliases",
    "bhq_order_version_metadata",
}


class MigrationTests(unittest.TestCase):
    def test_schema_and_repeatable_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "orders.db")
            first = apply_migrations(connection, ROOT / "migrations")
            second = apply_migrations(connection, ROOT / "migrations")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(EXPECTED_TABLES.issubset(tables))
            self.assertEqual(first[0]["status"], "applied")
            self.assertEqual(second[0]["status"], "already_applied")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM database_migrations"
                ).fetchone()[0],
                7,
            )
            screenshot_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(screenshots)")
            }
            self.assertIn("image_status", screenshot_columns)
            store_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(stores)")
            }
            self.assertIn("status", store_columns)
            version_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(order_versions)")
            }
            self.assertIn("discounted_total_amount", version_columns)
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT store_code,store_name,status FROM stores"
                ).fetchone()),
                ("SR", "SR", "active"),
            )
            connection.close()

    def test_screenshot_path_deduplication_and_pending_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "orders.db")
            apply_migrations(connection, ROOT / "migrations")
            now = "2026-07-31T00:00:00+00:00"
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO screenshots(
                        sha256, relative_path, image_role,
                        protected_until_review, created_at
                    ) VALUES (?, ?, 'ocr_temp', 0, ?)
                    """,
                    ("a" * 64, r"C:\absolute\image.png", now),
                )
            connection.execute(
                """
                INSERT INTO screenshots(
                    sha256, relative_path, image_role,
                    protected_until_review, created_at
                ) VALUES (?, ?, 'pending_review', 1, ?)
                """,
                ("b" * 64, "pending/image.png", now),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO screenshots(
                        sha256, relative_path, image_role,
                        protected_until_review, created_at
                    ) VALUES (?, ?, 'ocr_temp', 0, ?)
                    """,
                    ("b" * 64, "temp/duplicate.png", now),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM screenshots WHERE sha256 = ?", ("b" * 64,)
                )
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
