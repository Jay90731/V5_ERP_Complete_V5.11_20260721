from __future__ import annotations

import copy
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_excel import parse_bhq_xls
from v6_db_web.bhq_import_service import BhqImportError, import_bhq


SOURCE = Path(r"C:\Users\USER\Downloads\202607ALL 訂單明細表.xls")
DATABASE = ROOT / "data" / "orders_bhq_test.db"
CORE_TABLES = ("orders", "order_versions", "order_items", "stores", "products")


def memory_clone() -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(":memory:")
    try:
        source.backup(target)
    finally:
        source.close()
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    return target


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CORE_TABLES
    }


class BhqImportAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parsed = parse_bhq_xls(SOURCE)

    def setUp(self) -> None:
        self.connection = memory_clone()

    def tearDown(self) -> None:
        self.connection.close()

    def test_actual_import_shape_and_special_rows(self) -> None:
        self.assertEqual(self.parsed["order_count"], 257)
        self.assertEqual(self.parsed["valid_item_count"], 551)
        self.assertEqual(self.parsed["empty_product_code_count"], 352)
        self.assertEqual(self.parsed["store_count"], 18)
        self.assertEqual(
            sum(item["quantity"] == 0 for order in self.parsed["orders"] for item in order["items"]),
            15,
        )
        self.assertEqual(
            [order["category"] for order in self.parsed["orders"]].count(".."), 3
        )
        bhq = self.connection.execute(
            "SELECT COUNT(*) FROM order_versions WHERE source_system='BHQ'"
        ).fetchone()[0]
        self.assertEqual(bhq, 257)
        zero_rows = self.connection.execute(
            """
            SELECT COUNT(*) FROM order_items i
            JOIN order_versions v ON v.id=i.order_version_id
            WHERE v.source_system='BHQ' AND i.quantity=0
            """
        ).fetchone()[0]
        self.assertEqual(zero_rows, 15)

    def test_same_file_is_idempotent(self) -> None:
        before = counts(self.connection)
        result = import_bhq(self.connection, self.parsed)
        self.assertEqual(counts(self.connection), before)
        self.assertEqual(result.inserted_orders, 0)
        self.assertEqual(result.inserted_versions, 0)
        self.assertEqual(result.inserted_items, 0)
        self.assertEqual(result.inserted_stores, 0)
        self.assertEqual(result.inserted_products, 0)
        self.assertEqual(result.skipped_orders, 257)

    def test_modified_bhq_order_creates_one_version(self) -> None:
        changed = copy.deepcopy(self.parsed)
        target = changed["orders"][0]
        target["items"][0]["product_name"] += "（版本測試）"
        before = counts(self.connection)
        previous_versions = self.connection.execute(
            "SELECT COUNT(*) FROM order_versions v JOIN orders o ON o.id=v.order_id WHERE o.order_no=?",
            (target["order_no"],),
        ).fetchone()[0]
        result = import_bhq(self.connection, changed)
        after = counts(self.connection)
        self.assertEqual(result.inserted_orders, 0)
        self.assertEqual(result.inserted_versions, 1)
        self.assertEqual(after["order_versions"] - before["order_versions"], 1)
        self.assertEqual(after["orders"] - before["orders"], 0)
        current_versions = self.connection.execute(
            "SELECT COUNT(*) FROM order_versions v JOIN orders o ON o.id=v.order_id WHERE o.order_no=?",
            (target["order_no"],),
        ).fetchone()[0]
        self.assertEqual(current_versions, previous_versions + 1)

    def test_cross_system_collision_rolls_back_core_tables(self) -> None:
        collision_no = self.connection.execute(
            """
            SELECT o.order_no FROM orders o JOIN order_versions v ON v.id=o.current_version_id
            WHERE v.source_system<>'BHQ' ORDER BY o.id LIMIT 1
            """
        ).fetchone()[0]
        collision = copy.deepcopy(self.parsed)
        collision["orders"] = [collision["orders"][0]]
        collision["orders"][0]["order_no"] = collision_no
        before = counts(self.connection)
        with self.assertRaisesRegex(BhqImportError, "跨系統訂單撞號"):
            import_bhq(self.connection, collision)
        self.assertEqual(counts(self.connection), before)
        failed = self.connection.execute(
            "SELECT result,error_summary FROM bhq_import_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(failed["result"], "failed")
        self.assertIn("跨系統訂單撞號", failed["error_summary"])

    def test_fault_rolls_back_entire_batch(self) -> None:
        changed = copy.deepcopy(self.parsed)
        changed["orders"][0]["items"][0]["product_name"] += "（rollback）"
        before = counts(self.connection)

        def fail_after_first_order(stage: str, index: int) -> None:
            if stage == "after_order" and index == 1:
                raise RuntimeError("injected rollback test")

        with self.assertRaisesRegex(RuntimeError, "injected rollback test"):
            import_bhq(self.connection, changed, fault_injector=fail_after_first_order)
        self.assertEqual(counts(self.connection), before)
        failed = self.connection.execute(
            "SELECT result,error_summary FROM bhq_import_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(failed["result"], "failed")


if __name__ == "__main__":
    unittest.main()
