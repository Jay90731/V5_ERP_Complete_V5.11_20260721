from __future__ import annotations

import argparse
import json
import locale
import socket
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

class WebStartupError(RuntimeError):
    pass


def decode_windows_output(value: bytes) -> str:
    for encoding in (locale.getpreferredencoding(False), "utf-8", "cp950"):
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("utf-8", errors="replace")


def port_owner_reason(port: int) -> str:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        output = decode_windows_output(result.stdout)
    except (OSError, subprocess.SubprocessError):
        return "無法取得占用程序資訊"
    pid = ""
    matching_line = ""
    suffix = f":{port}"
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP":
            continue
        if columns[1].endswith(suffix) and columns[3].upper() == "LISTENING":
            matching_line = " ".join(columns)
            pid = columns[-1]
            break
    if not pid:
        return "連接埠正在使用，但 netstat 未回報 LISTENING PID"
    process_name = "未知程序"
    try:
        task = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        task_output = decode_windows_output(task.stdout).strip()
        if task_output and not task_output.startswith("INFO:"):
            process_name = task_output.split(",", 1)[0].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        pass
    return f"PID={pid}, 程序={process_name}, netstat={matching_line}"


def assert_port_available(host: str, port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
    except OSError as error:
        reason = port_owner_reason(port)
        raise WebStartupError(
            f"連接埠 {port} 已被占用，網站未啟動。{reason}；系統錯誤：{error}"
        ) from error
    finally:
        probe.close()


def validate_database(database: Path) -> dict[str, object]:
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as error:
        raise WebStartupError(f"無法開啟正式資料庫：{database}；{error}") from error
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        formal = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM orders o
                JOIN stores s ON s.id=o.store_id
                WHERE o.department_code='4001' AND o.is_voided=0
                  AND o.status='confirmed' AND s.store_code='SR'
                  AND EXISTS(
                    SELECT 1 FROM order_items i
                    WHERE i.order_version_id=o.current_version_id
                  )
                """
            ).fetchone()[0]
        )
        pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
            ).fetchone()[0]
        )
    except sqlite3.Error as error:
        raise WebStartupError(f"正式資料庫結構或內容無法讀取：{error}") from error
    finally:
        connection.close()
    if quick != "ok":
        raise WebStartupError(f"SQLite quick_check 未通過：{quick}")
    return {"quick_check": quick, "formal_orders": formal, "pending": pending}


def preflight(app: Path, database: Path, host: str, port: int) -> dict[str, object]:
    python = Path(sys.executable).resolve()
    required = (
        ("runtime\\python\\python.exe", python),
        ("web_v1\\app.py", app.resolve()),
        ("data\\orders.db", database.resolve()),
    )
    missing = [f"{label}：{path}" for label, path in required if not path.is_file()]
    if missing:
        raise WebStartupError("缺少必要檔案：" + "；".join(missing))
    if python.name.lower() != "python.exe" or "runtime" not in {
        part.lower() for part in python.parts
    }:
        raise WebStartupError(f"未使用封裝內的 runtime Python：{python}")
    assert_port_available(host, port)
    return {
        "python": str(python),
        "app": str(app.resolve()),
        "database": str(database.resolve()),
        "host": host,
        "port": port,
        **validate_database(database.resolve()),
    }


def write_error_log(log_path: Path, error: BaseException, diagnostics: dict | None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "failed_at": datetime.now().astimezone().isoformat(),
        "error_type": type(error).__name__,
        "error": str(error),
        "diagnostics": diagnostics or {},
        "traceback": traceback.format_exc(),
    }
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2))
        stream.write("\n")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=root / "data" / "orders.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--startup-log", type=Path, default=root / "logs" / "web_startup_error.log"
    )
    args = parser.parse_args()
    diagnostics: dict[str, object] | None = None
    try:
        diagnostics = preflight(Path(__file__), args.database, args.host, args.port)
        from wedding_web.server import run

        print("網站啟動前檢查通過：", json.dumps(diagnostics, ensure_ascii=False))
        print("黑色視窗關閉前，網站會持續執行。按 Ctrl+C 可停止網站。")
        run(args.database.resolve(), args.host, args.port, args.open_browser)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"網站啟動失敗：{error}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        write_error_log(args.startup_log.resolve(), error, diagnostics)
        print(f"完整錯誤已寫入：{args.startup_log.resolve()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
