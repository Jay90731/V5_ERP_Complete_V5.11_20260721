from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_watcher import (  # noqa: E402
    BhqFolderWatcher,
    WatcherConfig,
    install_stop_handlers,
    validate_database_ready,
)
from v6_db_web.database_lock import database_write_lock  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="本機 BHQ Excel 資料夾監看服務")
    parser.add_argument("--config", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--once", action="store_true", help="只掃描一次後結束")
    action.add_argument(
        "--validate-only", action="store_true", help="只驗證設定與資料庫，不監看"
    )
    action.add_argument(
        "--validate-database-only",
        action="store_true",
        help="先驗證固定正式路徑及既有資料庫，不建立監看資料夾",
    )
    action.add_argument("--status", action="store_true", help="顯示監看服務狀態")
    action.add_argument(
        "--retry-failed", action="store_true", help="人工將已登錄失敗檔排回 INBOX"
    )
    action.add_argument(
        "--drain", action="store_true", help="等待 INBOX/Processing 處理完畢後結束"
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--enable-formal-import",
        action="store_true",
        help="正式模式第二道啟用開關；測試時不可使用",
    )
    args = parser.parse_args()
    config = WatcherConfig.load(args.config)
    config.validate(
        ROOT,
        cli_formal_enabled=args.enable_formal_import,
        require_runtime_folders=not args.validate_database_only,
    )
    database_ready = validate_database_ready(config)
    if args.validate_database_only:
        print(
            json.dumps(
                {
                    "status": "database_valid",
                    "mode": config.mode,
                    "database": str(config.database),
                    "database_ready": database_ready,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    watcher = BhqFolderWatcher(config, ROOT)
    if args.status:
        status_file = config.resolved_status_file()
        payload = (
            json.loads(status_file.read_text(encoding="utf-8"))
            if status_file.is_file()
            else {"status": "not_started", "status_file": str(status_file)}
        )
        payload["database_ready"] = database_ready
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.validate_only:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "mode": config.mode,
                    "watch_folder": str(config.watch_folder),
                    "database": str(config.database),
                    "state_file": str(config.state_file),
                    "reports_folder": str(config.reports_folder),
                    "database_ready": database_ready,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    with database_write_lock(
        config.resolved_instance_lock_file(), owner="BHQ watcher single instance"
    ):
        if args.retry_failed:
            print(
                json.dumps(watcher.retry_failed_files(), ensure_ascii=False, indent=2)
            )
            return 0
        if args.once:
            print(json.dumps(watcher.scan_once(), ensure_ascii=False, indent=2))
            return 0
        if args.drain:
            deadline = time.monotonic() + max(1, args.timeout)
            all_outcomes = []
            while time.monotonic() < deadline:
                all_outcomes.extend(watcher.scan_once())
                pending = len(watcher._eligible_inbox_files()) + len(
                    watcher._processing_files()
                )
                if pending == 0:
                    print(json.dumps(all_outcomes, ensure_ascii=False, indent=2))
                    return 0
                time.sleep(min(config.scan_interval_seconds, 1.0))
            print(json.dumps(all_outcomes, ensure_ascii=False, indent=2))
            print("BHQ drain 等待逾時，檔案仍保留待下次處理", file=sys.stderr)
            return 3
        stop_event = threading.Event()
        install_stop_handlers(stop_event)
        print(
            f"BHQ 監看服務已啟動：mode={config.mode}, folder={config.watch_folder}",
            flush=True,
        )
        try:
            watcher.run_forever(stop_event)
        except KeyboardInterrupt:
            stop_event.set()
        print("BHQ 監看服務已停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
