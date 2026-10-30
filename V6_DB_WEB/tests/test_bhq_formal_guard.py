from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_watcher import WatcherConfig, validate_database_ready  # noqa: E402
from tools import bhq_formal_database_guard as offline_guard  # noqa: E402


TEMPLATE_DATABASE = ROOT / "data" / "orders_bhq_test.db"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BhqFormalDatabaseGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = (self.base / "V6_DB_WEB").resolve()
        self.database = (self.project / "data" / "orders.db").resolve()
        self.export = (self.base / "BHQ" / "_Export").resolve()
        self.guard_config = self.base / "bhq_watcher_formal.json"
        self.guard_config.write_text(
            json.dumps({"database": str(self.database)}), encoding="utf-8"
        )
        self.patchers = [
            patch("v6_db_web.bhq_watcher.FORMAL_PROJECT_ROOT", self.project),
            patch("v6_db_web.bhq_watcher.FORMAL_DATABASE_PATH", self.database),
            patch("v6_db_web.bhq_watcher.FORMAL_EXPORT_ROOT", self.export),
            patch("tools.bhq_formal_database_guard.FORMAL_PROJECT_ROOT", self.project),
            patch("tools.bhq_formal_database_guard.FORMAL_DATABASE_PATH", self.database),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def config(self, database: Path | None = None) -> WatcherConfig:
        return WatcherConfig(
            mode="formal",
            watch_folder=self.export / "INBOX",
            processing_folder=self.export / "Processing",
            completed_folder=self.export / "Completed",
            failed_folder=self.export / "Failed",
            reports_folder=self.export / "Results",
            logs_folder=self.export / "Logs",
            state_file=self.export / "Logs" / "state.json",
            stop_file=self.export / "Logs" / "stop",
            status_file=self.export / "Logs" / "status.json",
            instance_lock_file=self.export / "Logs" / "instance",
            database=(database or self.database).resolve(),
            formal_enabled=True,
            formal_enable_confirmation="ENABLE-LOCAL-BHQ-FORMAL-WATCHER",
            backup_before_formal_import=True,
        )

    def create_legal_database(self) -> None:
        self.database.parent.mkdir(parents=True)
        shutil.copy2(TEMPLATE_DATABASE, self.database)

    def validate_path(self, config: WatcherConfig | None = None) -> None:
        (config or self.config()).validate(
            self.project,
            cli_formal_enabled=True,
            require_runtime_folders=False,
        )

    def test_missing_fixed_database_is_rejected_without_creating_file(self) -> None:
        self.database.parent.mkdir(parents=True)
        before = sorted(str(path.relative_to(self.project)) for path in self.project.rglob("*"))
        with self.assertRaisesRegex(FileNotFoundError, "拒絕自動建立"):
            self.validate_path()
        after = sorted(str(path.relative_to(self.project)) for path in self.project.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse(self.database.exists())

    def test_missing_data_folder_is_rejected_without_creating_folder(self) -> None:
        self.project.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "data 資料夾不存在"):
            self.validate_path()
        self.assertFalse(self.database.parent.exists())
        self.assertFalse(self.database.exists())

    def test_zero_byte_database_is_rejected(self) -> None:
        self.database.parent.mkdir(parents=True)
        self.database.touch()
        with self.assertRaisesRegex(RuntimeError, "0 位元組"):
            self.validate_path()
        self.assertEqual(self.database.stat().st_size, 0)

    def test_r3_project_and_same_named_database_are_rejected_unchanged(self) -> None:
        r3 = self.base / "V6_DB_WEB_DUAL_R3_20260825_165102"
        r3_database = r3 / "data" / "orders.db"
        r3_database.parent.mkdir(parents=True)
        shutil.copy2(TEMPLATE_DATABASE, r3_database)
        before = sha256(r3_database)
        with self.assertRaisesRegex(ValueError, "拒絕 R3"):
            self.config(r3_database).validate(
                r3, cli_formal_enabled=True, require_runtime_folders=False
            )
        self.assertEqual(sha256(r3_database), before)

    def test_test_database_path_is_rejected(self) -> None:
        self.project.mkdir()
        test_database = self.project / "data" / "orders_bhq_test.db"
        with self.assertRaisesRegex(ValueError, "拒絕測試庫"):
            self.config(test_database).validate(
                self.project,
                cli_formal_enabled=True,
                require_runtime_folders=False,
            )
        self.assertFalse(test_database.exists())
        self.assertFalse(test_database.parent.exists())

    def test_migration_below_v7_is_rejected_without_further_writes(self) -> None:
        self.create_legal_database()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DELETE FROM database_migrations WHERE version>=7")
            connection.commit()
        finally:
            connection.close()
        self.validate_path()
        before = sha256(self.database)
        with self.assertRaisesRegex(RuntimeError, "至少 v7"):
            validate_database_ready(self.config())
        self.assertEqual(sha256(self.database), before)

    def test_missing_bhq_table_or_index_is_rejected(self) -> None:
        self.create_legal_database()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP INDEX idx_bhq_version_import")
            connection.execute("DROP TABLE bhq_import_runs")
            connection.commit()
        finally:
            connection.close()
        self.validate_path()
        before = sha256(self.database)
        with self.assertRaisesRegex(RuntimeError, "資料表、欄位或索引不完整"):
            validate_database_ready(self.config())
        self.assertEqual(sha256(self.database), before)

    def test_corrupt_database_is_rejected(self) -> None:
        self.database.parent.mkdir(parents=True)
        self.database.write_bytes(b"not-a-sqlite-database")
        self.validate_path()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "mode=rw"):
            validate_database_ready(self.config())
        self.assertEqual(self.database.read_bytes(), before)

    def test_failed_database_validation_does_not_move_inbox_file(self) -> None:
        for folder in ("INBOX", "Processing", "Completed", "Failed", "Results", "Logs"):
            (self.export / folder).mkdir(parents=True, exist_ok=True)
        source = self.export / "INBOX" / "waiting.xls"
        source.write_bytes(b"leave-in-inbox")
        self.database.parent.mkdir(parents=True)
        self.database.write_bytes(b"not-a-sqlite-database")
        self.validate_path()
        with self.assertRaises(RuntimeError):
            validate_database_ready(self.config())
        self.assertTrue(source.is_file())
        self.assertEqual(list((self.export / "Processing").iterdir()), [])

    def test_foreign_key_failure_is_rejected(self) -> None:
        self.create_legal_database()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO bhq_store_aliases(source_store_name,store_id,created_at,updated_at) VALUES('BROKEN',999999999,'x','x')"
            )
            connection.commit()
        finally:
            connection.close()
        self.validate_path()
        before = sha256(self.database)
        with self.assertRaisesRegex(RuntimeError, "完整性或外鍵"):
            validate_database_ready(self.config())
        self.assertEqual(sha256(self.database), before)

    def test_wrong_formal_marker_is_rejected(self) -> None:
        self.create_legal_database()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE system_settings SET setting_value='test' WHERE setting_key='database_mode'"
            )
            connection.commit()
        finally:
            connection.close()
        self.validate_path()
        before = sha256(self.database)
        with self.assertRaisesRegex(RuntimeError, "識別標記"):
            validate_database_ready(self.config())
        self.assertEqual(sha256(self.database), before)

    def test_only_complete_database_at_fixed_path_is_allowed_read_only(self) -> None:
        self.create_legal_database()
        self.validate_path()
        before = sha256(self.database)
        result = validate_database_ready(self.config())
        self.assertEqual(result["connection_mode"], "rw_existing")
        self.assertGreaterEqual(result["migration"], 7)
        self.assertEqual(result["database_mode"], "formal")
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["foreign_key_errors"], 0)
        self.assertTrue(result["required_structure_complete"])
        self.assertEqual(sha256(self.database), before)

    def test_packaged_offline_guard_rejects_missing_without_creating(self) -> None:
        self.project.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "data 資料夾不存在"):
            offline_guard.validate(self.project, self.guard_config)
        self.assertFalse(self.database.parent.exists())
        self.assertFalse(self.database.exists())

    def test_packaged_offline_guard_accepts_only_complete_existing_database(self) -> None:
        self.create_legal_database()
        before = sha256(self.database)
        result = offline_guard.validate(self.project, self.guard_config)
        self.assertEqual(result["connection_mode"], "rw_existing_query_only")
        self.assertEqual(result["migration"], 7)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["foreign_key_errors"], 0)
        self.assertEqual(sha256(self.database), before)


if __name__ == "__main__":
    unittest.main()
