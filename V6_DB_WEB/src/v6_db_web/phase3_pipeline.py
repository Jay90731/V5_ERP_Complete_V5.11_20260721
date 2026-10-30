from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from .db import checkpoint, connect
from .database_lock import database_write_lock
from .formal_database import (
    create_formal_backup,
    require_formal_marker,
    require_formal_path,
)
from .migrations import apply_migrations
from .ocr_ingest import ReplaySummary, replay_ocr_json


STAGES = (
    ("erp_prepare", "ERP準備"),
    ("query_orders", "查詢訂單"),
    ("capture", "批次截圖"),
    ("ocr", "OCR"),
    ("sqlite_write", "SQLite驗證及寫入"),
    ("complete", "完成"),
)

PIPELINE_MODES = {
    "daily_7_days": {
        "job_type": "daily_7_days",
        "capture_argument": "-ShortRangeDays",
        "capture_value": "7",
    },
    "monthly_6_months": {
        "job_type": "monthly_6_months",
        "capture_argument": "-FullSixMonths",
        "capture_value": None,
    },
}

LEGACY_MODE_ALIASES = {
    "erp_daily_7_days": "daily_7_days",
    "integration_replay": "daily_7_days",
}

MODE_LABELS = {
    "daily_7_days": "每日 7 天",
    "monthly_6_months": "最近 6 個月",
}

CAPTURE_IMAGE_NAME = re.compile(
    r"^(?:GOU[0-9A-Z-]{6,20}|seq_[0-9]{4})\.png$", re.IGNORECASE
)


class PipelineStopped(RuntimeError):
    pass


_ATOMIC_JSON_LOCKS: dict[str, threading.RLock] = {}
_ATOMIC_JSON_LOCKS_GUARD = threading.Lock()


def local_now() -> datetime:
    return datetime.now().astimezone()


def _atomic_json_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _ATOMIC_JSON_LOCKS_GUARD:
        return _ATOMIC_JSON_LOCKS.setdefault(key, threading.RLock())


def atomic_json(
    path: Path, payload: dict[str, Any], attempts: int = 12
) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _atomic_json_lock(path)
    with lock:
        encoded = json.dumps(
            payload, ensure_ascii=False, indent=2
        ).encode("utf-8")
        last_error: OSError | None = None
        for attempt in range(1, attempts + 1):
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}."
                f"{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                return
            except OSError as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
                if attempt >= attempts:
                    break
                time.sleep(min(0.8, 0.04 * (2 ** (attempt - 1))))
        raise OSError(
            f"Atomic JSON replace failed after {attempts} attempts: {path}"
        ) from last_error


def clear_control_flag(path: Path, attempts: int = 12) -> None:
    """Remove a control flag and verify it stayed removed."""
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            path.unlink(missing_ok=True)
            if not path.exists():
                return
        except OSError as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(0.8, 0.04 * (2 ** (attempt - 1))))
    raise OSError(
        f"Control flag could not be cleared after {attempts} attempts: {path}"
    ) from last_error


def normalize_iso_date(value: str) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) != 8:
        raise ValueError(f"Invalid query date: {value!r}")
    normalized = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    datetime.strptime(normalized, "%Y-%m-%d")
    return normalized


def normalize_date_range(date_from: str, date_to: str) -> tuple[str, str]:
    start = normalize_iso_date(date_from)
    end = normalize_iso_date(date_to)
    if start > end:
        raise ValueError(f"date_from must not be later than date_to: {start} > {end}")
    return start, end


def normalize_pipeline_mode(value: str) -> str:
    mode = LEGACY_MODE_ALIASES.get(str(value), str(value))
    if mode not in PIPELINE_MODES:
        raise ValueError(
            f"Unsupported pipeline mode: {value!r}; "
            f"expected one of {sorted(PIPELINE_MODES)}"
        )
    return mode


def subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def query_range_for_mode(mode: str, today: date) -> tuple[str, str]:
    normalized = normalize_pipeline_mode(mode)
    if normalized == "daily_7_days":
        start = today - timedelta(days=6)
    else:
        start = subtract_months(today, 6)
    return start.isoformat(), today.isoformat()


def capture_arguments_for_mode(mode: str) -> list[str]:
    config = PIPELINE_MODES[normalize_pipeline_mode(mode)]
    arguments = [str(config["capture_argument"])]
    if config["capture_value"] is not None:
        arguments.append(str(config["capture_value"]))
    return arguments


def capture_image_paths(directory: Path) -> list[Path]:
    images = sorted(
        path
        for path in directory.glob("*.png")
        if path.is_file() and CAPTURE_IMAGE_NAME.fullmatch(path.name)
    )
    for path in images:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if image.width < 1000 or image.height < 600:
                    raise ValueError(
                        f"截圖尺寸不足：{path.name} "
                        f"{image.width}x{image.height}"
                    )
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise ValueError(f"截圖損壞或無法讀取：{path}: {exc}") from exc
    return images


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rename_or_deduplicate_image(source: Path, target: Path) -> bool:
    if source == target:
        return source.is_file()
    if not source.is_file():
        return target.is_file()
    if target.exists():
        if _file_sha256(source) != _file_sha256(target):
            return False
        source.unlink()
        return True
    source.replace(target)
    return True


def reconcile_sequential_capture_names(ocr_json: Path, batch: Path) -> int:
    payload = json.loads(ocr_json.read_text(encoding="utf-8-sig"))
    input_dir = ocr_json.parent.parent / "input_images"
    captures = batch / "captures"
    renamed = 0
    changed = False
    for record in payload.get("orders", []):
        original_name = str(record.get("imageName") or "")
        order_no = str(record.get("orderNo") or "").upper()
        if not re.fullmatch(r"seq_[0-9]{4}\.png", original_name, re.I):
            continue
        if not re.fullmatch(r"GOU[0-9A-Z-]{6,20}", order_no):
            continue
        target_name = f"{order_no}.png"
        ocr_source = input_dir / original_name
        ocr_target = input_dir / target_name
        if not _rename_or_deduplicate_image(ocr_source, ocr_target):
            record["imageRenameStatus"] = "conflict_kept_sequential_name"
            changed = True
            continue
        _rename_or_deduplicate_image(
            captures / original_name, captures / target_name
        )
        record["originalImageName"] = original_name
        record["imageName"] = target_name
        record["sourcePath"] = str(ocr_target)
        record["imageRenameStatus"] = "renamed_after_ocr"
        renamed += 1
        changed = True
    if changed:
        atomic_json(ocr_json, payload)
    return renamed


def read_key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key.strip()] = value.strip()
    return result


def resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def resolve_executable(root: Path, candidates: list[str]) -> str:
    for value in candidates:
        path = Path(value)
        if path.is_absolute() or any(mark in value for mark in ("\\", "/")):
            resolved = resolve_path(root, value)
            if resolved.is_file():
                return str(resolved)
        else:
            found = shutil.which(value)
            if found:
                return found
    raise FileNotFoundError(f"Executable was not found: {candidates}")


def ensure_database(
    root: Path,
    configured_path: str,
    database_mode: str,
) -> Path:
    database = resolve_path(root, configured_path)
    if database_mode == "test":
        expected = (root / "data" / "orders_test.db").resolve()
        if database != expected:
            raise ValueError(
                "Test pipeline only allows data/orders_test.db; "
                f"received {database}"
            )
        if database.name.lower() == "orders.db":
            raise ValueError("Test pipeline cannot write orders.db")
        return database
    if database_mode == "formal":
        database = require_formal_path(database, root)
        if not database.is_file():
            raise FileNotFoundError(f"Formal orders.db is missing: {database}")
        connection = connect(database)
        try:
            require_formal_marker(connection)
        finally:
            connection.close()
        return database
    raise ValueError(
        "database_mode must be 'test' or 'formal'"
    )


@dataclass
class Phase3Config:
    root: Path
    raw: dict[str, Any]

    @property
    def database(self) -> Path:
        return ensure_database(
            self.root,
            str(self.raw["database_path"]),
            self.database_mode,
        )

    @property
    def database_mode(self) -> str:
        return str(self.raw.get("database_mode") or "test").strip().lower()

    def path(self, key: str) -> Path:
        return resolve_path(self.root, str(self.raw[key]))

    @property
    def runs_root(self) -> Path:
        return self.path("runs_path")

    @property
    def control_root(self) -> Path:
        return self.path("control_path")


def load_config(path: str | Path) -> Phase3Config:
    config_path = Path(path).resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    required = {
        "database_path",
        "runs_path",
        "control_path",
        "erp_exe_path",
        "capture_script",
        "ocr_script",
        "powershell_candidates",
        "tesseract_candidates",
        "tessdata_candidates",
        "daily_range_days",
        "minimum_wait_seconds",
        "maximum_wait_seconds",
        "maximum_orders",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"Missing Phase 3 config keys: {missing}")
    config = Phase3Config(config_path.parent, payload)
    config.database
    return config


class RunState:
    def __init__(self, run_dir: Path, payload: dict[str, Any]):
        self.run_dir = run_dir
        self.path = run_dir / "run_state.json"
        self.payload = payload

    @classmethod
    def create(
        cls,
        runs_root: Path,
        mode: str,
        query_start: str,
        query_end: str,
    ) -> "RunState":
        now = local_now()
        run_id = f"run_{now.strftime('%Y%m%d_%H%M%S_%f')[:-3]}"
        run_dir = runs_root / now.strftime("%Y-%m-%d") / run_id
        stages = [
            {
                "key": key,
                "label": label,
                "status": "pending",
                "progress": 0,
                "message": "",
                "started_at": "",
                "finished_at": "",
            }
            for key, label in STAGES
        ]
        state = cls(
            run_dir,
            {
                "version": "V6_PHASE3",
                "run_id": run_id,
                "run_mode": mode,
                "status": "running",
                "started_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "finished_at": "",
                "current_stage": "erp_prepare",
                "progress": 0,
                "message": "準備開始",
                "query_start": query_start,
                "query_end": query_end,
                "current_order": "",
                "completed_count": 0,
                "screenshot_count": 0,
                "ocr_count": 0,
                "pending_count": 0,
                "failed_count": 0,
                "capture_batch": "",
                "ocr_json": "",
                "scan_job_id": None,
                "database_path": "",
                "summary_path": "",
                "error_reason": "",
                "stages": stages,
            },
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        state.save()
        return state

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        path = run_dir / "run_state.json"
        return cls(
            run_dir,
            json.loads(path.read_text(encoding="utf-8-sig")),
        )

    def save(self) -> None:
        self.payload["updated_at"] = local_now().isoformat()
        atomic_json(self.path, self.payload)

    def stage(self, key: str) -> dict[str, Any]:
        for stage in self.payload["stages"]:
            if stage["key"] == key:
                return stage
        raise KeyError(key)

    def update_stage(
        self, key: str, status: str, message: str, progress: int
    ) -> None:
        stage = self.stage(key)
        now = local_now().isoformat()
        if status == "running" and not stage["started_at"]:
            stage["started_at"] = now
        if status in {"completed", "failed", "stopped", "skipped"}:
            stage["finished_at"] = now
        stage["status"] = status
        stage["message"] = message
        stage["progress"] = max(0, min(100, int(progress)))
        self.payload["current_stage"] = key
        self.payload["message"] = message
        stage_index = [item[0] for item in STAGES].index(key)
        overall = (stage_index + stage["progress"] / 100) / len(STAGES)
        self.payload["progress"] = round(overall * 100)
        self.save()

    def skip(self, key: str, message: str) -> None:
        self.update_stage(key, "skipped", message, 100)

    def complete(self, key: str, message: str) -> None:
        self.update_stage(key, "completed", message, 100)


class Phase3Pipeline:
    def __init__(
        self,
        config: Phase3Config,
        resume: bool = False,
        replay_ocr: str | Path | None = None,
        replay_date_from: str | None = None,
        replay_date_to: str | None = None,
        mode: str = "daily_7_days",
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.config = config
        self.resume = resume
        self.replay_ocr = Path(replay_ocr).resolve() if replay_ocr else None
        self.replay_date_from = replay_date_from
        self.replay_date_to = replay_date_to
        self.mode = normalize_pipeline_mode(mode)
        self.popen = popen
        self.stop_flag = config.control_root / "STOP_V6_NOW.flag"
        self.login_flag = config.control_root / "LOGIN_CONTINUE.flag"
        self.manual_maximize_request = (
            config.control_root / "MANUAL_MAXIMIZE_REQUIRED.flag"
        )
        self.manual_maximize_continue = (
            config.control_root / "MANUAL_MAXIMIZE_CONTINUE.flag"
        )
        self.latest_run_file = config.root / "latest_run_path.txt"
        self.state: RunState | None = None
        self.log_path: Path | None = None

    def log(self, message: str) -> None:
        if self.log_path is None:
            return
        timestamp = local_now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{timestamp} {message}\n")

    def stopped(self) -> bool:
        return self.stop_flag.exists()

    def assert_continue(self, point: str) -> None:
        if self.stopped():
            raise PipelineStopped(f"使用者要求停止：{point}")

    def find_resume_state(self) -> RunState:
        candidates = sorted(
            self.config.runs_root.glob("*/*/run_state.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            state = RunState.load(path.parent)
            stored_mode = normalize_pipeline_mode(
                state.payload.get("run_mode", "daily_7_days")
            )
            if (
                state.payload["status"] in {"stopped", "failed"}
                and stored_mode == self.mode
            ):
                state.payload["run_mode"] = stored_mode
                return state
        raise FileNotFoundError("找不到可續跑的 V6 第三階段批次")

    def initialize(self) -> None:
        self.config.control_root.mkdir(parents=True, exist_ok=True)
        clear_control_flag(self.stop_flag)
        clear_control_flag(self.login_flag)
        clear_control_flag(self.manual_maximize_request)
        clear_control_flag(self.manual_maximize_continue)
        if self.resume:
            self.state = self.find_resume_state()
            self.state.payload["status"] = "running"
            self.state.payload["finished_at"] = ""
            self.state.payload["error_reason"] = ""
            for stage in self.state.payload["stages"]:
                if stage["status"] in {"running", "failed", "stopped"}:
                    stage["status"] = "pending"
                    stage["progress"] = 0
                    stage["finished_at"] = ""
            self.state.save()
        else:
            now = local_now()
            if self.replay_ocr:
                if not self.replay_date_from or not self.replay_date_to:
                    raise ValueError(
                        "Replay mode requires date_from and date_to"
                    )
                query_start, query_end = normalize_date_range(
                    self.replay_date_from, self.replay_date_to
                )
            else:
                query_start, query_end = query_range_for_mode(
                    self.mode, now.date()
                )
            self.state = RunState.create(
                self.config.runs_root, self.mode, query_start, query_end
            )
        # A resume must never inherit a stop request from the prior process.
        clear_control_flag(self.stop_flag)
        self.log_path = self.state.run_dir / "phase3_run.log"
        self.latest_run_file.write_text(
            str(self.state.run_dir), encoding="utf-8"
        )
        self.state.payload["database_path"] = str(self.config.database)
        self.state.save()
        self.log(
            f"V6 Phase 3 started mode={self.state.payload['run_mode']} "
            f"resume={self.resume}"
        )

    @property
    def run_state(self) -> RunState:
        if self.state is None:
            raise RuntimeError("Pipeline has not been initialized")
        return self.state

    def latest_capture_batch(self) -> Path | None:
        batches = self.run_state.run_dir / "erp_capture" / "batches"
        candidates = [path for path in batches.glob("*") if path.is_dir()]
        return max(candidates, key=lambda path: path.stat().st_mtime, default=None)

    def monitor_capture(self, process: subprocess.Popen) -> None:
        last_values: tuple[str, int, int, float, float, int, str] | None = None
        while process.poll() is None:
            batch = self.latest_capture_batch()
            if batch:
                query_ready = False
                logs = sorted(batch.glob("v4_run_*.log"))
                if logs:
                    text = logs[-1].read_text(
                        encoding="utf-8-sig", errors="replace"
                    )
                    query_ready = (
                        "STEP OK: query_gou_orders" in text
                    )
                    if query_ready and (
                        self.run_state.stage("query_orders")["status"]
                        != "completed"
                    ):
                        self.run_state.complete(
                            "erp_prepare", "ERP 已登入並進入訂單畫面"
                        )
                        self.run_state.complete(
                            "query_orders",
                            f"{MODE_LABELS[self.mode]} GOU 清單已載入",
                        )
                batch_state = batch / "batch_state.json"
                if batch_state.exists():
                    try:
                        payload = json.loads(
                            batch_state.read_text(encoding="utf-8-sig")
                        )
                        current = str(payload.get("current_order") or "")
                        count = int(payload.get("captured_count") or 0)
                        duplicates = int(
                            payload.get("duplicate_transition_count") or 0
                        )
                        no_new_seconds = float(
                            payload.get("seconds_since_last_new_unique_order") or 0
                        )
                        no_screenshot_seconds = float(
                            payload.get("seconds_since_last_new_screenshot") or 0
                        )
                        key_attempts = int(
                            payload.get("stationary_key_attempts") or 0
                        )
                        verification = str(
                            payload.get("list_verification_status") or ""
                        )
                        values = (
                            current,
                            count,
                            duplicates,
                            no_new_seconds,
                            no_screenshot_seconds,
                            key_attempts,
                            verification,
                        )
                        if values != last_values and (query_ready or count > 0):
                            self.run_state.payload["current_order"] = current
                            self.run_state.payload["completed_count"] = count
                            self.run_state.payload["duplicate_transition_count"] = (
                                duplicates
                            )
                            self.run_state.payload[
                                "seconds_since_last_new_unique_order"
                            ] = no_new_seconds
                            self.run_state.payload[
                                "seconds_since_last_new_screenshot"
                            ] = no_screenshot_seconds
                            self.run_state.payload["stationary_key_attempts"] = (
                                key_attempts
                            )
                            self.run_state.payload["list_verification_status"] = (
                                verification
                            )
                            prefix = (
                                "查詢結果尾端驗證中｜"
                                if verification == "query_end_verifying"
                                else ""
                            )
                            message = (
                                f"{prefix}目前訂單 {current or '-'}｜完成 {count}｜"
                                f"距新增截圖 {no_screenshot_seconds:.0f}秒｜"
                                f"距新訂單 {no_new_seconds:.0f}秒｜"
                                f"按鍵嘗試 {key_attempts}｜重複 {duplicates}"
                            )
                            self.run_state.update_stage(
                                "capture",
                                "running",
                                message,
                                min(95, max(1, round(count / 2000 * 100))),
                            )
                            last_values = values
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
            time.sleep(0.5)

    def run_capture(self) -> tuple[Path, dict[str, str]]:
        state = self.run_state
        existing = self.latest_capture_batch()
        if existing and (existing / "summary.txt").exists():
            summary = read_key_values(existing / "summary.txt")
            if summary.get("status") == "completed":
                return existing, summary

        state.update_stage(
            "erp_prepare",
            "running",
            "請在 ERP 人工登入，完成後按「登入完成，繼續」",
            10,
        )
        query_stage = state.stage("query_orders")
        query_stage["message"] = "等待 ERP 登入"
        state.save()
        capture_root = state.run_dir / "erp_capture"
        batches_root = capture_root / "batches"
        batches_root.mkdir(parents=True, exist_ok=True)
        stdout_path = state.run_dir / "capture_stdout.log"
        stderr_path = state.run_dir / "capture_stderr.log"
        powershell = resolve_executable(
            self.config.root,
            list(self.config.raw["powershell_candidates"]),
        )
        # Clear once more immediately before spawning the capture process.
        clear_control_flag(self.stop_flag)
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.config.path("capture_script")),
            "-ErpExePath",
            str(self.config.path("erp_exe_path")),
            "-BatchesRootOverride",
            str(batches_root),
            "-StopFlagPathOverride",
            str(self.stop_flag),
            "-LoginContinueFlagPath",
            str(self.login_flag),
            "-ManualMaximizeRequestPath",
            str(self.manual_maximize_request),
            "-ManualMaximizeContinueFlagPath",
            str(self.manual_maximize_continue),
            "-MinimumWaitSeconds",
            str(int(self.config.raw["minimum_wait_seconds"])),
            "-MaximumWaitSeconds",
            str(int(self.config.raw["maximum_wait_seconds"])),
            "-MaximumOrders",
            str(int(self.config.raw["maximum_orders"])),
        ]
        command.extend(capture_arguments_for_mode(self.mode))
        if self.resume and existing:
            command.append("-ResumeLatest")
        self.log(f"Starting ERP capture: {command[0]} ...")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            process = self.popen(command, stdout=stdout, stderr=stderr)
            self.monitor_capture(process)
            exit_code = process.wait()
        batch = self.latest_capture_batch()
        if batch is None or not (batch / "summary.txt").exists():
            error = stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
            raise RuntimeError(
                f"ERP 截圖未產生批次摘要，exit={exit_code}: {error}"
            )
        summary = read_key_values(batch / "summary.txt")
        if summary.get("status") == "stopped_by_user":
            raise PipelineStopped(
                summary.get("stop_reason") or "ERP 截圖已安全停止"
            )
        if exit_code != 0 or summary.get("status") != "completed":
            error = stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
            raise RuntimeError(
                "ERP 截圖失敗："
                f"exit={exit_code}, status={summary.get('status')}, "
                f"reason={summary.get('stop_reason')}, {error}"
            )
        captures = batch / "captures"
        images = capture_image_paths(captures)
        expected = int(summary.get("success_count") or 0)
        if len(images) != expected:
            raise RuntimeError(
                f"截圖數量不一致：PNG={len(images)} success={expected}"
            )
        state.payload["capture_batch"] = str(batch)
        state.payload["screenshot_count"] = len(images)
        state.payload["completed_count"] = len(images)
        state.payload["query_start"] = normalize_iso_date(
            summary["date_start"]
        )
        state.payload["query_end"] = normalize_iso_date(summary["date_end"])
        state.complete("erp_prepare", "ERP 準備完成")
        state.complete(
            "query_orders", f"{MODE_LABELS[self.mode]} GOU 查詢完成"
        )
        state.complete("capture", f"完成 {len(images)} 張訂單截圖")
        return batch, summary

    def copy_capture_images(self, batch: Path) -> Path:
        source = batch / "captures"
        target = self.run_state.run_dir / "ocr" / "input_images"
        target.mkdir(parents=True, exist_ok=True)
        for image in capture_image_paths(source):
            destination = target / image.name
            if not destination.exists():
                shutil.copy2(image, destination)
        return target

    def monitor_ocr(
        self, process: subprocess.Popen, progress_path: Path
    ) -> None:
        last_values: tuple[int, int, str] | None = None
        while process.poll() is None:
            if progress_path.exists():
                try:
                    payload = json.loads(
                        progress_path.read_text(encoding="utf-8-sig")
                    )
                    current = int(payload.get("current") or 0)
                    total = int(payload.get("total") or 0)
                    image = str(payload.get("image") or "")
                    values = (current, total, image)
                    if values != last_values:
                        percent = round(current / total * 100) if total else 0
                        self.run_state.payload["current_order"] = Path(
                            image
                        ).stem
                        self.run_state.payload["completed_count"] = current
                        self.run_state.update_stage(
                            "ocr",
                            "running",
                            f"OCR {current}/{total}：{image}",
                            percent,
                        )
                        last_values = values
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            time.sleep(0.5)

    def run_ocr(self, batch: Path) -> Path:
        state = self.run_state
        ocr_root = state.run_dir / "ocr"
        data_path = ocr_root / "data" / "orders.json"
        if data_path.exists():
            try:
                existing = json.loads(
                    data_path.read_text(encoding="utf-8-sig")
                )
                if (
                    bool(existing.get("summary", {}).get("completed"))
                    and int(
                        existing.get("summary", {}).get("completedImages", -1)
                    )
                    == int(state.payload["screenshot_count"])
                ):
                    reconcile_sequential_capture_names(data_path, batch)
                    state.payload["ocr_count"] = int(
                        existing["summary"]["completedImages"]
                    )
                    state.complete(
                        "ocr",
                        f"OCR 已完成 {state.payload['ocr_count']} 張",
                    )
                    return data_path
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        self.assert_continue("開始 OCR 前")
        self.copy_capture_images(batch)
        tesseract = resolve_executable(
            self.config.root,
            list(self.config.raw["tesseract_candidates"]),
        )
        tessdata = next(
            (
                resolve_path(self.config.root, value)
                for value in self.config.raw["tessdata_candidates"]
                if resolve_path(self.config.root, value).is_dir()
            ),
            None,
        )
        if tessdata is None:
            raise FileNotFoundError("找不到 Tesseract 語言資料")
        progress = state.run_dir / "ocr_progress.json"
        stdout_path = state.run_dir / "ocr_stdout.log"
        stderr_path = state.run_dir / "ocr_stderr.log"
        state.update_stage("ocr", "running", "OCR 開始", 0)
        command = [
            sys.executable,
            str(self.config.path("ocr_script")),
            "--root",
            str(ocr_root),
            "--tesseract",
            tesseract,
            "--tessdata",
            str(tessdata),
            "--input-pattern",
            "*.png",
            "--date-from",
            str(state.payload["query_start"]),
            "--date-to",
            str(state.payload["query_end"]),
            "--progress-file",
            str(progress),
            "--stop-file",
            str(self.stop_flag),
        ]
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            process = self.popen(command, stdout=stdout, stderr=stderr)
            self.monitor_ocr(process, progress)
            exit_code = process.wait()
        if exit_code == 2 or self.stopped():
            raise PipelineStopped("OCR 已安全停止")
        if exit_code != 0 or not data_path.exists():
            error = stderr_path.read_text(
                encoding="utf-8", errors="replace"
            )
            raise RuntimeError(f"OCR 失敗，exit={exit_code}: {error}")
        payload = json.loads(data_path.read_text(encoding="utf-8-sig"))
        completed = int(payload.get("summary", {}).get("completedImages", -1))
        expected = int(state.payload["screenshot_count"])
        if not payload.get("summary", {}).get("completed") or completed != expected:
            raise RuntimeError(
                f"OCR 輸出不完整：completed={completed}, expected={expected}"
            )
        renamed = reconcile_sequential_capture_names(data_path, batch)
        if renamed:
            self.log(f"OCR recovered and renamed {renamed} sequential image(s)")
        state.payload["ocr_count"] = completed
        state.payload["ocr_json"] = str(data_path)
        state.complete("ocr", f"OCR 完成 {completed} 張")
        return data_path

    def replay_progress(
        self,
        index: int,
        total: int,
        record: dict[str, Any],
        result_code: str,
    ) -> None:
        order_no = str(record.get("orderNo") or Path(
            str(record.get("imageName") or "")
        ).stem)
        state = self.run_state
        state.payload["current_order"] = order_no
        state.payload["completed_count"] = index
        if result_code == "pending":
            state.payload["pending_count"] = (
                int(state.payload["pending_count"]) + 1
            )
        if result_code == "failed":
            state.payload["failed_count"] = (
                int(state.payload["failed_count"]) + 1
            )
        state.update_stage(
            "sqlite_write",
            "running",
            f"SQLite {index}/{total}：{order_no}（{result_code}）",
            round(index / total * 100) if total else 100,
        )

    def write_sqlite(self, ocr_json: Path) -> ReplaySummary:
        with database_write_lock(
            self.config.database,
            owner=f"phase3_pipeline:{self.mode}",
        ):
            if self.config.database_mode == "formal":
                backup = create_formal_backup(
                    self.config.database,
                    self.config.root,
                    backup_type="pre_update",
                    notes=(
                        f"Before {self.mode} ERP scan "
                        f"{self.run_state.payload['query_start']} through "
                        f"{self.run_state.payload['query_end']}"
                    ),
                )
                self.run_state.payload["formal_backup"] = backup["relative_path"]
                self.run_state.save()
            return self._write_sqlite_locked(ocr_json)

    def _write_sqlite_locked(self, ocr_json: Path) -> ReplaySummary:
        self.assert_continue("寫入 SQLite 前")
        state = self.run_state
        state.update_stage(
            "sqlite_write", "running", "驗證 OCR 並寫入測試資料庫", 0
        )
        connection = connect(self.config.database)
        try:
            apply_migrations(connection, self.config.root / "migrations")
            summary = replay_ocr_json(
                connection,
                ocr_json,
                state.payload["query_start"],
                state.payload["query_end"],
                job_type=PIPELINE_MODES[self.mode]["job_type"],
                progress_hook=self.replay_progress,
            )
            connection.execute(
                """
                INSERT INTO audit_logs(
                  event_type, entity_type, entity_id, actor, details_json, created_at
                ) VALUES('erp_scan_batch_ingested','scan_jobs',?,'phase3_pipeline',?,?)
                """,
                (
                    str(summary.scan_job_id),
                    json.dumps(
                        {
                            "source": "erp_capture_ocr",
                            "run_mode": self.mode,
                            "job_type": summary.job_type,
                            "date_from": summary.date_from,
                            "date_to": summary.date_to,
                            "source_file": summary.source_file,
                            "counts": summary.as_dict(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    summary.finished_at,
                ),
            )
            connection.commit()
            quick_check = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            checkpoint(connection)
        finally:
            connection.close()
        if quick_check != "ok" or foreign_keys:
            raise RuntimeError(
                f"SQLite 驗證失敗：quick_check={quick_check}, "
                f"foreign_keys={len(foreign_keys)}"
            )
        state.payload["scan_job_id"] = summary.scan_job_id
        state.payload["pending_count"] = summary.pending_count
        state.payload["failed_count"] = summary.failed_count
        state.complete(
            "sqlite_write",
            (
                f"SQLite 完成：新增 {summary.success_count}、"
                f"未變更 {summary.unchanged_count}、"
                f"異動 {summary.changed_count}、"
                f"待確認 {summary.pending_count}、"
                f"排除 {summary.excluded_count}、"
                f"失敗 {summary.failed_count}"
            ),
        )
        return summary

    def write_summary(self, replay: ReplaySummary) -> Path:
        state = self.run_state
        finished = local_now()
        started = datetime.fromisoformat(state.payload["started_at"])
        payload = {
            "version": "V6_PHASE3",
            "run_id": state.payload["run_id"],
            "run_mode": state.payload["run_mode"],
            "job_type": PIPELINE_MODES[self.mode]["job_type"],
            "query_start": state.payload["query_start"],
            "query_end": state.payload["query_end"],
            "screenshot_count": state.payload["screenshot_count"],
            "ocr_count": state.payload["ocr_count"],
            "scan_job_id": replay.scan_job_id,
            "total_count": replay.total_count,
            "new_count": replay.success_count,
            "unchanged_count": replay.unchanged_count,
            "changed_count": replay.changed_count,
            "pending_count": replay.pending_count,
            "excluded_count": replay.excluded_count,
            "failed_count": replay.failed_count,
            "database": str(self.config.database),
            "database_mode": self.config.database_mode,
            "formal_backup": state.payload.get("formal_backup", ""),
            "started_at": state.payload["started_at"],
            "finished_at": finished.isoformat(),
            "elapsed_seconds": round((finished - started).total_seconds(), 3),
            "status": "completed",
        }
        path = state.run_dir / "run_summary.json"
        atomic_json(path, payload)
        text = [
            f"V6 {MODE_LABELS[self.mode]}批次摘要",
            f"模式：{payload['run_mode']}",
            f"工作類型：{payload['job_type']}",
            f"查詢日期：{payload['query_start']} 至 {payload['query_end']}",
            f"截圖：{payload['screenshot_count']}",
            f"OCR：{payload['ocr_count']}",
            f"總訂單：{payload['total_count']}",
            f"新增：{payload['new_count']}",
            f"未變更：{payload['unchanged_count']}",
            f"已變更：{payload['changed_count']}",
            f"待確認：{payload['pending_count']}",
            f"排除：{payload['excluded_count']}",
            f"失敗：{payload['failed_count']}",
            f"SQLite：{payload['database']}",
            f"執行秒數：{payload['elapsed_seconds']}",
        ]
        path.with_suffix(".txt").write_text(
            "\n".join(text) + "\n", encoding="utf-8"
        )
        state.payload["summary_path"] = str(path)
        return path

    def run(self) -> dict[str, Any]:
        self.initialize()
        state = self.run_state
        try:
            if self.replay_ocr:
                payload = json.loads(
                    self.replay_ocr.read_text(encoding="utf-8-sig")
                )
                total = len(payload.get("orders") or [])
                state.payload["screenshot_count"] = total
                state.payload["ocr_count"] = total
                state.payload["ocr_json"] = str(self.replay_ocr)
                state.skip("erp_prepare", "整合重播不操作 ERP")
                state.skip("query_orders", "整合重播使用既有查詢結果")
                state.skip("capture", f"整合重播使用既有 {total} 張結果")
                state.skip("ocr", f"整合重播使用既有 {total} 筆 OCR")
                ocr_json = self.replay_ocr
            else:
                batch, _ = self.run_capture()
                self.assert_continue("截圖完成後")
                ocr_json = self.run_ocr(batch)
            replay = self.write_sqlite(ocr_json)
            summary_path = self.write_summary(replay)
            state.update_stage(
                "complete",
                "completed",
                f"V6 {MODE_LABELS[self.mode]}流程完成",
                100,
            )
            state.payload["status"] = "completed"
            state.payload["progress"] = 100
            state.payload["finished_at"] = local_now().isoformat()
            state.payload["message"] = "完成"
            state.save()
            self.log(f"V6 Phase 3 completed summary={summary_path}")
            return state.payload
        except PipelineStopped as exc:
            key = state.payload["current_stage"]
            current = state.stage(key)
            state.update_stage(
                key,
                "stopped",
                str(exc),
                int(current.get("progress") or 0),
            )
            state.payload["status"] = "stopped"
            state.payload["finished_at"] = local_now().isoformat()
            state.payload["error_reason"] = str(exc)
            state.save()
            self.log(f"STOPPED {exc}")
            return state.payload
        except BaseException as exc:
            key = state.payload["current_stage"]
            current = state.stage(key)
            state.update_stage(
                key,
                "failed",
                str(exc),
                int(current.get("progress") or 0),
            )
            state.payload["status"] = "failed"
            state.payload["finished_at"] = local_now().isoformat()
            state.payload["error_reason"] = str(exc)
            state.save()
            self.log(f"ERROR {type(exc).__name__}: {exc}")
            raise


def run_phase3(
    config_path: str | Path,
    resume: bool = False,
    replay_ocr: str | Path | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    mode: str = "daily_7_days",
) -> dict[str, Any]:
    pipeline = Phase3Pipeline(
        load_config(config_path),
        resume=resume,
        replay_ocr=replay_ocr,
        replay_date_from=date_from,
        replay_date_to=date_to,
        mode=mode,
    )
    return pipeline.run()
