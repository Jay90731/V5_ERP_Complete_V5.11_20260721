from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


ERP_SCOPE = """
v.source_system<>'BHQ'
AND o.department_code='4001'
AND s.store_code='SR'
AND o.is_voided=0
AND o.status='confirmed'
AND EXISTS (
  SELECT 1 FROM order_items required_item
  WHERE required_item.order_version_id=o.current_version_id
)
"""

BHQ_SCOPE = """
v.source_system='BHQ'
AND o.is_voided=0
AND o.status='confirmed'
AND EXISTS (
  SELECT 1 FROM order_items required_item
  WHERE required_item.order_version_id=o.current_version_id
)
"""

UNIFIED_SCOPE = f"(({ERP_SCOPE}) OR ({BHQ_SCOPE}))"

UNIFIED_AMOUNT = """
CASE WHEN v.source_system='BHQ'
     THEN v.discounted_total_amount
     ELSE v.total_discount_amount
END
"""


def scope_for_source(source_system: str) -> str:
    source = str(source_system or "ALL").strip().upper()
    if source == "ERP":
        return ERP_SCOPE
    if source == "BHQ":
        return BHQ_SCOPE
    if source in {"", "ALL"}:
        return UNIFIED_SCOPE
    raise ValueError(f"不支援的來源系統：{source_system}")


def source_label_sql() -> str:
    return "CASE WHEN v.source_system='BHQ' THEN 'BHQ' ELSE 'ERP' END"


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Repository:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database.as_uri() + "?mode=ro", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def health(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            source_counts = {
                row["source_system"]: int(row["order_count"])
                for row in connection.execute(
                    f"""
                    SELECT {source_label_sql()} source_system,COUNT(*) order_count
                    FROM orders o JOIN stores s ON s.id=o.store_id
                    JOIN order_versions v ON v.id=o.current_version_id
                    WHERE {UNIFIED_SCOPE}
                    GROUP BY {source_label_sql()}
                    """
                )
            }
            unified = connection.execute(
                """
                SELECT COUNT(*)
                FROM orders o JOIN stores s ON s.id=o.store_id
                JOIN order_versions v ON v.id=o.current_version_id
                WHERE """
                + UNIFIED_SCOPE
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
            ).fetchone()[0]
            last_updated = connection.execute(
                """
                SELECT MAX(value) FROM (
                  SELECT MAX(updated_at) value FROM orders
                  UNION ALL SELECT MAX(updated_at) FROM scan_jobs
                  UNION ALL SELECT MAX(updated_at) FROM review_queue
                  UNION ALL SELECT MAX(created_at) FROM web_manual_review_events
                )
                """
            ).fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        return {
            "status": "ok",
            "quick_check": quick,
            "formal_orders": unified,
            "unified_orders": unified,
            "erp_orders": source_counts.get("ERP", 0),
            "bhq_orders": source_counts.get("BHQ", 0),
            "pending": pending,
            "manual_write_enabled": True,
            "database_name": self.database.name,
            "last_updated_at": last_updated,
            "journal_mode": journal_mode,
        }

    def filters(self, source_system: str = "ALL") -> dict[str, Any]:
        scope = scope_for_source(source_system)
        with closing(self.connect()) as connection:
            months = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT substr(o.order_date,1,7) month
                    FROM orders o JOIN stores s ON s.id=o.store_id
                    JOIN order_versions v ON v.id=o.current_version_id
                    WHERE """
                    + scope
                    + " ORDER BY month DESC"
                )
            ]
            categories = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT o.category
                    FROM orders o JOIN stores s ON s.id=o.store_id
                    JOIN order_versions v ON v.id=o.current_version_id
                    WHERE """
                    + scope
                    + " GROUP BY o.category ORDER BY COUNT(*) DESC, o.category"
                )
            ]
            stores = [
                row_dict(row)
                for row in connection.execute(
                    """
                    SELECT s.store_code,s.store_name,COUNT(*) order_count
                    FROM orders o JOIN stores s ON s.id=o.store_id
                    JOIN order_versions v ON v.id=o.current_version_id
                    WHERE """
                    + scope
                    + " GROUP BY s.id,s.store_code,s.store_name ORDER BY s.store_code"
                )
            ]
        return {
            "source_system": str(source_system or "ALL").upper(),
            "months": months,
            "categories": categories,
            "stores": stores,
        }

    def orders(
        self,
        *,
        keyword: str = "",
        source_system: str = "ALL",
        store_code: str = "",
        date_from: str = "",
        date_to: str = "",
        month: str = "",
        category: str = "",
        order_no: str = "",
        product_code: str = "",
        product_name: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(10, min(100, page_size))
        where = [scope_for_source(source_system)]
        params: list[Any] = []
        if keyword:
            pattern = f"%{escape_like(keyword.strip())}%"
            where.append(
                """
                (
                  o.order_no LIKE ? ESCAPE '\\'
                  OR i.gift_code LIKE ? ESCAPE '\\'
                  OR COALESCE(NULLIF(imeta.product_name,''),p.product_name,'')
                       LIKE ? ESCAPE '\\'
                )
                """
            )
            params.extend([pattern, pattern, pattern])
        if store_code:
            where.append("s.store_code = ?")
            params.append(store_code)
        if date_from:
            where.append("o.order_date >= ?")
            params.append(date_from)
        if date_to:
            where.append("o.order_date <= ?")
            params.append(date_to)
        if month:
            where.append("substr(o.order_date,1,7) = ?")
            params.append(month)
        if category:
            where.append("o.category = ?")
            params.append(category)
        if order_no:
            where.append("o.order_no LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(order_no.strip())}%")
        if product_code:
            where.append("i.gift_code LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(product_code.strip())}%")
        if product_name:
            where.append(
                "COALESCE(NULLIF(imeta.product_name,''),p.product_name,'') LIKE ? ESCAPE '\\'"
            )
            params.append(f"%{escape_like(product_name.strip())}%")
        where_sql = " AND ".join(f"({part})" for part in where)
        from_sql = """
          FROM orders o
          JOIN stores s ON s.id=o.store_id
          JOIN order_versions v ON v.id=o.current_version_id
          JOIN order_items i ON i.order_version_id=v.id
          LEFT JOIN products p ON p.id=i.product_id
          LEFT JOIN web_order_item_metadata imeta ON imeta.order_item_id=i.id
        """
        with closing(self.connect()) as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) " + from_sql + " WHERE " + where_sql,
                    params,
                ).fetchone()[0]
            )
            order_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT o.id) " + from_sql + " WHERE " + where_sql,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT i.id item_id,s.store_code,s.store_name,o.category,o.order_no,
                       o.order_date,i.line_no,i.gift_code product_code,
                       COALESCE(NULLIF(imeta.product_name,''),NULLIF(p.product_name,''),'未對應') product_name,
                       i.quantity,
                       CASE WHEN v.source_system='BHQ'
                            THEN v.discounted_total_amount
                            ELSE i.discount_amount END discounted_amount,
                       {UNIFIED_AMOUNT} order_discounted_amount,
                       {source_label_sql()} source_system
                """
                + from_sql
                + " WHERE "
                + where_sql
                + " ORDER BY o.order_date DESC,o.order_no DESC,i.line_no,i.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [row_dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
            "order_count": order_count,
            "source_system": str(source_system or "ALL").upper(),
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def order_detail(self, order_no: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            order = connection.execute(
                f"""
                SELECT o.id, o.order_no, o.order_date, o.category,
                       o.department_code, o.status, o.is_voided,
                       s.store_code, s.store_name,
                       v.version_no, v.detail_count, v.total_quantity,
                       {UNIFIED_AMOUNT} total_discount_amount,
                       {source_label_sql()} source_system,v.effective_at
                FROM orders o
                JOIN stores s ON s.id=o.store_id
                JOIN order_versions v ON v.id=o.current_version_id
                WHERE o.order_no=? AND """
                + UNIFIED_SCOPE,
                (order_no,),
            ).fetchone()
            if order is None:
                return None
            items = connection.execute(
                f"""
                SELECT i.line_no, i.gift_code,
                       COALESCE(NULLIF(imeta.product_name,''),
                                NULLIF(p.product_name,''), '未對應') product_name,
                       i.quantity,
                       CASE WHEN v.source_system='BHQ'
                            THEN v.discounted_total_amount
                            ELSE i.discount_amount END discount_amount
                FROM orders o
                JOIN order_versions v ON v.id=o.current_version_id
                JOIN order_items i ON i.order_version_id=o.current_version_id
                LEFT JOIN products p ON p.id=i.product_id
                LEFT JOIN web_order_item_metadata imeta ON imeta.order_item_id=i.id
                JOIN stores s ON s.id=o.store_id
                WHERE o.order_no=? AND {UNIFIED_SCOPE}
                ORDER BY i.line_no, i.id
                """,
                (order_no,),
            ).fetchall()
        result = row_dict(order)
        result["items"] = [row_dict(row) for row in items]
        return result

    def reviews(
        self,
        *,
        keyword: str = "",
        status: str = "pending",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(10, min(100, page_size))
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("rq.status=?")
            params.append(status)
        if keyword:
            pattern = f"%{escape_like(keyword.strip())}%"
            where.append(
                """
                (COALESCE(rq.order_no,'') LIKE ? ESCAPE '\\'
                 OR rq.source_key LIKE ? ESCAPE '\\'
                 OR rq.reason LIKE ? ESCAPE '\\')
                """
            )
            params.extend([pattern, pattern, pattern])
        where_sql = " AND ".join(where) if where else "1=1"
        from_sql = """
          FROM review_queue rq
          LEFT JOIN scan_items si ON si.id=rq.scan_item_id
          LEFT JOIN web_pending_images wpi ON wpi.review_queue_id=rq.id
        """
        with closing(self.connect()) as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) " + from_sql + " WHERE " + where_sql,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT rq.id, rq.order_no, rq.source_key, rq.status,
                       rq.reason, rq.processing_note,
                       rq.first_seen_at, rq.last_seen_at,
                       si.image_name, si.image_relative_path,
                       si.image_status,
                       CASE WHEN wpi.review_queue_id IS NULL
                                  OR wpi.image_status<>'available' THEN 0 ELSE 1 END has_image
                """
                + from_sql
                + " WHERE "
                + where_sql
                + " ORDER BY rq.updated_at DESC, rq.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [row_dict(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def review_image(self, review_id: int) -> Path | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT wpi.relative_path
                FROM web_pending_images wpi
                JOIN review_queue rq ON rq.id=wpi.review_queue_id
                WHERE wpi.review_queue_id=? AND wpi.image_status='available'
                """,
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        root = self.database.parent.resolve()
        candidate = (root / str(row[0])).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def review_detail(self, review_id: int) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT rq.id, rq.order_no, rq.source_key, rq.status, rq.reason,
                       rq.processing_note, rq.raw_payload_json, rq.first_seen_at,
                       rq.last_seen_at, rq.resolved_at,
                       CASE WHEN wpi.review_queue_id IS NULL
                                  OR wpi.image_status<>'available' THEN 0 ELSE 1 END has_image,
                       wpi.retained_until
                FROM review_queue rq
                LEFT JOIN web_pending_images wpi ON wpi.review_queue_id=rq.id
                WHERE rq.id=?
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                raw = json.loads(str(row["raw_payload_json"] or "{}"))
            except json.JSONDecodeError:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw_items = raw.get("details")
            if not isinstance(raw_items, list):
                raw_items = []
            gift_codes = [
                str(item.get("giftCode", item.get("gift_code", ""))).strip()
                for item in raw_items
                if isinstance(item, dict)
            ]
            product_names: dict[str, str] = {}
            if gift_codes:
                placeholders = ",".join("?" for _ in gift_codes)
                product_names = {
                    str(product["gift_code"]): str(product["product_name"] or "")
                    for product in connection.execute(
                        f"SELECT gift_code,product_name FROM products WHERE gift_code IN ({placeholders})",
                        gift_codes,
                    )
                }
            order_no = str(raw.get("orderNo") or row["order_no"] or "").strip().upper()
            existing = connection.execute(
                "SELECT order_date FROM orders WHERE order_no=?", (order_no,)
            ).fetchone()
            events = connection.execute(
                """
                SELECT action, write_result, note, actor, source, created_at
                FROM web_manual_review_events WHERE review_queue_id=?
                ORDER BY id DESC LIMIT 20
                """,
                (review_id,),
            ).fetchall()
        order_date = str(raw.get("orderDate") or raw.get("order_date") or "")
        order_date = re.sub(r"[./]", "-", order_date)
        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("giftCode", item.get("gift_code", ""))).strip()
            items.append(
                {
                    "gift_code": code,
                    "product_name": str(
                        item.get("productName", item.get("product_name", ""))
                        or product_names.get(code, "")
                    ).strip(),
                    "list_price": item.get("listPrice", item.get("list_price", 0)),
                    "quantity": item.get("quantity", 0),
                    "total_amount": item.get("totalAmount", item.get("total_amount", 0)),
                    "discount_amount": item.get(
                        "discountAmount", item.get("discount_amount", 0)
                    ),
                }
            )
        result = row_dict(row)
        result.pop("raw_payload_json", None)
        result["form"] = {
            "order_no": order_no,
            "order_date": order_date,
            "category": str(raw.get("category") or "").strip(),
            "department_code": str(
                raw.get("departmentCode", raw.get("department_code", "")) or ""
            ).strip(),
            "customer_name": str(
                raw.get("customerName", raw.get("customer_name", "")) or ""
            ).strip(),
            "store_code": "SR",
            "items": items,
        }
        result["existing_order"] = bool(existing)
        review_context = raw.get("reviewContext")
        if not isinstance(review_context, dict):
            review_context = {}
        result["date_review"] = {
            "selected_date": review_context.get("selected_date") or order_date or None,
            "suggested_date": review_context.get("suggested_date") or None,
            "candidates": review_context.get("candidates") or [],
            "dedicated_candidates": review_context.get("dedicated_candidates") or [],
            "main_region_candidates": review_context.get("main_region_candidates") or [],
            "original_database_date": (
                review_context.get("original_database_date")
                or (str(existing["order_date"]) if existing else None)
            ),
            "query_date_from": review_context.get("query_date_from"),
            "query_date_to": review_context.get("query_date_to"),
            "anomaly_reasons": review_context.get("anomaly_reasons") or [],
            "confidence": review_context.get("confidence") or "",
        }
        result["events"] = [row_dict(event) for event in events]
        return result

    def statistics(
        self,
        month: str = "",
        *,
        source_system: str = "ALL",
        store_code: str = "",
        date_from: str = "",
        date_to: str = "",
        category: str = "",
        order_no: str = "",
        product_code: str = "",
        product_name: str = "",
    ) -> dict[str, Any]:
        where = [scope_for_source(source_system)]
        params: list[Any] = []
        if store_code:
            where.append("s.store_code=?")
            params.append(store_code)
        if date_from:
            where.append("o.order_date>=?")
            params.append(date_from)
        if date_to:
            where.append("o.order_date<=?")
            params.append(date_to)
        if month:
            where.append("substr(o.order_date,1,7)=?")
            params.append(month)
        if category:
            where.append("o.category=?")
            params.append(category)
        if order_no:
            where.append("o.order_no LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(order_no.strip())}%")
        item_conditions: list[str] = []
        if product_code:
            item_conditions.append("filter_item.gift_code LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(product_code.strip())}%")
        if product_name:
            item_conditions.append(
                "COALESCE(NULLIF(filter_meta.product_name,''),filter_product.product_name,'') LIKE ? ESCAPE '\\'"
            )
            params.append(f"%{escape_like(product_name.strip())}%")
        if item_conditions:
            where.append(
                """
                EXISTS(
                  SELECT 1 FROM order_items filter_item
                  LEFT JOIN products filter_product ON filter_product.id=filter_item.product_id
                  LEFT JOIN web_order_item_metadata filter_meta ON filter_meta.order_item_id=filter_item.id
                  WHERE filter_item.order_version_id=v.id AND """
                + " AND ".join(item_conditions)
                + ")"
            )
        where_sql = " AND ".join(f"({part})" for part in where)
        cte = f"""
            WITH unified_orders AS (
              SELECT o.id,o.order_no,o.order_date,substr(o.order_date,1,7) month,
                     o.category,s.store_code,s.store_name,
                     {source_label_sql()} source_system,
                     (SELECT COALESCE(SUM(quantity),0) FROM order_items quantity_item
                      WHERE quantity_item.order_version_id=v.id) total_quantity,
                     {UNIFIED_AMOUNT} discounted_amount
              FROM orders o
              JOIN stores s ON s.id=o.store_id
              JOIN order_versions v ON v.id=o.current_version_id
              WHERE {where_sql}
            )
        """
        with closing(self.connect()) as connection:
            overall = connection.execute(
                cte
                + """
                SELECT COUNT(*) order_count,
                       COALESCE(SUM(total_quantity),0) box_count,
                       COALESCE(SUM(discounted_amount),0) amount,
                       COUNT(DISTINCT month) month_count
                FROM unified_orders
                """,
                params,
            ).fetchone()
            sources = connection.execute(
                cte
                + """
                SELECT source_system,COUNT(*) order_count,
                       COALESCE(SUM(total_quantity),0) box_count,
                       COALESCE(SUM(discounted_amount),0) amount
                FROM unified_orders GROUP BY source_system ORDER BY source_system DESC
                """,
                params,
            ).fetchall()
            stores = connection.execute(
                cte
                + """
                SELECT store_code,store_name,source_system,COUNT(*) order_count,
                       COALESCE(SUM(total_quantity),0) box_count,
                       COALESCE(SUM(discounted_amount),0) amount
                FROM unified_orders GROUP BY store_code,store_name,source_system
                ORDER BY source_system DESC,store_code
                """,
                params,
            ).fetchall()
            categories = connection.execute(
                cte
                + """
                SELECT category, COUNT(*) order_count,
                       COALESCE(SUM(total_quantity),0) box_count,
                       COALESCE(SUM(discounted_amount),0) amount,
                       ROUND(AVG(discounted_amount)) average_order_amount,
                       CASE WHEN SUM(total_quantity)=0 THEN 0
                            ELSE ROUND(1.0*SUM(discounted_amount)/SUM(total_quantity)) END average_box_amount,
                       ROUND(AVG(total_quantity)) average_order_boxes
                FROM unified_orders GROUP BY category
                ORDER BY order_count DESC, category
                """,
                params,
            ).fetchall()
            monthly = connection.execute(
                cte
                + """
                SELECT month, COUNT(*) order_count,
                       COALESCE(SUM(total_quantity),0) box_count,
                       COALESCE(SUM(discounted_amount),0) amount
                FROM unified_orders GROUP BY month ORDER BY month DESC
                """,
                params,
            ).fetchall()
            gaps = connection.execute(
                """
                SELECT month, expected_order_no, first_observed_order_no, status
                FROM history_month_gaps WHERE status='open' ORDER BY month
                """
            ).fetchall()
        return {
            "overall": row_dict(overall),
            "sources": [row_dict(row) for row in sources],
            "stores": [row_dict(row) for row in stores],
            "categories": [row_dict(row) for row in categories],
            "monthly": [row_dict(row) for row in monthly],
            "gaps": [row_dict(row) for row in gaps],
        }
