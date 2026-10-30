from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.bhq_excel import BHQ_STORE_CODES, parse_bhq_xls
from v6_db_web.bhq_import_service import BhqImportError, import_bhq


DATABASE = ROOT / "data" / "orders_bhq_test.db"
BASELINE = ROOT / "backups" / "formal" / "orders_20260825_161236_465268.db"
SOURCE = Path(r"C:\Users\USER\Downloads\202607ALL 訂單明細表.xls")
CORE = ("orders", "order_versions", "order_items", "stores", "products")


def connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in CORE}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def memory_clone(source: sqlite3.Connection) -> sqlite3.Connection:
    target = sqlite3.connect(":memory:")
    source.backup(target)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    return target


def main() -> int:
    parsed = parse_bhq_xls(SOURCE)
    before = connect(BASELINE, read_only=True)
    after = connect(DATABASE, read_only=True)
    try:
        before_counts = table_counts(before)
        after_counts = table_counts(after)
        before_version_columns = [row[1] for row in before.execute("PRAGMA table_info(order_versions)")]
        after_version_columns = [row[1] for row in after.execute("PRAGMA table_info(order_versions)")]
        runs = [dict(row) for row in after.execute("SELECT * FROM bhq_import_runs ORDER BY id")]
        bhq_orders = after.execute(
            "SELECT COUNT(*) FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system='BHQ'"
        ).fetchone()[0]
        bhq_items = after.execute(
            "SELECT COUNT(*) FROM order_items i JOIN order_versions v ON v.id=i.order_version_id WHERE v.source_system='BHQ'"
        ).fetchone()[0]
        zero_items = after.execute(
            "SELECT COUNT(*) FROM order_items i JOIN order_versions v ON v.id=i.order_version_id WHERE v.source_system='BHQ' AND i.quantity=0"
        ).fetchone()[0]
        uncategorized = [row[0] for row in after.execute(
            "SELECT o.order_no FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system='BHQ' AND o.category='..' ORDER BY o.order_no"
        )]
        stores = [dict(row) for row in after.execute(
            """
            SELECT a.source_store_name,s.store_code,s.store_name
            FROM bhq_store_aliases a JOIN stores s ON s.id=a.store_id
            ORDER BY s.store_code
            """
        )]
        product_audits = [dict(row) for row in after.execute(
            "SELECT event_type,entity_id,details_json FROM audit_logs WHERE event_type IN ('bhq_product_created','bhq_product_name_filled') ORDER BY id"
        )]
        metadata_names = after.execute(
            """
            SELECT COUNT(*) FROM web_order_item_metadata m
            JOIN order_items i ON i.id=m.order_item_id
            JOIN order_versions v ON v.id=i.order_version_id
            WHERE v.source_system='BHQ'
            """
        ).fetchone()[0]
        amount_mismatches = 0
        order_amounts = {order["order_no"]: order["discounted_amount"] for order in parsed["orders"]}
        for row in after.execute(
            """
            SELECT o.order_no,v.discounted_total_amount,m.original_total_amount
            FROM orders o JOIN order_versions v ON v.id=o.current_version_id
            JOIN bhq_order_version_metadata m ON m.order_version_id=v.id
            WHERE v.source_system='BHQ'
            """
        ):
            expected = order_amounts[row["order_no"]]
            amount_mismatches += int(row["discounted_total_amount"] != expected or row["original_total_amount"] != expected)

        memory = memory_clone(after)
        try:
            modified = copy.deepcopy(parsed)
            modified["orders"][0]["items"][0]["product_name"] += "（驗證版本）"
            core_before = table_counts(memory)
            modified_result = import_bhq(memory, modified).as_dict()
            core_modified = table_counts(memory)

            collision = copy.deepcopy(parsed)
            collision["orders"] = [collision["orders"][0]]
            collision_no = memory.execute(
                "SELECT o.order_no FROM orders o JOIN order_versions v ON v.id=o.current_version_id WHERE v.source_system<>'BHQ' LIMIT 1"
            ).fetchone()[0]
            collision["orders"][0]["order_no"] = collision_no
            collision_before = table_counts(memory)
            try:
                import_bhq(memory, collision)
                collision_result = {"protected": False, "error": None}
            except BhqImportError as exc:
                collision_result = {
                    "protected": True,
                    "error": str(exc),
                    "core_counts_unchanged": table_counts(memory) == collision_before,
                }

            rollback = copy.deepcopy(parsed)
            rollback["orders"][0]["items"][0]["product_name"] += "（驗證rollback）"
            rollback_before = table_counts(memory)
            try:
                import_bhq(
                    memory,
                    rollback,
                    fault_injector=lambda stage, index: (_ for _ in ()).throw(RuntimeError("injected failure"))
                    if stage == "after_order" and index == 1 else None,
                )
                rollback_result = {"rolled_back": False}
            except RuntimeError as exc:
                rollback_result = {
                    "rolled_back": True,
                    "error": str(exc),
                    "core_counts_unchanged": table_counts(memory) == rollback_before,
                }
        finally:
            memory.close()

        report = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "database": str(DATABASE),
            "database_sha256": sha256(DATABASE),
            "recovery_source": str(BASELINE),
            "recovery_source_sha256": sha256(BASELINE),
            "migration": {
                "before_latest_version": before.execute("SELECT MAX(version) FROM database_migrations").fetchone()[0],
                "after_latest_version": after.execute("SELECT MAX(version) FROM database_migrations").fetchone()[0],
                "order_versions_columns_before": before_version_columns,
                "order_versions_columns_after": after_version_columns,
                "new_tables": ["bhq_import_runs", "bhq_store_aliases", "bhq_order_version_metadata"],
            },
            "counts_before": before_counts,
            "counts_after": after_counts,
            "count_increase": {name: after_counts[name] - before_counts[name] for name in CORE},
            "source": {
                "rows": parsed["source_row_count"], "orders": parsed["order_count"],
                "valid_items": parsed["valid_item_count"],
                "empty_product_code_rows_excluded": parsed["empty_product_code_count"],
                "stores": parsed["store_count"],
                "duplicate_product_code_rows_preserved": parsed["duplicate_product_code_row_count"],
            },
            "actual": {
                "bhq_current_orders": bhq_orders, "bhq_current_items": bhq_items,
                "zero_quantity_items_preserved": zero_items,
                "uncategorized_orders_preserved": uncategorized,
                "item_level_product_names": metadata_names,
                "discounted_amount_mismatches": amount_mismatches,
                "stores": stores,
            },
            "product_handling": {
                "distinct_source_codes": len({i["product_code"] for o in parsed["orders"] for i in o["items"]}),
                "audit_events": product_audits,
            },
            "import_runs": runs,
            "modified_version_test": {
                "result": modified_result,
                "version_increase": core_modified["order_versions"] - core_before["order_versions"],
                "order_increase": core_modified["orders"] - core_before["orders"],
            },
            "cross_system_collision_test": collision_result,
            "transaction_rollback_test": rollback_result,
            "integrity_check": after.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_errors": len(after.execute("PRAGMA foreign_key_check").fetchall()),
            "recovery": {
                "method": "關閉所有程式後，將 recovery_source 複製覆蓋 data/orders_bhq_test.db；不得覆蓋正式 orders.db。",
            },
        }
    finally:
        before.close()
        after.close()

    output = ROOT / "reports" / "BHQ_TEST_VALIDATION_202607ALL.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
