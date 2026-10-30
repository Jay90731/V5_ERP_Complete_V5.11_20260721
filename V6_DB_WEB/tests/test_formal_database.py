from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests.helpers import load_history, save_history, write_fixture
from v6_db_web import formal_database
from v6_db_web.db import connect
from v6_db_web.formal_database import (
    FORMAL_CONFIRMATION,
    RESTORE_CONFIRMATION,
    create_formal_backup,
    apply_formal_store_migration,
    import_formal_v5,
    initialize_formal_database,
    reject_formal_path,
    restore_formal_database,
    source_baseline,
    validate_formal_database,
)


class FormalDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "V5.10"
        self.state = write_fixture(self.source)
        self.database = self.root / "data" / "orders.db"
        self.test_database = self.root / "data" / "orders_test.db"
        self.test_database.parent.mkdir(parents=True)
        self.test_database.write_bytes(b"do-not-change-test-database")
        self.test_before = self.test_database.read_bytes()
        self.expected = source_baseline(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self):
        return initialize_formal_database(
            self.database,
            ROOT / "migrations",
            self.root,
            FORMAL_CONFIRMATION,
        )

    def test_initialization_requires_confirmation_and_never_copies_test_db(self):
        with self.assertRaises(PermissionError):
            initialize_formal_database(
                self.database, ROOT / "migrations", self.root, "wrong"
            )
        result = self.initialize()
        self.assertTrue(result["created_from_migrations"])
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(self.test_database.read_bytes(), self.test_before)
        connection = connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT setting_value FROM system_settings WHERE setting_key='database_mode'"
                ).fetchone()[0],
                "formal",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM database_migrations"
                ).fetchone()[0],
                7,
            )
        finally:
            connection.close()

    def test_test_general_mode_rejects_orders_db(self):
        with self.assertRaisesRegex(ValueError, "cannot write orders.db"):
            reject_formal_path(self.database, self.root)
        self.assertEqual(
            reject_formal_path(self.test_database, self.root),
            self.test_database.resolve(),
        )

    def test_two_imports_are_idempotent_and_pending_has_scan_items(self):
        self.initialize()
        with patch.object(formal_database, "EXPECTED_BASELINE", self.expected):
            first = import_formal_v5(self.database, self.source, self.root)
            second = import_formal_v5(self.database, self.source, self.root)
            validation = validate_formal_database(
                self.database, self.source, self.root
            )
        self.assertEqual(first["import"]["inserted_orders"], 2)
        self.assertEqual(first["import"]["inserted_versions"], 2)
        self.assertEqual(first["import"]["inserted_items"], 3)
        self.assertEqual(second["import"]["inserted_orders"], 0)
        self.assertEqual(second["import"]["inserted_versions"], 0)
        self.assertEqual(second["import"]["inserted_items"], 0)
        self.assertTrue(validation["passed"], validation["checks"])
        connection = connect(self.database)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM scan_items WHERE result_code='pending'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE store_id IS NOT NULL"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()
        self.assertEqual(self.test_database.read_bytes(), self.test_before)

    def test_invalid_import_rolls_back_entire_batch(self):
        self.initialize()
        history = load_history(self.state)
        history["orders"]["GOU26070002"]["current_version"]["details"][0][
            "quantity"
        ] = "bad"
        history["orders"]["GOU26070002"]["versions"][0]["order"]["details"][0][
            "quantity"
        ] = "bad"
        save_history(self.state, history)
        with self.assertRaises(ValueError):
            import_formal_v5(self.database, self.source, self.root)
        connection = connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM order_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM order_items").fetchone()[0], 0)
        finally:
            connection.close()

    def test_backup_and_atomic_restore(self):
        self.initialize()
        with patch.object(formal_database, "EXPECTED_BASELINE", self.expected):
            import_formal_v5(self.database, self.source, self.root)
        backup = create_formal_backup(self.database, self.root)
        connection = connect(self.database)
        try:
            connection.execute(
                """
                INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at)
                VALUES ('restore_test','test','marker','test','{}','2026-08-03T00:00:00+00:00')
                """
            )
            connection.commit()
        finally:
            connection.close()
        result = restore_formal_database(
            self.database,
            backup["backup"],
            self.root,
            RESTORE_CONFIRMATION,
        )
        self.assertTrue(result["validation"]["valid"])
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE event_type='restore_test'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(len(connection.execute("PRAGMA foreign_key_check").fetchall()), 0)
        finally:
            connection.close()
        self.assertTrue(Path(result["pre_restore_backup"]["backup"]).is_file())

    def test_formal_sr_migration_backs_up_audits_and_is_idempotent(self):
        self.initialize()
        with patch.object(formal_database, "EXPECTED_BASELINE", self.expected):
            import_formal_v5(self.database, self.source, self.root)
        before = {}
        connection = connect(self.database)
        try:
            for table in ("orders", "order_versions", "order_items"):
                before[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        finally:
            connection.close()

        first = apply_formal_store_migration(
            self.database, ROOT / "migrations", self.root
        )
        second = apply_formal_store_migration(
            self.database, ROOT / "migrations", self.root
        )
        self.assertTrue(first["passed"])
        self.assertTrue(second["passed"])
        self.assertTrue(Path(first["backup"]["backup"]).is_file())
        connection = connect(self.database)
        try:
            after = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in ("orders", "order_versions", "order_items")
            }
            self.assertEqual(after, before)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0],
                1,
            )
            self.assertGreaterEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE event_type='formal_sr_store_update'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
