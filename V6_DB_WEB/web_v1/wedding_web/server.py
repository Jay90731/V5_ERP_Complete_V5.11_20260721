from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .repository import Repository
from .write_service import (
    DatabaseWriteLockError,
    ManualReviewConflictError,
    ManualReviewService,
    ManualReviewValidationError,
)


class WeddingOrderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        repository: Repository,
        static_root: Path,
        review_service: ManualReviewService | None = None,
    ):
        super().__init__(address, handler)
        self.repository = repository
        self.static_root = static_root.resolve()
        self.review_service = review_service or ManualReviewService(repository.database)


class Handler(BaseHTTPRequestHandler):
    server: WeddingOrderServer

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.log_date_time_string(), format % args))

    def json_response(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def static_response(self, requested: str) -> None:
        relative = "index.html" if requested in ("", "/") else requested.lstrip("/")
        candidate = (self.server.static_root / relative).resolve()
        try:
            candidate.relative_to(self.server.static_root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def file_response(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(data)

    def json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ManualReviewValidationError("請求長度格式錯誤") from exc
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ManualReviewValidationError("人工修正資料不可為空且不得超過 2 MB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManualReviewValidationError("人工修正 JSON 格式錯誤") from exc
        if not isinstance(payload, dict):
            raise ManualReviewValidationError("人工修正內容必須是物件")
        return payload

    @staticmethod
    def first(query: dict[str, list[str]], key: str, default: str = "") -> str:
        values = query.get(key)
        return values[0] if values else default

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self.json_response(self.server.repository.health())
            elif parsed.path == "/api/filters":
                self.json_response(
                    self.server.repository.filters(
                        self.first(query, "source_system", "ALL")
                    )
                )
            elif parsed.path == "/api/orders":
                self.json_response(
                    self.server.repository.orders(
                        keyword=self.first(query, "keyword"),
                        source_system=self.first(query, "source_system", "ALL"),
                        store_code=self.first(query, "store_code"),
                        date_from=self.first(query, "date_from"),
                        date_to=self.first(query, "date_to"),
                        month=self.first(query, "month"),
                        category=self.first(query, "category"),
                        order_no=self.first(query, "order_no"),
                        product_code=self.first(query, "product_code"),
                        product_name=self.first(query, "product_name"),
                        page=int(self.first(query, "page", "1")),
                        page_size=int(self.first(query, "page_size", "30")),
                    )
                )
            elif parsed.path.startswith("/api/orders/"):
                order_no = unquote(parsed.path[len("/api/orders/") :])
                result = self.server.repository.order_detail(order_no)
                if result is None:
                    self.json_response({"error": "找不到訂單"}, HTTPStatus.NOT_FOUND)
                else:
                    self.json_response(result)
            elif parsed.path == "/api/reviews":
                self.json_response(
                    self.server.repository.reviews(
                        keyword=self.first(query, "keyword"),
                        status=self.first(query, "status", "pending"),
                        page=int(self.first(query, "page", "1")),
                        page_size=int(self.first(query, "page_size", "30")),
                    )
                )
            elif parsed.path.startswith("/api/reviews/"):
                suffix = parsed.path[len("/api/reviews/") :]
                if "/" in suffix:
                    self.json_response({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)
                else:
                    result = self.server.repository.review_detail(int(suffix))
                    if result is None:
                        self.json_response(
                            {"error": "找不到待確認資料"}, HTTPStatus.NOT_FOUND
                        )
                    else:
                        self.json_response(result)
            elif parsed.path.startswith("/api/review-images/"):
                review_id = int(parsed.path[len("/api/review-images/") :])
                image = self.server.repository.review_image(review_id)
                if image is None:
                    self.json_response(
                        {"error": "找不到待確認圖片"}, HTTPStatus.NOT_FOUND
                    )
                else:
                    self.file_response(image)
            elif parsed.path == "/api/statistics":
                self.json_response(
                    self.server.repository.statistics(
                        self.first(query, "month"),
                        source_system=self.first(query, "source_system", "ALL"),
                        store_code=self.first(query, "store_code"),
                        date_from=self.first(query, "date_from"),
                        date_to=self.first(query, "date_to"),
                        category=self.first(query, "category"),
                        order_no=self.first(query, "order_no"),
                        product_code=self.first(query, "product_code"),
                        product_name=self.first(query, "product_name"),
                    )
                )
            elif parsed.path.startswith("/api/"):
                self.json_response({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self.static_response(parsed.path)
        except (ValueError, sqlite3.Error) as error:
            self.json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.json_response({"error": "伺服器處理失敗", "detail": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if self.headers.get("X-Wedding-Write") != "1":
                self.json_response(
                    {"error": "拒絕未授權的本機寫入請求"}, HTTPStatus.FORBIDDEN
                )
                return
            prefix = "/api/reviews/"
            suffix = "/actions"
            if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
                self.json_response({"error": "API 不存在"}, HTTPStatus.NOT_FOUND)
                return
            review_text = parsed.path[len(prefix) : -len(suffix)].strip("/")
            review_id = int(review_text)
            payload = self.json_body()
            result = self.server.review_service.process(
                review_id,
                str(payload.get("action") or ""),
                payload.get("order"),
                str(payload.get("note") or ""),
            )
            result["metrics"] = self.server.repository.health()
            self.json_response(result)
        except ManualReviewValidationError as error:
            self.json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except ManualReviewConflictError as error:
            self.json_response({"error": str(error)}, HTTPStatus.CONFLICT)
        except DatabaseWriteLockError as error:
            self.json_response(
                {
                    "error": "資料庫正在更新，請稍後重試",
                    "detail": str(error),
                },
                HTTPStatus.LOCKED,
            )
        except (ValueError, sqlite3.IntegrityError) as error:
            self.json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.json_response(
                {"error": "人工寫入失敗，資料已回復", "detail": str(error)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def wait_for_health_and_open(url: str, timeout: float = 20.0) -> None:
    health_url = url.rstrip("/") + "/api/health"
    deadline = time.monotonic() + timeout
    last_error = "尚未收到回應"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("status") == "ok":
                    print(f"網站健康檢查通過：{health_url} (HTTP 200)")
                    webbrowser.open(url)
                    return
                last_error = f"HTTP {response.status}, payload={payload}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(0.25)
    print(f"網站已建立服務，但健康檢查逾時，未自動開啟瀏覽器：{last_error}")


def run(database: Path, host: str, port: int, open_browser: bool) -> None:
    static_root = Path(__file__).resolve().parent.parent / "static"
    review_service = ManualReviewService(database)
    review_service.ensure_ready()
    repository = Repository(database)
    health = repository.health()
    if health["quick_check"] != "ok":
        raise RuntimeError("SQLite quick_check 未通過")
    server = WeddingOrderServer(
        (host, port), Handler, repository, static_root, review_service
    )
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"婚約訂單網站已啟動：{url}")
    if open_browser:
        threading.Thread(
            target=wait_for_health_and_open,
            args=(url,),
            name="wedding-web-health-check",
            daemon=True,
        ).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    candidates = (root.parent / "data" / "orders.db", root / "data" / "orders.db")
    default_database = next((path for path in candidates if path.is_file()), candidates[-1])
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=default_database)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    run(args.database.resolve(), args.host, args.port, args.open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
