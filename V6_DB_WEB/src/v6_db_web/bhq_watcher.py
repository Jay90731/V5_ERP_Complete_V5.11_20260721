from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bhq_excel import dry_run_summary, parse_bhq_xls
from .bhq_import_service import bhq_content_hash, import_bhq
from .database_lock import DatabaseWriteLockError, database_write_lock
from .db import connect_existing
from .formal_database import create_formal_backup, database_mode, require_formal_marker


FORMAL_ENABLE_CONFIRMATION = "ENABLE-LOCAL-BHQ-FORMAL-WATCHER"
SUPPORTED_SUFFIXES = {".xls"}
FORMAL_PROJECT_ROOT = Path(r"C:\Users\auntstella\Desktop\V6_DB_WEB").resolve()
FORMAL_DATABASE_PATH = (FORMAL_PROJECT_ROOT / "data" / "orders.db").resolve()
FORMAL_EXPORT_ROOT = Path(r"C:\Users\auntstella\BHQ\_Export").resolve()

BHQ_REQUIRED_COLUMNS = {
    "orders": {"id", "order_no", "current_version_id", "current_content_hash"},
    "order_versions": {
        "id",
        "order_id",
        "source_system",
        "discounted_total_amount",
    },
    "order_items": {"id", "order_version_id", "line_no", "gift_code", "quantity"},
    "web_order_item_metadata": {"order_item_id", "product_name", "created_at"},
    "bhq_import_runs": {
        "id",
        "source_filename",
        "source_file_sha256",
        "sheet_name",
        "started_at",
        "finished_at",
        "source_row_count",
        "source_order_count",
        "inserted_order_count",
        "inserted_version_count",
        "inserted_item_count",
        "skipped_order_count",
        "anomaly_count",
        "result",
        "error_summary",
        "summary_json",
        "created_at",
        "updated_at",
    },
    "bhq_store_aliases": {
        "source_store_name",
        "store_id",
        "created_at",
        "updated_at",
    },
    "bhq_order_version_metadata": {
        "order_version_id",
        "import_run_id",
        "raw_store_name",
        "original_total_amount",
        "source_rows_json",
        "created_at",
    },
    "database_migrations": {"version", "name", "checksum", "applied_at"},
    "system_settings": {"setting_key", "setting_value"},
}
BHQ_REQUIRED_INDEXES = {
    "idx_bhq_import_file",
    "idx_bhq_version_import",
    "idx_order_versions_source_discounted",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _resolve_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _is_local_windows_path(path: Path) -> bool:
    text = str(path)
    return bool(path.drive) and not text.startswith("\\\\")


def _unlink_with_retry(path: Path, attempts: int = 20) -> None:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05)


@dataclass(frozen=True)
class WatcherConfig:
    mode: str
    watch_folder: Path
    database: Path
    state_file: Path
    reports_folder: Path
    processing_folder: Path | None = None
    completed_folder: Path | None = None
    failed_folder: Path | None = None
    logs_folder: Path | None = None
    stop_file: Path | None = None
    status_file: Path | None = None
    instance_lock_file: Path | None = None
    scan_interval_seconds: float = 5.0
    stable_checks: int = 3
    minimum_file_age_seconds: float = 10.0
    formal_enabled: bool = False
    formal_enable_confirmation: str = ""
    backup_before_formal_import: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "WatcherConfig":
        source = Path(path).resolve()
        payload = json.loads(source.read_text(encoding="utf-8"))
        base = source.parent
        return cls(
            mode=str(payload.get("mode") or "").strip().lower(),
            watch_folder=_resolve_path(str(payload.get("watch_folder") or ""), base),
            database=_resolve_path(str(payload.get("database") or ""), base),
            state_file=_resolve_path(str(payload.get("state_file") or ""), base),
            reports_folder=_resolve_path(
                str(payload.get("reports_folder") or ""), base
            ),
            processing_folder=_resolve_path(
                str(payload.get("processing_folder") or "Processing"),
                base,
            ),
            completed_folder=_resolve_path(
                str(payload.get("completed_folder") or "Completed"),
                base,
            ),
            failed_folder=_resolve_path(
                str(payload.get("failed_folder") or "Failed"),
                base,
            ),
            logs_folder=_resolve_path(
                str(payload.get("logs_folder") or "Logs"),
                base,
            ),
            stop_file=_resolve_path(
                str(payload.get("stop_file") or "Logs/bhq_watcher.stop"),
                base,
            ),
            status_file=_resolve_path(
                str(payload.get("status_file") or "Logs/bhq_watcher_status.json"),
                base,
            ),
            instance_lock_file=_resolve_path(
                str(payload.get("instance_lock_file") or "Logs/bhq_watcher.instance"),
                base,
            ),
            scan_interval_seconds=float(payload.get("scan_interval_seconds", 5)),
            stable_checks=int(payload.get("stable_checks", 3)),
            minimum_file_age_seconds=float(
                payload.get("minimum_file_age_seconds", 10)
            ),
            formal_enabled=bool(payload.get("formal_enabled", False)),
            formal_enable_confirmation=str(
                payload.get("formal_enable_confirmation") or ""
            ),
            backup_before_formal_import=bool(
                payload.get("backup_before_formal_import", True)
            ),
        )

    def validate(
        self,
        root: Path,
        *,
        cli_formal_enabled: bool = False,
        require_runtime_folders: bool = True,
        require_activation_confirmation: bool = True,
    ) -> None:
        root = root.resolve()
        test_database = (root / "data" / "orders_bhq_test.db").resolve()
        if self.mode not in {"test", "formal"}:
            raise ValueError("mode 必須是 test 或 formal")
        if self.stable_checks < 1:
            raise ValueError("stable_checks 必須至少為 1")
        if self.scan_interval_seconds <= 0 or self.minimum_file_age_seconds < 0:
            raise ValueError("掃描間隔必須大於 0，檔案最短等待時間不得小於 0")

        if self.mode == "test":
            if self.database != test_database:
                raise ValueError(f"測試模式只允許寫入 {test_database}")
            if self.database == FORMAL_DATABASE_PATH:
                raise ValueError("測試模式不得指向正式 orders.db")
            if not self.database.parent.is_dir():
                raise FileNotFoundError(
                    f"測試 SQLite data 資料夾不存在：{self.database.parent}"
                )
            if not self.database.is_file():
                raise FileNotFoundError(f"測試 SQLite 不存在：{self.database}")
            if self.database.stat().st_size <= 0:
                raise RuntimeError(f"測試 SQLite 是 0 位元組：{self.database}")
            if require_runtime_folders and not self.watch_folder.is_dir():
                raise FileNotFoundError(f"監看資料夾不存在：{self.watch_folder}")
            return

        if root != FORMAL_PROJECT_ROOT:
            raise ValueError(
                f"正式模式只允許固定專案 {FORMAL_PROJECT_ROOT}；拒絕 R3、其他 DUAL_R* 或任意專案：{root}"
            )
        if self.database != FORMAL_DATABASE_PATH:
            raise ValueError(
                f"正式模式只允許固定資料庫 {FORMAL_DATABASE_PATH}；拒絕測試庫、R3及其他同名 orders.db：{self.database}"
            )
        if not FORMAL_PROJECT_ROOT.is_dir():
            raise FileNotFoundError(f"固定正式專案不存在：{FORMAL_PROJECT_ROOT}")
        if not FORMAL_DATABASE_PATH.parent.is_dir():
            raise FileNotFoundError(
                f"固定正式 data 資料夾不存在，拒絕自動建立：{FORMAL_DATABASE_PATH.parent}"
            )
        if not FORMAL_DATABASE_PATH.is_file():
            raise FileNotFoundError(
                f"固定正式 orders.db 不存在，拒絕自動建立：{FORMAL_DATABASE_PATH}"
            )
        if FORMAL_DATABASE_PATH.stat().st_size <= 0:
            raise RuntimeError(f"固定正式 orders.db 是 0 位元組：{FORMAL_DATABASE_PATH}")
        lifecycle = self.lifecycle_folders()
        if not _is_local_windows_path(self.watch_folder):
            raise ValueError("正式監看資料夾必須是本機磁碟，不可使用網路路徑")
        if any("onedrive" in part.lower() for part in self.watch_folder.parts):
            raise ValueError("正式 BHQ 匯出資料夾不可位於 OneDrive")
        if _is_within(self.watch_folder, root):
            raise ValueError("正式 BHQ 匯出資料夾不可位於專案資料夾內")
        expected_base = FORMAL_EXPORT_ROOT
        expected = {
            "watch_folder": expected_base / "INBOX",
            "processing_folder": expected_base / "Processing",
            "completed_folder": expected_base / "Completed",
            "failed_folder": expected_base / "Failed",
            "reports_folder": expected_base / "Results",
            "logs_folder": expected_base / "Logs",
        }
        actual = {
            "watch_folder": self.watch_folder,
            "processing_folder": lifecycle["processing_folder"],
            "completed_folder": lifecycle["completed_folder"],
            "failed_folder": lifecycle["failed_folder"],
            "reports_folder": self.reports_folder,
            "logs_folder": lifecycle["logs_folder"],
        }
        mismatches = {
            key: {"actual": str(actual[key]), "expected": str(value)}
            for key, value in expected.items()
            if actual[key].resolve() != value.resolve()
        }
        if mismatches:
            raise ValueError(f"正式 BHQ 資料夾範圍不符：{mismatches}")
        if require_runtime_folders:
            for name, folder in actual.items():
                if not folder.is_dir():
                    raise FileNotFoundError(f"正式 {name} 不存在：{folder}")
            if _is_within(self.state_file, self.watch_folder):
                raise ValueError("正式狀態檔不可放在 INBOX 內")
        if require_activation_confirmation:
            if not self.formal_enabled or not cli_formal_enabled:
                raise PermissionError("正式自動匯入尚未雙重啟用")
            if self.formal_enable_confirmation != FORMAL_ENABLE_CONFIRMATION:
                raise PermissionError("正式自動匯入確認字串不符")
        if not self.backup_before_formal_import:
            raise PermissionError("正式自動匯入不得關閉匯入前備份")

    def lifecycle_folders(self) -> dict[str, Path]:
        base = self.watch_folder.parent
        return {
            "processing_folder": (self.processing_folder or base / "Processing").resolve(),
            "completed_folder": (self.completed_folder or base / "Completed").resolve(),
            "failed_folder": (self.failed_folder or base / "Failed").resolve(),
            "logs_folder": (self.logs_folder or base / "Logs").resolve(),
        }

    def resolved_stop_file(self) -> Path:
        return (self.stop_file or self.lifecycle_folders()["logs_folder"] / "bhq_watcher.stop").resolve()

    def resolved_status_file(self) -> Path:
        return (self.status_file or self.lifecycle_folders()["logs_folder"] / "bhq_watcher_status.json").resolve()

    def resolved_instance_lock_file(self) -> Path:
        return (self.instance_lock_file or self.lifecycle_folders()["logs_folder"] / "bhq_watcher.instance").resolve()


def validate_database_ready(config: WatcherConfig) -> dict[str, Any]:
    try:
        connection = connect_existing(config.database, query_only=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            missing_tables = sorted(set(BHQ_REQUIRED_COLUMNS) - tables)
            missing_columns = {}
            for table, required in BHQ_REQUIRED_COLUMNS.items():
                if table not in tables:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                missing = sorted(required - columns)
                if missing:
                    missing_columns[table] = missing
            missing_indexes = sorted(BHQ_REQUIRED_INDEXES - indexes)
            migration = None
            if "database_migrations" in tables:
                migration = connection.execute(
                    "SELECT MAX(version) FROM database_migrations"
                ).fetchone()[0]
            mode = database_mode(connection) if "system_settings" in tables else ""
        finally:
            connection.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise RuntimeError(
            f"既有 SQLite 無法以 mode=rw 安全開啟或驗證：{config.database}；{exc}"
        ) from exc

    checks = {
        "connection_mode": "rw_existing",
        "database_size": config.database.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_keys,
        "migration": migration,
        "database_mode": mode,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
        "required_structure_complete": not (
            missing_tables or missing_columns or missing_indexes
        ),
    }
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check 失敗：{checks}")
    if migration is None or int(migration) < 7:
        raise RuntimeError(f"BHQ 正式監看要求 migration 至少 v7：{checks}")
    if not checks["required_structure_complete"]:
        raise RuntimeError(f"BHQ 資料表、欄位或索引不完整：{checks}")
    if config.mode == "formal" and mode != "formal":
        raise RuntimeError(f"正式資料庫識別標記不正確：{checks}")
    if foreign_keys:
        raise RuntimeError(f"SQLite 完整性或外鍵檢查失敗：{checks}")
    return checks


class WatcherState:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.payload: dict[str, Any] = {
            "schema_version": 1,
            "updated_at": utc_now(),
            "files": {},
        }
        if self.path.is_file():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded.get("schema_version") != 1 or not isinstance(
                loaded.get("files"), dict
            ):
                raise RuntimeError(f"BHQ 監看狀態檔格式錯誤：{self.path}")
            self.payload = loaded

    def get(self, digest: str) -> dict[str, Any] | None:
        value = self.payload["files"].get(digest)
        return value if isinstance(value, dict) else None

    def record(self, digest: str, result: dict[str, Any]) -> None:
        self.payload["files"][digest] = result
        self._save()

    def remove(self, digest: str) -> None:
        self.payload["files"].pop(digest, None)
        self._save()

    def _save(self) -> None:
        self.payload["updated_at"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class BhqFolderWatcher:
    def __init__(self, config: WatcherConfig, root: Path):
        self.config = config
        self.root = root.resolve()
        self.state = WatcherState(config.state_file)
        self.folders = config.lifecycle_folders()
        if config.mode == "formal":
            missing = [str(folder) for folder in self.folders.values() if not folder.is_dir()]
            if not self.config.reports_folder.is_dir():
                missing.append(str(self.config.reports_folder))
            if missing:
                raise FileNotFoundError(
                    f"正式監看資料夾尚未備妥，監看器不會自行建立：{missing}"
                )
        else:
            for folder in self.folders.values():
                folder.mkdir(parents=True, exist_ok=True)
            self.config.reports_folder.mkdir(parents=True, exist_ok=True)
        self._observations: dict[str, tuple[int, int, int]] = {}
        self._settled: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _accepted_filename(path: Path) -> bool:
        name = path.name.lower()
        return (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not name.startswith("~$")
            and not name.endswith(".tmp")
            and not name.endswith(".partial")
        )

    def _eligible_inbox_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.config.watch_folder.iterdir()
                if self._accepted_filename(path)
            ),
            key=lambda path: path.name.lower(),
        )

    def _processing_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.folders["processing_folder"].iterdir()
                if self._accepted_filename(path)
            ),
            key=lambda path: path.name.lower(),
        )

    def _unique_destination(self, folder: Path, source: Path, digest: str = "") -> Path:
        destination = folder / source.name
        if not destination.exists():
            return destination
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        suffix = f"_{digest[:8]}" if digest else ""
        return folder / f"{source.stem}_{timestamp}{suffix}{source.suffix}"

    def _move(self, source: Path, folder: Path, digest: str = "") -> Path:
        destination = self._unique_destination(folder, source, digest)
        os.replace(source, destination)
        return destination

    def _is_stable(self, path: Path) -> bool:
        stat = path.stat()
        key = os.path.normcase(str(path.resolve()))
        signature = (int(stat.st_size), int(stat.st_mtime_ns))
        previous = self._observations.get(key)
        checks = previous[2] + 1 if previous and previous[:2] == signature else 1
        self._observations[key] = (*signature, checks)
        old_enough = time.time() - stat.st_mtime >= self.config.minimum_file_age_seconds
        return checks >= self.config.stable_checks and old_enough and stat.st_size > 0

    def scan_once(self) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        candidates = list(self._processing_files())
        for path in self._eligible_inbox_files():
            if not self._is_stable(path):
                continue
            try:
                processing = self._move(path, self.folders["processing_folder"])
            except (OSError, PermissionError):
                continue
            candidates.append(processing)
        for path in candidates:
            outcome = self.process_file(path)
            outcomes.append(outcome)
        return outcomes

    def validate_database_ready(self) -> dict[str, Any]:
        return validate_database_ready(self.config)

    def _validate_parsed(self, parsed: dict[str, Any]) -> None:
        if parsed["anomaly_count"]:
            raise RuntimeError(
                f"BHQ dry-run 有 {parsed['anomaly_count']} 筆異常：{parsed['anomalies'][:5]}"
            )
        if parsed["order_count"] <= 0 or parsed["valid_item_count"] <= 0:
            raise RuntimeError("BHQ Excel 沒有可匯入的訂單或商品明細")
        empty_orders = [
            order["order_no"] for order in parsed["orders"] if not order["items"]
        ]
        if empty_orders:
            raise RuntimeError(f"BHQ 訂單沒有有效商品明細：{empty_orders[:10]}")
        empty_names = [
            (order["order_no"], item["source_row"])
            for order in parsed["orders"]
            for item in order["items"]
            if not item["product_name"]
        ]
        if empty_names:
            raise RuntimeError(f"BHQ 商品名稱空白：{empty_names[:10]}")

    def _database_preflight(self, parsed: dict[str, Any]) -> dict[str, Any]:
        checks = validate_database_ready(self.config)
        connection = connect_existing(self.config.database, query_only=True)
        try:
            order_nos = [order["order_no"] for order in parsed["orders"]]
            placeholders = ",".join("?" for _ in order_nos)
            collisions = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT o.order_no,COALESCE(v.source_system,'') source_system
                    FROM orders o LEFT JOIN order_versions v ON v.id=o.current_version_id
                    WHERE o.order_no IN ({placeholders})
                      AND COALESCE(v.source_system,'')<>'BHQ'
                    ORDER BY o.order_no
                    """,
                    order_nos,
                )
            ]
            identical = 0
            for order in parsed["orders"]:
                row = connection.execute(
                    "SELECT current_content_hash FROM orders WHERE order_no=?",
                    (order["order_no"],),
                ).fetchone()
                if row and row[0] == bhq_content_hash(order):
                    identical += 1
        finally:
            connection.close()
        if collisions:
            raise RuntimeError(f"BHQ 與非 BHQ 訂單撞號：{collisions[:10]}")
        return {
            **checks,
            "cross_system_collisions": collisions,
            "identical_orders_before_import": identical,
        }

    def _postflight(self) -> dict[str, Any]:
        return validate_database_ready(self.config)

    def _write_service_status(
        self, status: str, outcomes: list[dict[str, Any]] | None = None
    ) -> None:
        destination = self.config.resolved_status_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "service": "bhq_folder_watcher",
            "mode": self.config.mode,
            "status": status,
            "pid": os.getpid() if status == "running" else None,
            "updated_at": utc_now(),
            "watch_folder": str(self.config.watch_folder),
            "processing_folder": str(self.folders["processing_folder"]),
            "database": str(self.config.database),
            "last_outcomes": outcomes or [],
        }
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, destination)

    def retry_failed_files(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for source in sorted(
            (
                path
                for path in self.folders["failed_folder"].iterdir()
                if self._accepted_filename(path)
            ),
            key=lambda path: path.name.lower(),
        ):
            digest = file_sha256(source)
            prior = self.state.get(digest)
            if not prior or prior.get("status") != "failed":
                results.append(
                    {
                        "status": "not_registered_failed",
                        "source": str(source),
                        "sha256": digest,
                    }
                )
                continue
            destination = self._move(source, self.config.watch_folder, digest)
            self.state.remove(digest)
            results.append(
                {
                    "status": "retry_queued",
                    "source": str(source),
                    "destination": str(destination),
                    "sha256": digest,
                    "previous_report": prior.get("report"),
                }
            )
        return results

    def _write_reports(self, payload: dict[str, Any], digest: str) -> dict[str, str]:
        self.config.reports_folder.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        destination = self.config.reports_folder / (
            f"BHQ_WATCH_{timestamp}_{digest[:12]}.json"
        )
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        stats = payload.get("import") or {}
        backup = payload.get("backup") or {}
        postflight = payload.get("postflight") or payload.get("database_validation") or {}
        text_report = destination.with_suffix(".txt")
        lines = [
            "BHQ Excel 自動匯入結果",
            "=" * 32,
            f"狀態：{payload.get('status', '')}",
            f"來源檔名：{Path(str(payload.get('source', ''))).name}",
            f"檔案 SHA-256：{payload.get('sha256', '')}",
            f"新增訂單：{stats.get('inserted_orders', 0)}",
            f"新增版本：{stats.get('inserted_versions', 0)}",
            f"新增明細：{stats.get('inserted_items', 0)}",
            f"新增門市：{stats.get('inserted_stores', 0)}",
            f"新增產品：{stats.get('inserted_products', 0)}",
            f"相同跳過：{stats.get('skipped_orders', 0)}",
            f"異常：{len(stats.get('anomalies') or []) if stats else payload.get('anomaly_count', 0)}",
            f"開始時間：{payload.get('started_at', '')}",
            f"完成時間：{payload.get('finished_at', '')}",
            f"正式備份位置：{backup.get('backup', '未建立（已完成檔案直接略過）')}",
            f"資料庫 integrity_check：{postflight.get('integrity_check', '沿用前次完成驗證')}",
            f"外鍵錯誤：{postflight.get('foreign_key_errors', '沿用前次完成驗證')}",
        ]
        if payload.get("error"):
            lines.extend(["", f"失敗原因：{payload['error']}"])
        if payload.get("previous_report"):
            lines.extend(["", f"前次完成報告：{payload['previous_report']}"])
        text_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"json": str(destination), "text": str(text_report)}

    def process_file(self, path: Path) -> dict[str, Any]:
        source = path.resolve()
        allowed = (
            _is_within(source, self.config.watch_folder)
            or _is_within(source, self.folders["processing_folder"])
        )
        if not allowed:
            raise ValueError(f"來源檔不在 INBOX 或 Processing：{source}")
        digest = file_sha256(source)
        prior = self.state.get(digest)
        if prior and prior.get("status") == "completed":
            payload = {
                "service": "bhq_folder_watcher",
                "mode": self.config.mode,
                "status": "already_completed",
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "source": str(source),
                "sha256": digest,
                "previous_report": prior.get("report"),
                "import": {
                    "inserted_orders": 0,
                    "inserted_versions": 0,
                    "inserted_items": 0,
                    "inserted_stores": 0,
                    "inserted_products": 0,
                    "skipped_orders": (prior.get("import") or {}).get(
                        "skipped_orders", 257
                    ),
                    "anomalies": [],
                },
            }
            reports = self._write_reports(payload, digest)
            try:
                completed = self._move(
                    source, self.folders["completed_folder"], digest
                )
            except OSError as exc:
                return {
                    **payload,
                    "status": "deferred",
                    "reason": f"已完成檔案移至 Completed 失敗：{exc}",
                    "reports": reports,
                }
            return {
                **payload,
                "completed_file": str(completed),
                "reports": reports,
            }
        if prior and prior.get("status") == "failed":
            try:
                failed = self._move(source, self.folders["failed_folder"], digest)
            except OSError as exc:
                return {
                    "status": "deferred",
                    "source": str(source),
                    "sha256": digest,
                    "reason": f"失敗檔案移至 Failed 失敗：{exc}",
                }
            return {
                "status": "already_failed",
                "source": str(source),
                "sha256": digest,
                "previous_report": prior.get("report"),
                "reason": prior.get("error"),
                "failed_file": str(failed),
            }
        started = utc_now()
        try:
            parsed = parse_bhq_xls(source)
            self._validate_parsed(parsed)
            if parsed["file_sha256"] != digest or file_sha256(source) != digest:
                return {
                    "status": "deferred",
                    "reason": "檔案在解析期間仍有變動",
                    "source": str(source),
                    "sha256": digest,
                }
            with database_write_lock(
                self.config.database,
                owner=f"bhq_watcher:{self.config.mode}:{source.name}",
            ):
                if file_sha256(source) != digest:
                    return {
                        "status": "deferred",
                        "reason": "取得資料庫鎖後來源檔已變動",
                        "source": str(source),
                        "sha256": digest,
                    }
                preflight = self._database_preflight(parsed)
                backup = None
                if self.config.mode == "formal":
                    backup = create_formal_backup(
                        self.config.database,
                        self.root,
                        backup_type="pre_update",
                        notes=f"Before automatic BHQ import: {source.name} ({digest})",
                    )
                connection = connect_existing(self.config.database)
                try:
                    if self.config.mode == "formal":
                        require_formal_marker(connection)
                    stats = import_bhq(connection, parsed)
                finally:
                    connection.close()
                postflight = self._postflight()
                if (
                    postflight["integrity_check"] != "ok"
                    or postflight["foreign_key_errors"]
                ):
                    raise RuntimeError(f"BHQ 匯入後健康檢查失敗：{postflight}")
            payload = {
                "service": "bhq_folder_watcher",
                "mode": self.config.mode,
                "status": "completed",
                "started_at": started,
                "finished_at": utc_now(),
                "source": str(source),
                "sha256": digest,
                "dry_run": dry_run_summary(parsed),
                "preflight": preflight,
                "backup": backup,
                "import": stats.as_dict(),
                "postflight": postflight,
            }
            reports = self._write_reports(payload, digest)
            state_entry = {
                "status": "completed",
                "source": str(source),
                "filename": source.name,
                "sha256": digest,
                "finished_at": payload["finished_at"],
                "report": reports["json"],
                "text_report": reports["text"],
                "import": stats.as_dict(),
            }
            self.state.record(digest, state_entry)
            try:
                completed = self._move(
                    source, self.folders["completed_folder"], digest
                )
            except OSError as exc:
                return {
                    **payload,
                    **state_entry,
                    "status": "completed_pending_archive",
                    "reason": str(exc),
                    "reports": reports,
                }
            return {
                **payload,
                **state_entry,
                "status": "completed",
                "completed_file": str(completed),
                "reports": reports,
            }
        except DatabaseWriteLockError as exc:
            return {
                "status": "deferred",
                "reason": str(exc),
                "source": str(source),
                "sha256": digest,
            }
        except Exception as exc:
            payload = {
                "service": "bhq_folder_watcher",
                "mode": self.config.mode,
                "status": "failed",
                "started_at": started,
                "finished_at": utc_now(),
                "source": str(source),
                "sha256": digest,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            reports = self._write_reports(payload, digest)
            state_entry = {
                **payload,
                "report": reports["json"],
                "text_report": reports["text"],
            }
            self.state.record(digest, state_entry)
            try:
                failed = self._move(source, self.folders["failed_folder"], digest)
            except OSError as move_error:
                return {
                    **state_entry,
                    "status": "failed_pending_archive",
                    "move_error": str(move_error),
                    "reports": reports,
                }
            return {**state_entry, "failed_file": str(failed), "reports": reports}

    def run_forever(self, stop_event: threading.Event) -> None:
        stop_file = self.config.resolved_stop_file()
        _unlink_with_retry(stop_file)
        self._write_service_status("running")
        last_outcomes: list[dict[str, Any]] = []
        try:
            while not stop_event.is_set() and not stop_file.exists():
                last_outcomes = self.scan_once()
                for outcome in last_outcomes:
                    print(
                        json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                        flush=True,
                    )
                self._write_service_status("running", last_outcomes)
                deadline = time.monotonic() + self.config.scan_interval_seconds
                while time.monotonic() < deadline:
                    if stop_event.wait(min(0.5, deadline - time.monotonic())):
                        break
                    if stop_file.exists():
                        break
        finally:
            _unlink_with_retry(stop_file)
            self._write_service_status("stopped", last_outcomes)


def install_stop_handlers(stop_event: threading.Event) -> None:
    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
