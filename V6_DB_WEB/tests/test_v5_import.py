from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.db import connect
from v6_db_web.importer import import_v5
from v6_db_web.migrations import apply_migrations
from v6_db_web.validation import validate_v5_import

from tests.helpers import load_history, save_history, write_fixture


class V5ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "v5"
        self.state = write_fixture(self.source)
        self.database = self.root / "orders_test.db"
        self.connection = connect(self.database)
        apply_migrations(self.connection, ROOT / "migrations")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def count(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_first_import_preserves_data_and_assigns_sr_to_official_order(self):
        stats = import_v5(self.connection, self.source)
        self.assertEqual(stats.inserted_orders, 2)
        self.assertEqual(stats.inserted_versions, 2)
        self.assertEqual(stats.inserted_items, 3)
        self.assertEqual(stats.pending_inserted, 1)
        self.assertEqual(self.count("orders"), 2)
        self.assertEqual(self.count("order_versions"), 2)
        self.assertEqual(self.count("order_items"), 3)
        self.assertEqual(self.count("review_queue"), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM orders WHERE store_id IS NOT NULL"
            ).fetchone()[0],
            1,
        )
        assigned = self.connection.execute(
            """
            SELECT o.order_no,o.department_code,s.store_code
            FROM orders o JOIN stores s ON s.id=o.store_id
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in assigned],
            [("GOU26070001", "4001", "SR")],
        )
        self.assertEqual(self.count("stores"), 1)
        report = validate_v5_import(self.connection, self.source)
        self.assertTrue(report["passed"], report["differences"])

    def test_identical_rerun_does_not_duplicate_orders_or_versions(self):
        import_v5(self.connection, self.source)
        second = import_v5(self.connection, self.source)
        self.assertEqual(second.inserted_orders, 0)
        self.assertEqual(second.inserted_versions, 0)
        self.assertEqual(self.count("orders"), 2)
        self.assertEqual(self.count("order_versions"), 2)
        self.assertEqual(self.count("order_items"), 3)
        self.assertEqual(self.count("review_queue"), 1)

    def test_changed_order_creates_new_complete_version(self):
        import_v5(self.connection, self.source)
        history = load_history(self.state)
        entry = history["orders"]["GOU26070001"]
        entry["current_version"]["details"][0]["quantity"] = 5
        entry["current_version"]["details"][0]["total_amount"] = 4500
        entry["current_version"]["details"][0]["discount_amount"] = 4200
        entry["current_version_id"] = "source-v2"
        entry["updated_at"] = "2026-07-17T08:00:00+08:00"
        save_history(self.state, history)

        stats = import_v5(self.connection, self.source)
        self.assertEqual(stats.inserted_versions, 1)
        order_id = self.connection.execute(
            "SELECT id FROM orders WHERE order_no = 'GOU26070001'"
        ).fetchone()[0]
        versions = self.connection.execute(
            """
            SELECT id, version_no FROM order_versions
            WHERE order_id = ? ORDER BY version_no
            """,
            (order_id,),
        ).fetchall()
        self.assertEqual([row["version_no"] for row in versions], [1, 2])
        old_quantity = self.connection.execute(
            """
            SELECT quantity FROM order_items
            WHERE order_version_id = ? AND line_no = 1
            """,
            (versions[0]["id"],),
        ).fetchone()[0]
        new_quantity = self.connection.execute(
            """
            SELECT quantity FROM order_items
            WHERE order_version_id = ? AND line_no = 1
            """,
            (versions[1]["id"],),
        ).fetchone()[0]
        self.assertEqual(old_quantity, 2)
        self.assertEqual(new_quantity, 5)

    def test_pending_survives_when_next_import_does_not_contain_it(self):
        import_v5(self.connection, self.source)
        (self.state / "current_orders.json").write_text(
            json.dumps({"orders": []}), encoding="utf-8"
        )
        import_v5(self.connection, self.source)
        self.assertEqual(self.count("review_queue"), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM review_queue"
            ).fetchone()[0],
            "pending",
        )

    def test_database_reopens_with_complete_data(self):
        import_v5(self.connection, self.source)
        self.connection.close()
        self.connection = connect(self.database)
        self.assertEqual(self.count("orders"), 2)
        self.assertEqual(self.count("order_items"), 3)
        self.assertEqual(
            self.connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )

    def test_invalid_order_rolls_back_entire_import(self):
        history = load_history(self.state)
        history["orders"]["GOU26070002"]["current_version"]["details"][0][
            "quantity"
        ] = "not-a-number"
        history["orders"]["GOU26070002"]["versions"][0]["order"]["details"][0][
            "quantity"
        ] = "not-a-number"
        save_history(self.state, history)
        with self.assertRaises(ValueError):
            import_v5(self.connection, self.source)
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(self.count("order_versions"), 0)
        self.assertEqual(self.count("scan_jobs"), 0)


if __name__ == "__main__":
    unittest.main()
