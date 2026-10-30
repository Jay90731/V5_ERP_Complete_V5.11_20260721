from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from tools.r4_deployment_preflight import capture, compare, digest, inventory, retention, write_report
from v6_db_web.database_lock import DatabaseWriteLockError


@contextmanager
def write_connection(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def fixture(path):
    path.parent.mkdir(parents=True)
    with write_connection(path) as db:
        db.executescript("""
          CREATE TABLE system_settings(setting_key TEXT PRIMARY KEY,setting_value TEXT);
          INSERT INTO system_settings VALUES('database_mode','formal');
          CREATE TABLE database_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT,applied_at TEXT);
          INSERT INTO database_migrations VALUES(6,'006_fixture.sql','fixture','2026-08-01');
          CREATE TABLE orders(id INTEGER PRIMARY KEY,order_no TEXT UNIQUE,order_date TEXT,category TEXT,
            status TEXT,is_voided INTEGER,current_version_id INTEGER);
          CREATE TABLE order_versions(id INTEGER PRIMARY KEY,order_id INTEGER REFERENCES orders(id),
            version_no INTEGER,content_hash TEXT,source_system TEXT);
          CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_version_id INTEGER REFERENCES order_versions(id),
            line_no INTEGER,quantity INTEGER);
          CREATE TABLE review_queue(id INTEGER PRIMARY KEY,source_key TEXT,status TEXT,updated_at TEXT);
          CREATE TABLE web_manual_review_events(id INTEGER PRIMARY KEY,review_queue_id INTEGER,action TEXT,created_at TEXT);
          CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,event_type TEXT,created_at TEXT);
          CREATE TABLE scan_jobs(id INTEGER PRIMARY KEY,status TEXT);
          INSERT INTO orders VALUES(1,'ERP1','2026-08-01','A','confirmed',0,1);
          INSERT INTO order_versions VALUES(1,1,1,'HASH1','ERP');
          INSERT INTO order_items VALUES(1,1,1,2);
          INSERT INTO review_queue VALUES(1,'review1','excluded','2026-08-01');
          INSERT INTO web_manual_review_events VALUES(1,1,'excluded','2026-08-01');
          INSERT INTO audit_logs VALUES(1,'manual_excluded','2026-08-01');
        """)


class DeploymentPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'R3' / 'data' / 'orders.db'
        fixture(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_reads_every_table_without_writing(self):
        before = digest(self.source)
        result = inventory(self.source)
        self.assertEqual(result['integrity_check'], ['ok'])
        self.assertEqual(result['foreign_key_errors'], [])
        self.assertEqual(result['tables']['web_manual_review_events']['count'], 1)
        self.assertEqual(digest(self.source), before)

    def test_target_only_new_orders_versions_and_manual_events_are_detected(self):
        old = inventory(self.source)
        with write_connection(self.source) as db:
            db.execute("INSERT INTO orders VALUES(2,'ERP2','2026-08-31','A','confirmed',0,2)")
            db.execute("INSERT INTO order_versions VALUES(2,2,1,'HASH2','ERP')")
            db.execute("INSERT INTO web_manual_review_events VALUES(2,1,'confirmed','2026-08-31')")
        result = compare(inventory(self.source), old)
        self.assertEqual(result['orders_and_versions']['orders']['target_only'], ['ERP2'])
        self.assertEqual(len(result['orders_and_versions']['versions']['target_only']), 1)
        self.assertEqual(result['tables']['web_manual_review_events']['target_only_keys'], ['[2]'])

    def test_equal_counts_do_not_hide_changed_manual_decisions(self):
        old = inventory(self.source)
        with write_connection(self.source) as db:
            db.execute("UPDATE review_queue SET status='resolved' WHERE id=1")
        result = compare(inventory(self.source), old)
        self.assertEqual(result['tables']['review_queue']['target_count'], 1)
        self.assertEqual(result['tables']['review_queue']['different_row_keys'], ['[1]'])

    def test_same_order_version_number_different_hash_is_conflict(self):
        old = inventory(self.source)
        with write_connection(self.source) as db:
            db.execute("UPDATE order_versions SET content_hash='CONFLICT' WHERE id=1")
        changes = compare(inventory(self.source), old)
        self.assertEqual(len(changes['orders_and_versions']['versions']['conflicts']), 1)

    def test_schema_addition_uses_original_column_projection(self):
        original = inventory(self.source)
        with write_connection(self.source) as db:
            db.execute('ALTER TABLE order_versions ADD COLUMN discounted_total_amount INTEGER')
            db.execute("INSERT INTO audit_logs VALUES(2,'new_backup','2026-08-31')")
        self.assertTrue(retention(original, inventory(self.source, original))['passed'])
        self.assertFalse(retention(original, inventory(self.source))['passed'])

    def test_deleting_or_overwriting_original_data_fails_retention(self):
        original = inventory(self.source)
        with write_connection(self.source) as db:
            db.execute('DELETE FROM web_manual_review_events')
            db.execute("UPDATE review_queue SET status='pending'")
        result = retention(original, inventory(self.source, original))
        self.assertFalse(result['passed'])
        self.assertFalse(result['tables']['web_manual_review_events']['passed'])
        self.assertFalse(result['tables']['review_queue']['passed'])

    def test_missing_database_does_not_create_file_or_directory(self):
        missing = self.base / 'missing' / 'orders.db'
        with self.assertRaises(FileNotFoundError):
            inventory(missing)
        self.assertFalse(missing.parent.exists())

    def test_capture_includes_wal_and_assets_without_source_sqlite_writes(self):
        asset = self.source.parent / 'images' / 'pending.png'
        asset.parent.mkdir()
        asset.write_bytes(b'synthetic-image')
        config = self.source.parent.parent / 'phase3_config.json'
        config.write_text('{"database_path":"data/orders.db"}', encoding='utf-8')
        keeper = sqlite3.connect(self.source)
        try:
            keeper.execute('PRAGMA journal_mode=WAL')
            keeper.execute("INSERT INTO audit_logs VALUES(2,'new_in_wal','2026-08-31')")
            keeper.commit()
            before = digest(self.source)
            destination = self.base / 'export'
            result = capture(self.source.parent.parent, destination)
            self.assertEqual(result['status'], 'verified_capture')
            self.assertEqual(digest(self.source), before)
            self.assertEqual(inventory(destination / 'latest_r3.sqlite')['tables']['audit_logs']['count'], 2)
            self.assertEqual((destination / 'assets/data/images/pending.png').read_bytes(), b'synthetic-image')
            self.assertEqual(digest(destination / 'assets/phase3_config.json'), digest(config))
            self.assertTrue(result['configurations_verified'])
            self.assertFalse((destination / 'data/orders.db').exists())
        finally:
            keeper.close()

    def test_capture_refuses_existing_output_or_output_within_source(self):
        output = self.base / 'existing'
        output.mkdir()
        with self.assertRaises(FileExistsError):
            capture(self.source.parent.parent, output)
        with self.assertRaises(ValueError):
            capture(self.source.parent.parent, self.source.parent / 'export')

    def test_capture_rejects_running_job_without_export(self):
        with write_connection(self.source) as db:
            db.execute("INSERT INTO scan_jobs VALUES(1,'running')")
        with self.assertRaises(RuntimeError):
            capture(self.source.parent.parent, self.base / 'export')
        self.assertFalse((self.base / 'export').exists())

    def test_capture_rejects_bad_marker_without_export(self):
        with write_connection(self.source) as db:
            db.execute("UPDATE system_settings SET setting_value='test'")
        before = digest(self.source)
        with self.assertRaises(RuntimeError):
            capture(self.source.parent.parent, self.base / 'export')
        self.assertFalse((self.base / 'export').exists())
        self.assertEqual(digest(self.source), before)

    def test_capture_rejects_foreign_key_error_without_export(self):
        with write_connection(self.source) as db:
            db.execute('INSERT INTO order_items VALUES(2,999,1,1)')
        before = digest(self.source)
        with self.assertRaises(RuntimeError):
            capture(self.source.parent.parent, self.base / 'export')
        self.assertFalse((self.base / 'export').exists())
        self.assertEqual(digest(self.source), before)

    def test_capture_rejects_corrupt_database_without_export(self):
        self.source.write_bytes(b'not-a-sqlite-database')
        before = digest(self.source)
        with self.assertRaises(sqlite3.DatabaseError):
            capture(self.source.parent.parent, self.base / 'export')
        self.assertFalse((self.base / 'export').exists())
        self.assertEqual(digest(self.source), before)

    def test_capture_rejects_busy_shared_lock_before_export(self):
        with patch('tools.r4_deployment_preflight.database_write_lock',
                   side_effect=DatabaseWriteLockError('ERP busy')):
            with self.assertRaises(DatabaseWriteLockError):
                capture(self.source.parent.parent, self.base / 'export')
        self.assertFalse((self.base / 'export').exists())

    def test_capture_rejects_future_formal_output(self):
        future = self.base / 'future_formal'
        with patch('tools.r4_deployment_preflight.FIXED_ROOT', future):
            with self.assertRaises(ValueError):
                capture(self.source.parent.parent, future / 'data')
        self.assertFalse(future.exists())

    def test_report_cannot_create_database_or_overwrite_evidence(self):
        missing = self.base / 'missing' / 'orders.db'
        with self.assertRaises(ValueError):
            write_report(missing, {})
        self.assertFalse(missing.parent.exists())
        output = self.base / 'evidence.json'
        write_report(output, {'first': True})
        before = digest(output)
        with self.assertRaises(FileExistsError):
            write_report(output, {})
        self.assertEqual(digest(output), before)


if __name__ == '__main__':
    unittest.main()
