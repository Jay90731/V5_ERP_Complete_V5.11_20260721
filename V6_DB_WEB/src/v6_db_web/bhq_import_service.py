from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .importer import ensure_order, json_text


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def bhq_content_hash(order: dict[str, Any]) -> str:
    content = {
        "order_date": order["order_date"],
        "category": order["category"],
        "discounted_amount": order["discounted_amount"],
        "source_store_name": order["source_store_name"],
        "items": [
            {
                "line_no": item["line_no"],
                "product_code": item["product_code"],
                "product_name": item["product_name"],
                "quantity": item["quantity"],
            }
            for item in order["items"]
        ],
    }
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bhq_item_hash(item: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "line_no": item["line_no"],
            "product_code": item["product_code"],
            "product_name": item["product_name"],
            "quantity": item["quantity"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class BhqImportStats:
    import_run_id: int | None = None
    inserted_orders: int = 0
    inserted_versions: int = 0
    inserted_items: int = 0
    inserted_stores: int = 0
    inserted_products: int = 0
    filled_product_names: int = 0
    skipped_orders: int = 0
    anomalies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class BhqImportError(RuntimeError):
    pass


def _require_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(order_versions)")
    }
    if "discounted_total_amount" not in columns:
        raise BhqImportError("BHQ migration 007 尚未套用")
    for table in ("bhq_import_runs", "bhq_store_aliases", "bhq_order_version_metadata"):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            raise BhqImportError(f"BHQ migration 缺少資料表：{table}")


def _ensure_stores(
    connection: sqlite3.Connection,
    parsed: dict[str, Any],
    timestamp: str,
    stats: BhqImportStats,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_name, values in sorted(parsed["stores"].items()):
        code = values["store_code"]
        if not code:
            raise BhqImportError(f"未知 BHQ 門市：{raw_name}")
        row = connection.execute(
            "SELECT id, store_name FROM stores WHERE store_code=?", (code,)
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO stores(store_code,store_name,active,created_at,updated_at,status)
                VALUES(?,?,1,?,?,'active')
                """,
                (code, raw_name, timestamp, timestamp),
            )
            store_id = int(cursor.lastrowid)
            stats.inserted_stores += 1
        else:
            store_id = int(row["id"])
            if str(row["store_name"]) != raw_name:
                raise BhqImportError(
                    f"門市代碼 {code} 已對應不同名稱：{row['store_name']} != {raw_name}"
                )
        alias = connection.execute(
            "SELECT store_id FROM bhq_store_aliases WHERE source_store_name=?", (raw_name,)
        ).fetchone()
        if alias is None:
            connection.execute(
                "INSERT INTO bhq_store_aliases(source_store_name,store_id,created_at,updated_at) VALUES(?,?,?,?)",
                (raw_name, store_id, timestamp, timestamp),
            )
        elif int(alias["store_id"]) != store_id:
            raise BhqImportError(f"BHQ 門市別名錯配：{raw_name}")
        result[raw_name] = store_id
    return result


def _ensure_product(
    connection: sqlite3.Connection,
    code: str,
    name: str,
    timestamp: str,
    stats: BhqImportStats,
) -> int:
    row = connection.execute(
        "SELECT id, product_name, source FROM products WHERE gift_code=?", (code,)
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """
            INSERT INTO products(gift_code,product_name,active,source,created_at,updated_at)
            VALUES(?,?,1,'BHQ',?,?)
            """,
            (code, name or None, timestamp, timestamp),
        )
        product_id = int(cursor.lastrowid)
        stats.inserted_products += 1
        connection.execute(
            "INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at) VALUES('bhq_product_created','product',?,'bhq_import',?,?)",
            (str(product_id), json_text({"gift_code": code, "product_name": name}), timestamp),
        )
        return product_id
    product_id = int(row["id"])
    old_name = str(row["product_name"] or "").strip()
    if not old_name and name:
        connection.execute(
            "UPDATE products SET product_name=?,updated_at=? WHERE id=?",
            (name, timestamp, product_id),
        )
        stats.filled_product_names += 1
        connection.execute(
            "INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at) VALUES('bhq_product_name_filled','product',?,'bhq_import',?,?)",
            (
                str(product_id),
                json_text(
                    {"gift_code": code, "old_name": old_name, "new_name": name, "source_preserved": row["source"]}
                ),
                timestamp,
            ),
        )
    return product_id


def _insert_version(
    connection: sqlite3.Connection,
    order_id: int,
    order: dict[str, Any],
    digest: str,
    import_run_id: int,
    parsed: dict[str, Any],
    timestamp: str,
    stats: BhqImportStats,
) -> int:
    version_no = int(
        connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM order_versions WHERE order_id=?",
            (order_id,),
        ).fetchone()[0]
    )
    payload = {
        "source_filename": parsed["source_filename"],
        "source_file_sha256": parsed["file_sha256"],
        "sheet_name": parsed["sheet_name"],
        "order": order,
    }
    cursor = connection.execute(
        """
        INSERT INTO order_versions(
          order_id,version_no,content_hash,order_date,category,department_code,
          is_voided,detail_count,total_quantity,total_list_amount,
          total_discount_amount,source_system,source_version_id,
          source_payload_json,effective_at,created_at,discounted_total_amount
        ) VALUES(?,?,?,?,?,'BHQ',0,?,?,0,0,'BHQ',?,?,?,?,?)
        """,
        (
            order_id,
            version_no,
            digest,
            order["order_date"],
            order["category"],
            len(order["items"]),
            sum(item["quantity"] for item in order["items"]),
            f"{parsed['file_sha256']}:{order['order_no']}",
            json_text(payload),
            timestamp,
            timestamp,
            order["discounted_amount"],
        ),
    )
    version_id = int(cursor.lastrowid)
    for item in order["items"]:
        product_id = _ensure_product(
            connection, item["product_code"], item["product_name"], timestamp, stats
        )
        item_cursor = connection.execute(
            """
            INSERT INTO order_items(
              order_version_id,line_no,gift_code,product_id,list_price,quantity,
              total_amount,discount_amount,item_hash,created_at
            ) VALUES(?,?,?,?,0,?,0,0,?,?)
            """,
            (
                version_id,
                item["line_no"],
                item["product_code"],
                product_id,
                item["quantity"],
                bhq_item_hash(item),
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO web_order_item_metadata(order_item_id,product_name,created_at) VALUES(?,?,?)",
            (int(item_cursor.lastrowid), item["product_name"], timestamp),
        )
    connection.execute(
        """
        INSERT INTO bhq_order_version_metadata(
          order_version_id,import_run_id,raw_store_name,original_total_amount,
          source_rows_json,created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            version_id,
            import_run_id,
            order["source_store_name"],
            order["discounted_amount"],
            json_text(order["source_rows"]),
            timestamp,
        ),
    )
    stats.inserted_versions += 1
    stats.inserted_items += len(order["items"])
    return version_id


def import_bhq(
    connection: sqlite3.Connection,
    parsed: dict[str, Any],
    fault_injector: Callable[[str, int], None] | None = None,
) -> BhqImportStats:
    _require_schema(connection)
    if parsed["anomaly_count"]:
        raise BhqImportError(f"BHQ dry-run 含 {parsed['anomaly_count']} 筆異常")
    started = utc_now()
    stats = BhqImportStats()
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """
            INSERT INTO bhq_import_runs(
              source_filename,source_file_sha256,sheet_name,started_at,
              source_row_count,source_order_count,result,summary_json,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'running','{}',?,?)
            """,
            (
                parsed["source_filename"], parsed["file_sha256"], parsed["sheet_name"],
                started, parsed["source_row_count"], parsed["order_count"], started, started,
            ),
        )
        stats.import_run_id = int(cursor.lastrowid)
        connection.execute("SAVEPOINT bhq_file_batch")
        try:
            stores = _ensure_stores(connection, parsed, started, stats)
            for order_index, order in enumerate(parsed["orders"], start=1):
                digest = bhq_content_hash(order)
                current = connection.execute(
                    """
                    SELECT o.id,o.current_content_hash,o.current_version_id,v.source_system
                    FROM orders o LEFT JOIN order_versions v ON v.id=o.current_version_id
                    WHERE o.order_no=?
                    """,
                    (order["order_no"],),
                ).fetchone()
                if current is not None and current["source_system"] != "BHQ":
                    raise BhqImportError(
                        f"跨系統訂單撞號：{order['order_no']}，既有來源={current['source_system']}"
                    )
                if current is not None and current["current_content_hash"] == digest:
                    stats.skipped_orders += 1
                    if fault_injector:
                        fault_injector("after_order", order_index)
                    continue

                common_order = {
                    "order_no": order["order_no"],
                    "order_date": order["order_date"],
                    "category": order["category"],
                    "department_code": "BHQ",
                    "is_voided": False,
                    "details": [
                        {
                            "gift_code": item["product_code"], "list_price": 0,
                            "quantity": item["quantity"], "total_amount": 0,
                            "discount_amount": 0,
                        }
                        for item in order["items"]
                    ],
                }
                order_id, inserted = ensure_order(
                    connection, common_order, started, started, started
                )
                if inserted:
                    stats.inserted_orders += 1
                version_id = _insert_version(
                    connection, order_id, order, digest, int(stats.import_run_id),
                    parsed, started, stats,
                )
                connection.execute(
                    """
                    UPDATE orders SET current_version_id=?,current_content_hash=?,
                      order_date=?,category=?,department_code='BHQ',store_id=?,
                      is_voided=0,status='confirmed',last_seen_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        version_id, digest, order["order_date"], order["category"],
                        stores[order["source_store_name"]], started, started, order_id,
                    ),
                )
                if fault_injector:
                    fault_injector("after_order", order_index)
            connection.execute("RELEASE SAVEPOINT bhq_file_batch")
        except Exception as exc:
            connection.execute("ROLLBACK TO SAVEPOINT bhq_file_batch")
            connection.execute("RELEASE SAVEPOINT bhq_file_batch")
            finished = utc_now()
            connection.execute(
                """
                UPDATE bhq_import_runs SET finished_at=?,result='failed',
                  anomaly_count=1,error_summary=?,summary_json=?,updated_at=? WHERE id=?
                """,
                (
                    finished, str(exc), json_text({"error": str(exc)}), finished,
                    stats.import_run_id,
                ),
            )
            connection.commit()
            raise

        finished = utc_now()
        summary = stats.as_dict()
        connection.execute(
            """
            UPDATE bhq_import_runs SET finished_at=?,inserted_order_count=?,
              inserted_version_count=?,inserted_item_count=?,skipped_order_count=?,
              anomaly_count=0,result='completed',summary_json=?,updated_at=? WHERE id=?
            """,
            (
                finished, stats.inserted_orders, stats.inserted_versions,
                stats.inserted_items, stats.skipped_orders, json_text(summary),
                finished, stats.import_run_id,
            ),
        )
        connection.execute(
            "INSERT INTO audit_logs(event_type,entity_type,entity_id,actor,details_json,created_at) VALUES('bhq_excel_import','bhq_import_run',?,'bhq_import',?,?)",
            (str(stats.import_run_id), json_text(summary), finished),
        )
        connection.commit()
        return stats
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
