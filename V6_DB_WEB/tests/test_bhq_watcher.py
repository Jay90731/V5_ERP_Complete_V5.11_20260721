from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_watcher import BhqFolderWatcher, WatcherConfig  # noqa: E402
from v6_db_web.database_lock import database_write_lock, lock_path_for_database  # noqa: E402


PACKAGED_SOURCE = ROOT / "acceptance" / "202607ALL 訂單明細表.xls"
SOURCE = PACKAGED_SOURCE if PACKAGED_SOURCE.is_file() else Path.home() / "Downloads" / "202607ALL 訂單明細表.xls"
TEMPLATE_DATABASE = ROOT / "data" / "orders_bhq_test.db"
CORE_TABLES = ("orders", "order_versions", "order_items", "stores", "products")


def clone_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30
    )
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in CORE_TABLES
        }
    finally:
        connection.close()


class BhqFolderWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "BHQ_EXPORT"
        self.inbox.mkdir()
        self.database = self.root / "orders_bhq_test.db"
        clone_database(TEMPLATE_DATABASE, self.database)
        self.state = self.root / "state" / "watcher.json"
        self.reports = self.root / "reports"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def config(self, *, stable_checks: int = 1) -> WatcherConfig:
        return WatcherConfig(
            mode="test",
            watch_folder=self.inbox,
            database=self.database,
            state_file=self.state,
            reports_folder=self.reports,
            scan_interval_seconds=0.01,
            stable_checks=stable_checks,
            minimum_file_age_seconds=0,
        )

    def add_valid_file(self, name: str = "BHQ_202607.xls") -> Path:
        destination = self.inbox / name
        shutil.copy2(SOURCE, destination)
        return destination

    def test_real_xls_end_to_end_is_idempotent_and_persistent(self) -> None:
        self.add_valid_file()
        before = counts(self.database)
        watcher = BhqFolderWatcher(self.config(), self.root)
        outcomes = watcher.scan_once()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "completed")
        self.assertEqual(outcomes[0]["import"]["skipped_orders"], 257)
        self.assertEqual(outcomes[0]["import"]["inserted_orders"], 0)
        self.assertEqual(outcomes[0]["import"]["inserted_versions"], 0)
        self.assertEqual(outcomes[0]["import"]["inserted_items"], 0)
        self.assertEqual(counts(self.database), before)
        self.assertTrue(Path(outcomes[0]["report"]).is_file())
        self.assertTrue(Path(outcomes[0]["text_report"]).is_file())
        self.assertTrue(Path(outcomes[0]["completed_file"]).is_file())
        self.assertEqual(
            Path(outcomes[0]["completed_file"]).parent,
            self.root / "Completed",
        )
        self.assertTrue(self.state.is_file())
        self.assertEqual(watcher.scan_once(), [])

        self.add_valid_file()
        restarted = BhqFolderWatcher(self.config(), self.root)
        repeated = restarted.scan_once()
        self.assertEqual(repeated[0]["status"], "already_completed")
        self.assertEqual(counts(self.database), before)

    def test_file_must_be_stable_for_configured_number_of_checks(self) -> None:
        self.add_valid_file()
        watcher = BhqFolderWatcher(self.config(stable_checks=2), self.root)
        self.assertEqual(watcher.scan_once(), [])
        self.assertEqual(watcher.scan_once()[0]["status"], "completed")

    def test_erp_database_lock_defers_bhq_without_writing(self) -> None:
        self.add_valid_file()
        watcher = BhqFolderWatcher(self.config(), self.root)
        before = counts(self.database)
        outcome: list[dict] = []

        def run_scan() -> None:
            outcome.extend(watcher.scan_once())

        with database_write_lock(self.database, "simulated ERP sqlite write"):
            thread = threading.Thread(target=run_scan)
            thread.start()
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0]["status"], "deferred")
        self.assertIn("資料庫正在更新", outcome[0]["reason"])
        self.assertEqual(counts(self.database), before)
        self.assertFalse(self.state.exists())

        retry = watcher.scan_once()
        self.assertEqual(retry[0]["status"], "completed")
        self.assertEqual(counts(self.database), before)

    def test_separate_erp_process_and_watcher_share_os_file_lock(self) -> None:
        self.add_valid_file()
        watcher = BhqFolderWatcher(self.config(), self.root)
        before = counts(self.database)
        code = (
            "import sys,time;"
            f"sys.path.insert(0,{str(ROOT / 'src')!r});"
            "from v6_db_web.database_lock import database_write_lock;"
            "lock=database_write_lock(sys.argv[1],'simulated ERP process');"
            "lock.__enter__();print('LOCKED',flush=True);time.sleep(20)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.database)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            result = watcher.scan_once()
            self.assertEqual(result[0]["status"], "deferred")
            self.assertEqual(counts(self.database), before)
        finally:
            process.terminate()
            process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def test_invalid_excel_is_quarantined_in_state_without_database_change(self) -> None:
        (self.inbox / "broken.xls").write_bytes(b"not an xls workbook")
        before = counts(self.database)
        watcher = BhqFolderWatcher(self.config(), self.root)
        result = watcher.scan_once()
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(counts(self.database), before)
        self.assertTrue(Path(result[0]["report"]).is_file())
        self.assertTrue(Path(result[0]["failed_file"]).is_file())
        self.assertEqual(watcher.scan_once(), [])
        (self.inbox / "broken.xls").write_bytes(b"not an xls workbook")
        restarted = BhqFolderWatcher(self.config(), self.root)
        self.assertEqual(restarted.scan_once()[0]["status"], "already_failed")

    def test_failed_file_requires_explicit_manual_retry(self) -> None:
        (self.inbox / "broken.xls").write_bytes(b"not an xls workbook")
        watcher = BhqFolderWatcher(self.config(), self.root)
        failed = watcher.scan_once()[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(watcher.scan_once(), [])
        retried = watcher.retry_failed_files()
        self.assertEqual(retried[0]["status"], "retry_queued")
        self.assertTrue(Path(retried[0]["destination"]).is_file())
        self.assertIsNone(watcher.state.get(failed["sha256"]))

    def test_stop_flag_waits_for_loop_and_records_clean_stop(self) -> None:
        watcher = BhqFolderWatcher(self.config(), self.root)
        stop_event = threading.Event()
        thread = threading.Thread(target=watcher.run_forever, args=(stop_event,))
        thread.start()
        status_file = self.config().resolved_status_file()
        for _ in range(100):
            if status_file.is_file():
                break
            time.sleep(0.01)
        self.config().resolved_stop_file().write_text("stop", encoding="ascii")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        status = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "stopped")

    def test_temporary_excel_lock_files_and_non_xls_are_ignored(self) -> None:
        (self.inbox / "~$REPORT.xls").write_bytes(b"temporary")
        (self.inbox / "readme.txt").write_text("ignore", encoding="utf-8")
        watcher = BhqFolderWatcher(self.config(), self.root)
        self.assertEqual(watcher.scan_once(), [])

    def test_formal_mode_rejects_nonapproved_folder_scope(self) -> None:
        formal_root = self.root / "project"
        formal_database = formal_root / "data" / "orders.db"
        formal_database.parent.mkdir(parents=True)
        clone_database(TEMPLATE_DATABASE, formal_database)
        config = WatcherConfig(
            mode="formal",
            watch_folder=self.inbox,
            database=formal_database,
            state_file=formal_root / "state.json",
            reports_folder=formal_root / "reports",
            formal_enabled=False,
            formal_enable_confirmation="",
        )
        with self.assertRaisesRegex(ValueError, "固定專案|資料夾範圍不符"):
            config.validate(formal_root, cli_formal_enabled=False)

    def test_watcher_and_erp_resolve_the_same_lock_file(self) -> None:
        self.assertEqual(
            lock_path_for_database(self.database),
            self.database.with_name("orders_bhq_test.db.write.lock").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
