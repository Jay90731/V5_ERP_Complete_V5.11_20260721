from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


WEB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WEB_ROOT.parent
sys.path.insert(0, str(WEB_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import app as web_app  # noqa: E402
from wedding_web import server as web_server  # noqa: E402


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        return


class WebStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "桌面 測試資料夾"
        self.python = self.root / "runtime" / "python" / "python.exe"
        self.app = self.root / "web_v1" / "app.py"
        self.database = self.root / "data" / "orders.db"
        self.python.parent.mkdir(parents=True)
        self.app.parent.mkdir(parents=True)
        self.database.parent.mkdir(parents=True)
        self.python.write_bytes(b"test")
        self.app.write_text("# test", encoding="utf-8")
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE stores(id INTEGER PRIMARY KEY, store_code TEXT);
            CREATE TABLE orders(
              id INTEGER PRIMARY KEY, department_code TEXT, is_voided INTEGER,
              status TEXT, store_id INTEGER, current_version_id INTEGER
            );
            CREATE TABLE order_items(id INTEGER PRIMARY KEY, order_version_id INTEGER);
            CREATE TABLE review_queue(id INTEGER PRIMARY KEY, status TEXT);
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_preflight_supports_chinese_and_spaces(self):
        with mock.patch.object(web_app.sys, "executable", str(self.python)):
            result = web_app.preflight(
                self.app, self.database, "127.0.0.1", 0
            )
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(result["formal_orders"], 0)

    def test_preflight_rejects_missing_app(self):
        with mock.patch.object(web_app.sys, "executable", str(self.python)):
            with self.assertRaisesRegex(web_app.WebStartupError, "web_v1"):
                web_app.preflight(
                    self.root / "web_v1" / "missing.py",
                    self.database,
                    "127.0.0.1",
                    0,
                )

    def test_preflight_reports_occupied_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with mock.patch.object(web_app.sys, "executable", str(self.python)):
                with self.assertRaisesRegex(
                    web_app.WebStartupError, f"連接埠 {port} 已被占用"
                ):
                    web_app.preflight(
                        self.app, self.database, "127.0.0.1", port
                    )
        finally:
            listener.close()

    def test_error_log_is_utf8_and_contains_traceback(self):
        log = self.root / "logs" / "web_startup_error.log"
        try:
            raise web_app.WebStartupError("測試錯誤")
        except web_app.WebStartupError as error:
            web_app.write_error_log(log, error, {"port": 8765})
        payload = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(payload["error"], "測試錯誤")
        self.assertIn("WebStartupError", payload["traceback"])

    def test_browser_opens_only_after_health_200(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with mock.patch.object(web_server.webbrowser, "open") as open_browser:
                web_server.wait_for_health_and_open(url, timeout=2)
                open_browser.assert_called_once_with(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_batch_launchers_do_not_use_start_command(self):
        normal_path = PROJECT_ROOT / "START_WEDDING_ORDERS_WEB.bat"
        debug = PROJECT_ROOT / "START_WEDDING_ORDERS_WEB_DEBUG.bat"
        normal = normal_path.read_text(encoding="ascii")
        self.assertNotIn("\nstart ", normal.lower())
        self.assertIn('"%PYTHON%" -X utf8', normal)
        self.assertIn("pause", normal.lower())
        self.assertTrue(debug.is_file())
        for launcher in (normal_path, debug):
            launcher.read_text(encoding="ascii")


if __name__ == "__main__":
    unittest.main()
