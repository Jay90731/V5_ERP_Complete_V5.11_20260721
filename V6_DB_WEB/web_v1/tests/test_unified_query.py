from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WEB_ROOT.parent
DATABASE = PROJECT_ROOT / "data" / "orders.db"
sys.path.insert(0, str(WEB_ROOT))

from wedding_web.repository import Repository  # noqa: E402


class UnifiedQueryAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = Repository(DATABASE)
        cls.hash_before = hashlib.sha256(DATABASE.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        assert hashlib.sha256(DATABASE.read_bytes()).hexdigest() == cls.hash_before

    def read(self):
        connection = sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def test_erp_scope_matches_legacy_scope_dynamically(self):
        with self.read() as connection:
            legacy = connection.execute(
                """
                WITH legacy_orders AS (
                  SELECT o.id,v.total_discount_amount amount
                  FROM orders o JOIN stores s ON s.id=o.store_id
                  JOIN order_versions v ON v.id=o.current_version_id
                  WHERE v.source_system<>'BHQ' AND o.department_code='4001'
                    AND s.store_code='SR' AND o.is_voided=0 AND o.status='confirmed'
                    AND EXISTS(SELECT 1 FROM order_items i
                               WHERE i.order_version_id=o.current_version_id)
                )
                SELECT COUNT(*) order_count,COALESCE(SUM(amount),0) amount
                FROM legacy_orders
                """
            ).fetchone()
            legacy_items = connection.execute(
                """
                SELECT COUNT(*) FROM orders o JOIN stores s ON s.id=o.store_id
                JOIN order_versions v ON v.id=o.current_version_id
                JOIN order_items i ON i.order_version_id=o.current_version_id
                WHERE v.source_system<>'BHQ' AND o.department_code='4001'
                  AND s.store_code='SR' AND o.is_voided=0 AND o.status='confirmed'
                """
            ).fetchone()[0]
        actual = self.repository.statistics(source_system="ERP")["overall"]
        rows = self.repository.orders(source_system="ERP", page_size=10)
        self.assertEqual(actual["order_count"], legacy["order_count"])
        self.assertEqual(actual["amount"], legacy["amount"])
        self.assertEqual(rows["total"], legacy_items)

    def test_bhq_and_unified_totals(self):
        erp = self.repository.statistics(source_system="ERP")["overall"]
        bhq = self.repository.statistics(source_system="BHQ")["overall"]
        all_sources = self.repository.statistics(source_system="ALL")["overall"]
        self.assertEqual(bhq["order_count"], 257)
        self.assertEqual(self.repository.orders(source_system="BHQ")["total"], 551)
        self.assertEqual(bhq["amount"], 8_742_030)
        self.assertEqual(all_sources["order_count"], erp["order_count"] + 257)
        self.assertEqual(all_sources["amount"], erp["amount"] + 8_742_030)

    def test_multi_item_bhq_amount_is_counted_once(self):
        with self.read() as connection:
            row = connection.execute(
                """
                SELECT o.order_no,COUNT(i.id) item_count,v.discounted_total_amount amount
                FROM orders o JOIN order_versions v ON v.id=o.current_version_id
                JOIN order_items i ON i.order_version_id=v.id
                WHERE v.source_system='BHQ'
                GROUP BY o.id HAVING COUNT(i.id)>1 LIMIT 1
                """
            ).fetchone()
        lines = self.repository.orders(
            source_system="BHQ", order_no=row["order_no"], page_size=100
        )
        stats = self.repository.statistics(
            source_system="BHQ", order_no=row["order_no"]
        )["overall"]
        self.assertEqual(lines["total"], row["item_count"])
        self.assertTrue(all(item["discounted_amount"] == row["amount"] for item in lines["items"]))
        self.assertEqual(stats["order_count"], 1)
        self.assertEqual(stats["amount"], row["amount"])

    def test_store_options_obey_source_scope(self):
        all_stores = {row["store_code"] for row in self.repository.filters("ALL")["stores"]}
        erp_stores = {row["store_code"] for row in self.repository.filters("ERP")["stores"]}
        bhq_stores = {row["store_code"] for row in self.repository.filters("BHQ")["stores"]}
        self.assertEqual(erp_stores, {"SR"})
        self.assertEqual(len(bhq_stores), 18)
        self.assertNotIn("SR", bhq_stores)
        self.assertEqual(all_stores, erp_stores | bhq_stores)

    def test_bhq_special_rows_and_names_are_preserved(self):
        with self.read() as connection:
            uncategorized = connection.execute(
                """SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id
                   WHERE v.source_system='BHQ' AND o.category='..'"""
            ).fetchone()[0]
            zero_quantity = connection.execute(
                """SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id
                   JOIN order_items i ON i.order_version_id=v.id
                   WHERE v.source_system='BHQ' AND i.quantity=0"""
            ).fetchone()[0]
            duplicate_rows = connection.execute(
                """
                SELECT COALESCE(SUM(n-1),0) FROM (
                  SELECT COUNT(*) n FROM orders o
                  JOIN order_versions v ON v.id=o.current_version_id
                  JOIN order_items i ON i.order_version_id=v.id
                  WHERE v.source_system='BHQ'
                  GROUP BY v.id,i.gift_code HAVING COUNT(*)>1
                )
                """
            ).fetchone()[0]
            missing_names = connection.execute(
                """
                SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id
                JOIN order_items i ON i.order_version_id=v.id
                LEFT JOIN web_order_item_metadata m ON m.order_item_id=i.id
                WHERE v.source_system='BHQ' AND COALESCE(NULLIF(m.product_name,''),'')=''
                """
            ).fetchone()[0]
        self.assertEqual(uncategorized, 3)
        self.assertEqual(zero_quantity, 15)
        self.assertEqual(duplicate_rows, 79)
        self.assertEqual(missing_names, 0)

    def test_filters_and_pagination_are_stable(self):
        first_page = self.repository.orders(source_system="BHQ", page=1, page_size=100)
        sample = first_page["items"][0]
        filtered = self.repository.orders(
            source_system="BHQ",
            store_code=sample["store_code"],
            date_from=sample["order_date"],
            date_to=sample["order_date"],
            category=sample["category"],
            product_code=sample["product_code"],
            product_name=sample["product_name"],
            page_size=100,
        )
        self.assertGreater(filtered["total"], 0)
        self.assertTrue(all(row["store_code"] == sample["store_code"] for row in filtered["items"]))
        self.assertTrue(all(row["order_date"] == sample["order_date"] for row in filtered["items"]))
        self.assertTrue(all(row["category"] == sample["category"] for row in filtered["items"]))
        seen = []
        for page in range(1, first_page["pages"] + 1):
            seen.extend(
                row["item_id"]
                for row in self.repository.orders(
                    source_system="BHQ", page=page, page_size=100
                )["items"]
            )
        self.assertEqual(len(seen), 551)
        self.assertEqual(len(set(seen)), 551)

    def test_frontend_keeps_raw_category_and_read_only_bhq_controls(self):
        javascript = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        html = (WEB_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("value === '..' ? '未分類'", javascript)
        self.assertIn("來源系統", html)
        self.assertNotIn("客戶姓名", html)


if __name__ == "__main__":
    unittest.main()
