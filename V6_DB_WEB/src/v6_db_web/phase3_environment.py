from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .phase3_pipeline import (
    load_config,
    resolve_executable,
    resolve_path,
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def check_phase3_environment(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    expected_path = config.root / "components" / "COMPONENT_SOURCES.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8-sig"))
    component_checks: dict[str, dict[str, Any]] = {}
    for relative, expected_hash in expected["files"].items():
        path = config.root / "components" / relative
        actual_hash = file_sha256(path) if path.is_file() else ""
        component_checks[relative] = {
            "exists": path.is_file(),
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "matches": actual_hash == expected_hash,
        }

    def executable(candidates: list[str]) -> tuple[bool, str]:
        try:
            return True, resolve_executable(config.root, candidates)
        except FileNotFoundError:
            return False, ""

    powershell_ok, powershell = executable(
        list(config.raw["powershell_candidates"])
    )
    tesseract_ok, tesseract = executable(
        list(config.raw["tesseract_candidates"])
    )
    tessdata = next(
        (
            resolve_path(config.root, value)
            for value in config.raw["tessdata_candidates"]
            if resolve_path(config.root, value).is_dir()
        ),
        None,
    )
    erp_path = config.path("erp_exe_path")
    database = config.database
    connection = sqlite3.connect(database)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_errors = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        marker_row = connection.execute(
            """
            SELECT setting_value FROM system_settings
            WHERE setting_key = 'database_mode'
            """
        ).fetchone()
        database_marker = str(marker_row[0]) if marker_row else ""
    finally:
        connection.close()

    is_test_mode = config.database_mode == "test"
    checks = {
        "database_target_matches_mode": (
            database.name == "orders_test.db"
            if is_test_mode
            else database.name == "orders.db" and database_marker == "formal"
        ),
        "database_exists": database.is_file(),
        "database_quick_check_ok": quick_check == "ok",
        "foreign_key_errors_zero": foreign_key_errors == 0,
        "erp_executable_exists": erp_path.is_file(),
        "powershell_exists": powershell_ok,
        "python_is_running": Path(sys.executable).is_file(),
        "pillow_available": importlib.util.find_spec("PIL") is not None,
        "tesseract_exists": tesseract_ok,
        "tessdata_exists": tessdata is not None,
        "capture_script_exists": config.path("capture_script").is_file(),
        "ocr_script_exists": config.path("ocr_script").is_file(),
        "component_integrity_ok": all(
            item["matches"] for item in component_checks.values()
        ),
        "no_password_in_config": not any(
            key.lower() in {"password", "account", "username", "credential"}
            for key in config.raw
        ),
        "daily_range_is_7": int(config.raw["daily_range_days"]) == 7,
    }
    replay_required = {
        key: value
        for key, value in checks.items()
        if key
        not in {
            "erp_executable_exists",
            "tesseract_exists",
            "tessdata_exists",
            "pillow_available",
        }
    }
    return {
        "checked_at": datetime.now().astimezone().isoformat(),
        "config": str(Path(config_path).resolve()),
        "database": str(database),
        "database_mode": config.database_mode,
        "database_marker": database_marker,
        "python": sys.executable,
        "powershell": powershell,
        "erp_executable": str(erp_path),
        "tesseract": tesseract,
        "tessdata": str(tessdata) if tessdata else "",
        "quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
        "component_checks": component_checks,
        "checks": checks,
        "ready_for_replay": all(replay_required.values()),
        "ready_for_erp_test": all(checks.values()),
    }


def write_environment_report(
    path: str | Path, report: dict[str, Any]
) -> tuple[Path, Path]:
    json_path = Path(path).resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "V6 第三階段環境檢查",
        f"檢查時間：{report['checked_at']}",
        f"可執行重播：{report['ready_for_replay']}",
        f"可執行 ERP 測試：{report['ready_for_erp_test']}",
        f"測試資料庫：{report['database']}",
        f"SQLite quick_check：{report['quick_check']}",
        "",
    ]
    for key, passed in report["checks"].items():
        lines.append(f"{'[OK]' if passed else '[X ]'} {key}")
    text_path = json_path.with_suffix(".txt")
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, text_path
