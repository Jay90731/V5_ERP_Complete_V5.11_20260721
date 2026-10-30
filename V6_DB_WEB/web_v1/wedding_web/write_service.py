from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


SHARED_SOURCE = Path(__file__).resolve().parents[2] / "src"
if str(SHARED_SOURCE) not in sys.path:
    sys.path.insert(0, str(SHARED_SOURCE))

from v6_db_web.canonical import (  # noqa: E402
    canonical_content as shared_canonical_content,
    content_hash as shared_content_hash,
    item_hash as shared_item_hash,
)
from v6_db_web.database_lock import (  # noqa: E402
    DatabaseWriteLockError,
    database_write_lock,
)
from v6_db_web.db import connect as shared_connect  # noqa: E402


ORDER_NUMBER_PATTERN = re.compile(r"^GOU[0-9A-Z-]{6,20}$")
MANUAL_SCHEMA_VERSION = 2
MANUAL_SCHEMA_NAME = "web_v1_manual_review_writeback"


class ManualReviewError(RuntimeError):
    pass


class ManualReviewValidationError(ManualReviewError):
    pass


class ManualReviewConflictError(ManualReviewError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    relative_path: str
    sha256: str
    size: int
    created_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_order_no(value: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(value).upper())


def normalize_date(value: Any) -> str:
    text = normalize_text(value)
    digits = re.sub(r"\D", "", text)
    candidates = [text]
    if len(digits) == 8:
        candidates.append(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
    for candidate in candidates:
        candidate = re.sub(r"[./]", "-", candidate)
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            continue
    raise ManualReviewValidationError("訂貨日期格式錯誤，請使用有效日期")


def normalize_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ManualReviewValidationError(f"{label}必須是整數")
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    text = text.replace(",", "").replace("$", "").replace("NT", "").strip()
    if not text:
        raise ManualReviewValidationError(f"{label}不可空白")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ManualReviewValidationError(f"{label}必須是整數") from exc
    if number != number.to_integral_value():
        raise ManualReviewValidationError(f"{label}必須是整數")
    result = int(number)
    if result < minimum:
        raise ManualReviewValidationError(f"{label}不可小於{minimum}")
    return result


def normalize_manual_order(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ManualReviewValidationError("缺少人工修正表單")
    order_no = normalize_order_no(payload.get("order_no"))
    if not ORDER_NUMBER_PATTERN.fullmatch(order_no):
        raise ManualReviewValidationError("訂單編號必須是有效的 GOU 訂單編號")
    category = normalize_text(payload.get("category"))
    if not category:
        raise ManualReviewValidationError("訂單分類不可空白")
    department_code = normalize_text(payload.get("department_code"))
    if department_code != "4001":
        raise ManualReviewValidationError("正式訂單的部門代號必須是 4001")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManualReviewValidationError("正式訂單至少需要一筆品項")
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ManualReviewValidationError(f"第{index}筆品項格式錯誤")
        gift_code = normalize_text(raw.get("gift_code"))
        if not gift_code:
            raise ManualReviewValidationError(f"第{index}筆品項代號不可空白")
        item = {
            "gift_code": gift_code,
            "product_name": normalize_text(raw.get("product_name")),
            "list_price": normalize_integer(raw.get("list_price"), f"第{index}筆單價"),
            "quantity": normalize_integer(raw.get("quantity"), f"第{index}筆數量", minimum=1),
            "total_amount": normalize_integer(raw.get("total_amount"), f"第{index}筆金額"),
            "discount_amount": normalize_integer(raw.get("discount_amount"), f"第{index}筆折後金額"),
        }
        if item["discount_amount"] > item["total_amount"]:
            raise ManualReviewValidationError(f"第{index}筆折後金額不可大於金額")
        items.append(item)
    return {
        "order_no": order_no,
        "order_date": normalize_date(payload.get("order_date")),
        "category": category,
        "department_code": department_code,
        "customer_name": normalize_text(payload.get("customer_name")),
        "store_code": "SR",
        "is_voided": False,
        "items": items,
    }


def shared_order_shape(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_no": order.get("order_no", ""),
        "order_date": order["order_date"],
        "category": order["category"],
        "department_code": order["department_code"],
        "is_voided": bool(order["is_voided"]),
        "details": [
            {
                key: item[key]
                for key in (
                    "gift_code",
                    "list_price",
                    "quantity",
                    "total_amount",
                    "discount_amount",
                )
            }
            for item in order["items"]
        ],
    }


def canonical_manual_order(order: dict[str, Any]) -> dict[str, Any]:
    return shared_canonical_content(shared_order_shape(order))


def manual_content_hash(order: dict[str, Any]) -> str:
    return shared_content_hash(shared_order_shape(order))


def item_hash(item: dict[str, Any]) -> str:
    return shared_item_hash({
        key: item[key]
        for key in ("gift_code", "list_price", "quantity", "total_amount", "discount_amount")
    })


def _database_root(database: Path) -> Path:
    return database.parent.parent if database.parent.name.lower() == "data" else database.parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def create_verified_backup(database: Path, review_id: int | str) -> BackupResult:
    database = database.resolve()
    root = _database_root(database)
    backup_dir = root / "backups" / "manual_review"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir / f"orders_before_review_{review_id}_{stamp}.db"
    temporary = destination.with_suffix(".db.tmp")
    source = sqlite3.connect(database)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(temporary)
    try:
        quick = check.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = check.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        check.close()
    if quick != "ok" or foreign_keys:
        temporary.unlink(missing_ok=True)
        raise ManualReviewError("人工寫入前備份驗證失敗")
    os.replace(temporary, destination)
    created = utc_now()
    return BackupResult(
        path=destination,
        relative_path=destination.relative_to(root).as_posix(),
        sha256=sha256_file(destination),
        size=destination.stat().st_size,
        created_at=created,
    )


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def ensure_manual_schema(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS web_pending_images(
          review_queue_id INTEGER PRIMARY KEY REFERENCES review_queue(id) ON DELETE CASCADE,
          relative_path TEXT NOT NULL UNIQUE,
          sha256 TEXT NOT NULL,
          source_relative_path TEXT,
          copied_at TEXT NOT NULL,
          processed_at TEXT,
          retained_until TEXT,
          image_status TEXT NOT NULL DEFAULT 'available'
            CHECK(image_status IN('available','missing','deleted'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS web_order_version_metadata(
          order_version_id INTEGER PRIMARY KEY REFERENCES order_versions(id) ON DELETE CASCADE,
          customer_name TEXT NOT NULL DEFAULT '',
          review_queue_id INTEGER REFERENCES review_queue(id) ON DELETE SET NULL,
          manual_note TEXT,
          created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS web_order_item_metadata(
          order_item_id INTEGER PRIMARY KEY REFERENCES order_items(id) ON DELETE CASCADE,
          product_name TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS web_manual_review_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          review_queue_id INTEGER NOT NULL REFERENCES review_queue(id),
          action TEXT NOT NULL CHECK(action IN('confirmed','excluded','held')),
          order_no TEXT,
          write_result TEXT NOT NULL,
          backup_relative_path TEXT NOT NULL,
          before_json TEXT NOT NULL,
          after_json TEXT NOT NULL,
          note TEXT,
          actor TEXT NOT NULL,
          source TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_web_manual_events_review ON web_manual_review_events(review_queue_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_web_version_customer ON web_order_version_metadata(customer_name)",
    ]
    for statement in statements:
        connection.execute(statement)
    pending_columns = _column_names(connection, "web_pending_images")
    additions = {
        "processed_at": "TEXT",
        "retained_until": "TEXT",
        "image_status": "TEXT NOT NULL DEFAULT 'available' CHECK(image_status IN('available','missing','deleted'))",
    }
    for name, declaration in additions.items():
        if name not in pending_columns:
            connection.execute(f"ALTER TABLE web_pending_images ADD COLUMN {name} {declaration}")
    connection.execute(
        """
        INSERT OR IGNORE INTO web_schema_migrations(version, name, applied_at)
        VALUES(?, ?, ?)
        """,
        (MANUAL_SCHEMA_VERSION, MANUAL_SCHEMA_NAME, utc_now()),
    )


def manual_schema_ready(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM web_schema_migrations WHERE version=? AND name=?",
            (MANUAL_SCHEMA_VERSION, MANUAL_SCHEMA_NAME),
        ).fetchone()
        return bool(row and "retained_until" in _column_names(connection, "web_pending_images"))
    except sqlite3.Error:
        return False


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


class ManualReviewService:
    def __init__(
        self,
        database: Path,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self.database = database.resolve()
        self.fault_injector = fault_injector

    def connect(self) -> sqlite3.Connection:
        return shared_connect(self.database)

    def ensure_ready(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            if manual_schema_ready(connection):
                return {"migrated": False, "backup": None}
        with database_write_lock(self.database, "manual review schema migration"):
            with closing(self.connect()) as connection:
                if manual_schema_ready(connection):
                    return {"migrated": False, "backup": None}
            backup = create_verified_backup(self.database, "schema_v2")
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                ensure_manual_schema(connection)
                connection.execute(
                    """
                    INSERT INTO audit_logs(event_type, entity_type, entity_id, actor, details_json, created_at)
                    VALUES('web_manual_schema_migrated','database',NULL,'website_startup',?,?)
                    """,
                    (json_text({"backup": backup.relative_path, "version": MANUAL_SCHEMA_VERSION}), utc_now()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return {"migrated": True, "backup": backup.relative_path}

    def _review_row(self, connection: sqlite3.Connection, review_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM review_queue WHERE id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise ManualReviewValidationError("找不到待確認資料")
        if row["status"] not in ("pending", "retry_requested"):
            raise ManualReviewConflictError("這筆待確認資料已經處理，請重新整理頁面")
        return row

    def _current_order(self, connection: sqlite3.Connection, order_no: str) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT o.id, o.order_no, o.order_date, o.category, o.department_code,
                   o.is_voided, o.current_version_id,
                   COALESCE(vmeta.customer_name, ometa.customer_name, '') customer_name
            FROM orders o
            JOIN order_versions v ON v.id=o.current_version_id
            LEFT JOIN web_order_version_metadata vmeta ON vmeta.order_version_id=v.id
            LEFT JOIN web_order_metadata ometa ON ometa.order_id=o.id
            WHERE o.order_no=?
            """,
            (order_no,),
        ).fetchone()
        if row is None:
            return None
        items = connection.execute(
            """
            SELECT i.gift_code,
                   COALESCE(imeta.product_name, p.product_name, '') product_name,
                   i.list_price, i.quantity, i.total_amount, i.discount_amount
            FROM order_items i
            LEFT JOIN web_order_item_metadata imeta ON imeta.order_item_id=i.id
            LEFT JOIN products p ON p.id=i.product_id
            WHERE i.order_version_id=? ORDER BY i.line_no, i.id
            """,
            (row["current_version_id"],),
        ).fetchall()
        return {
            "id": int(row["id"]),
            "version_id": int(row["current_version_id"]),
            "order_no": row["order_no"],
            "order_date": row["order_date"],
            "category": row["category"],
            "department_code": row["department_code"],
            "customer_name": row["customer_name"],
            "store_code": "SR",
            "is_voided": bool(row["is_voided"]),
            "items": [_row_dict(item) for item in items],
        }

    def _sr_store_id(self, connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            "SELECT id FROM stores WHERE store_code='SR' AND active=1 ORDER BY id"
        ).fetchall()
        if len(rows) != 1:
            raise ManualReviewValidationError("SR 門市主檔必須且只能有一筆有效資料")
        return int(rows[0][0])

    def _ensure_product(
        self, connection: sqlite3.Connection, gift_code: str, product_name: str, timestamp: str
    ) -> int:
        existing = connection.execute(
            "SELECT id, product_name FROM products WHERE gift_code=?", (gift_code,)
        ).fetchone()
        if existing is not None:
            if not normalize_text(existing["product_name"]) and product_name:
                connection.execute(
                    "UPDATE products SET product_name=?, updated_at=? WHERE id=?",
                    (product_name, timestamp, int(existing["id"])),
                )
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO products(gift_code, product_name, active, source, created_at, updated_at)
            VALUES(?,?,1,'manual_review',?,?)
            """,
            (gift_code, product_name or None, timestamp, timestamp),
        )
        return int(cursor.lastrowid)

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        order_id: int,
        review_id: int,
        order: dict[str, Any],
        note: str,
        timestamp: str,
    ) -> tuple[int, bool]:
        digest = manual_content_hash(order)
        existing = connection.execute(
            "SELECT id FROM order_versions WHERE order_id=? AND content_hash=?",
            (order_id, digest),
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False
        version_no = int(
            connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM order_versions WHERE order_id=?",
                (order_id,),
            ).fetchone()[0]
        )
        total_quantity = sum(item["quantity"] for item in order["items"])
        total_amount = sum(item["total_amount"] for item in order["items"])
        discount_amount = sum(item["discount_amount"] for item in order["items"])
        source_payload = {"source": "website_manual_review", "review_id": review_id, **order}
        cursor = connection.execute(
            """
            INSERT INTO order_versions(
              order_id, version_no, content_hash, order_date, category,
              department_code, is_voided, detail_count, total_quantity,
              total_list_amount, total_discount_amount, source_system,
              source_version_id, source_payload_json, effective_at, created_at
            ) VALUES(?,?,?,?,?,?,0,?,?,?,?,? ,NULL,?,?,?)
            """,
            (
                order_id, version_no, digest, order["order_date"], order["category"],
                order["department_code"], len(order["items"]), total_quantity,
                total_amount, discount_amount, "website_manual_review",
                json_text(source_payload), timestamp, timestamp,
            ),
        )
        version_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO web_order_version_metadata(
              order_version_id, customer_name, review_queue_id, manual_note, created_at
            ) VALUES(?,?,?,?,?)
            """,
            (version_id, order["customer_name"], review_id, note, timestamp),
        )
        for line_no, item in enumerate(order["items"], start=1):
            product_id = self._ensure_product(
                connection, item["gift_code"], item["product_name"], timestamp
            )
            item_cursor = connection.execute(
                """
                INSERT INTO order_items(
                  order_version_id, line_no, gift_code, product_id, list_price,
                  quantity, total_amount, discount_amount, item_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id, line_no, item["gift_code"], product_id,
                    item["list_price"], item["quantity"], item["total_amount"],
                    item["discount_amount"], item_hash(item), timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO web_order_item_metadata(order_item_id, product_name, created_at) VALUES(?,?,?)",
                (int(item_cursor.lastrowid), item["product_name"], timestamp),
            )
        return version_id, True

    def _sync_manual_metadata(
        self,
        connection: sqlite3.Connection,
        order_id: int,
        version_id: int,
        review_id: int,
        order: dict[str, Any],
        note: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO web_order_metadata(order_id, customer_name, customer_source, updated_at)
            VALUES(?,?,'manual_review',?)
            ON CONFLICT(order_id) DO UPDATE SET customer_name=excluded.customer_name,
              customer_source=excluded.customer_source, updated_at=excluded.updated_at
            """,
            (order_id, order["customer_name"], timestamp),
        )
        connection.execute(
            """
            INSERT INTO web_order_version_metadata(
              order_version_id, customer_name, review_queue_id, manual_note, created_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(order_version_id) DO UPDATE SET
              customer_name=excluded.customer_name,
              review_queue_id=excluded.review_queue_id,
              manual_note=excluded.manual_note
            """,
            (version_id, order["customer_name"], review_id, note, timestamp),
        )
        names_by_item: dict[tuple[Any, ...], list[str]] = {}
        for item in order["items"]:
            key = (
                item["gift_code"],
                item["list_price"],
                item["quantity"],
                item["total_amount"],
                item["discount_amount"],
            )
            names_by_item.setdefault(key, []).append(item["product_name"])
        rows = connection.execute(
            """
            SELECT id, gift_code, list_price, quantity, total_amount, discount_amount
            FROM order_items WHERE order_version_id=? ORDER BY line_no, id
            """,
            (version_id,),
        ).fetchall()
        for row in rows:
            key = (
                row["gift_code"],
                row["list_price"],
                row["quantity"],
                row["total_amount"],
                row["discount_amount"],
            )
            candidates = names_by_item.get(key, [])
            product_name = candidates.pop(0) if candidates else ""
            product_id = self._ensure_product(
                connection, str(row["gift_code"]), product_name, timestamp
            )
            connection.execute(
                "UPDATE order_items SET product_id=? WHERE id=?",
                (product_id, int(row["id"])),
            )
            connection.execute(
                """
                INSERT INTO web_order_item_metadata(order_item_id, product_name, created_at)
                VALUES(?,?,?)
                ON CONFLICT(order_item_id) DO UPDATE SET product_name=excluded.product_name
                """,
                (int(row["id"]), product_name, timestamp),
            )

    def _write_confirmed_order(
        self,
        connection: sqlite3.Connection,
        review_id: int,
        order: dict[str, Any],
        note: str,
        timestamp: str,
    ) -> tuple[str, int, int]:
        current = self._current_order(connection, order["order_no"])
        same = current is not None and canonical_manual_order(current) == canonical_manual_order(order)
        sr_id = self._sr_store_id(connection)
        digest = manual_content_hash(order)
        if current is None:
            cursor = connection.execute(
                """
                INSERT INTO orders(
                  order_no, current_version_id, current_content_hash, order_date,
                  category, department_code, store_id, is_voided, status,
                  first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES(?,NULL,?,?,?,?,?,0,'confirmed',?,?,?,?)
                """,
                (
                    order["order_no"], digest, order["order_date"], order["category"],
                    order["department_code"], sr_id, timestamp, timestamp, timestamp, timestamp,
                ),
            )
            order_id = int(cursor.lastrowid)
            result = "new"
        else:
            order_id = int(current["id"])
            result = "unchanged" if same else "changed"
        if same:
            version_id = int(current["version_id"])
        else:
            version_id, inserted = self._insert_version(
                connection, order_id, review_id, order, note, timestamp
            )
            if result == "changed" and not inserted:
                result = "changed_reverted"
        connection.execute(
            """
            UPDATE orders SET current_version_id=?, current_content_hash=?,
              order_date=?, category=?, department_code=?, store_id=?, is_voided=0,
              status='confirmed', last_seen_at=?, updated_at=? WHERE id=?
            """,
            (
                version_id, digest, order["order_date"], order["category"],
                order["department_code"], sr_id, timestamp, timestamp, order_id,
            ),
        )
        self._sync_manual_metadata(
            connection,
            order_id,
            version_id,
            review_id,
            order,
            note,
            timestamp,
        )
        return result, order_id, version_id

    def _record_backup(self, connection: sqlite3.Connection, backup: BackupResult) -> None:
        connection.execute(
            """
            INSERT INTO backup_records(
              backup_type, relative_path, sha256, database_size, status,
              created_at, verified_at, notes
            ) VALUES('pre_update',?,?,?,'verified',?,?,?)
            """,
            (
                backup.relative_path, backup.sha256, backup.size,
                backup.created_at, backup.created_at, "Before website manual review write",
            ),
        )

    def _retain_review_image(
        self, connection: sqlite3.Connection, review_id: int, timestamp: str
    ) -> None:
        retained_until = (
            datetime.fromisoformat(timestamp).astimezone(timezone.utc) + timedelta(days=7)
        ).isoformat()
        connection.execute(
            """
            UPDATE web_pending_images SET processed_at=?, retained_until=?, image_status='available'
            WHERE review_queue_id=?
            """,
            (timestamp, retained_until, review_id),
        )

    def _update_scan_item(
        self,
        connection: sqlite3.Connection,
        review: sqlite3.Row,
        *,
        status: str,
        result: str,
        order_id: int | None,
        version_id: int | None,
        reason: str,
        timestamp: str,
    ) -> None:
        if review["scan_item_id"] is None:
            return
        connection.execute(
            """
            UPDATE scan_items SET status=?, result_code=?, order_id=?, order_version_id=?,
              reason=?, finished_at=COALESCE(finished_at,?), processed_at=? WHERE id=?
            """,
            (
                status, result, order_id, version_id, reason,
                timestamp, timestamp, int(review["scan_item_id"]),
            ),
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        review_id: int,
        action: str,
        order_no: str | None,
        result: str,
        backup: BackupResult,
        before: dict[str, Any],
        after: dict[str, Any],
        note: str,
        timestamp: str,
    ) -> None:
        actor = "local_web_user"
        source = "website_manual_review"
        connection.execute(
            """
            INSERT INTO web_manual_review_events(
              review_queue_id, action, order_no, write_result, backup_relative_path,
              before_json, after_json, note, actor, source, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id, action, order_no, result, backup.relative_path,
                json_text(before), json_text(after), note, actor, source, timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_logs(event_type, entity_type, entity_id, actor, details_json, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                f"manual_review_{action}", "review_queue", str(review_id), actor,
                json_text(
                    {
                        "source": source,
                        "backup": backup.relative_path,
                        "order_no": order_no,
                        "write_result": result,
                        "original_value": before,
                        "modified_value": after,
                    }
                ),
                timestamp,
            ),
        )

    def process(
        self,
        review_id: int,
        action: str,
        payload: dict[str, Any] | None,
        note: str = "",
    ) -> dict[str, Any]:
        action = normalize_text(action).lower()
        if action not in {"confirm", "exclude", "hold"}:
            raise ManualReviewValidationError("不支援的人工處理動作")
        note = normalize_text(note)
        with database_write_lock(self.database, f"manual review {review_id}"):
            connection = self.connect()
            try:
                review = self._review_row(connection, review_id)
                before_order_no = normalize_order_no(
                    (payload or {}).get("order_no") or review["order_no"]
                )
                before = {
                    "review": _row_dict(review),
                    "current_order": self._current_order(connection, before_order_no)
                    if before_order_no
                    else None,
                }
            finally:
                connection.close()
            backup = create_verified_backup(self.database, review_id)
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                review = self._review_row(connection, review_id)
                self._record_backup(connection, backup)
                timestamp = utc_now()
                order_id: int | None = None
                version_id: int | None = None
                order_no = normalize_order_no(review["order_no"]) or None
                if action == "confirm":
                    order = normalize_manual_order(payload)
                    order_no = order["order_no"]
                    result, order_id, version_id = self._write_confirmed_order(
                        connection, review_id, order, note, timestamp
                    )
                    if self.fault_injector:
                        self.fault_injector("after_order_write")
                    connection.execute(
                        """
                        UPDATE review_queue SET order_no=?, status='resolved',
                          processing_note=?, resolved_at=?, last_seen_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            order_no, note or "人工確認並寫入正式訂單",
                            timestamp, timestamp, timestamp, review_id,
                        ),
                    )
                    self._retain_review_image(connection, review_id, timestamp)
                    self._update_scan_item(
                        connection, review, status="imported", result=result,
                        order_id=order_id, version_id=version_id,
                        reason="人工確認寫入", timestamp=timestamp,
                    )
                    event_action = "confirmed"
                elif action == "exclude":
                    result = "excluded"
                    connection.execute(
                        """
                        UPDATE review_queue SET status='excluded', processing_note=?,
                          resolved_at=?, last_seen_at=?, updated_at=? WHERE id=?
                        """,
                        (
                            note or "人工確認不納入", timestamp, timestamp,
                            timestamp, review_id,
                        ),
                    )
                    self._retain_review_image(connection, review_id, timestamp)
                    self._update_scan_item(
                        connection, review, status="skipped", result="excluded",
                        order_id=None, version_id=None,
                        reason=note or "人工確認不納入", timestamp=timestamp,
                    )
                    event_action = "excluded"
                else:
                    result = "held"
                    connection.execute(
                        """
                        UPDATE review_queue SET status='pending', processing_note=?,
                          last_seen_at=?, updated_at=? WHERE id=?
                        """,
                        (note or "暫時保留待確認", timestamp, timestamp, review_id),
                    )
                    self._update_scan_item(
                        connection, review, status="pending_review", result="pending",
                        order_id=None, version_id=None,
                        reason=note or str(review["reason"]), timestamp=timestamp,
                    )
                    event_action = "held"
                after_review = connection.execute(
                    "SELECT * FROM review_queue WHERE id=?", (review_id,)
                ).fetchone()
                after = {
                    "review": _row_dict(after_review),
                    "current_order": self._current_order(connection, order_no)
                    if order_no
                    else None,
                }
                self._event(
                    connection,
                    review_id=review_id,
                    action=event_action,
                    order_no=order_no,
                    result=result,
                    backup=backup,
                    before=before,
                    after=after,
                    note=note,
                    timestamp=timestamp,
                )
                if self.fault_injector:
                    self.fault_injector("before_commit")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return {
            "review_id": review_id,
            "action": action,
            "result": result,
            "order_no": order_no,
            "order_id": order_id,
            "version_id": version_id,
            "backup": backup.relative_path,
        }
