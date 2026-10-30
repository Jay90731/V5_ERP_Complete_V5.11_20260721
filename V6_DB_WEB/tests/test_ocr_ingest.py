from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.db import connect
from v6_db_web.migrations import apply_migrations
from v6_db_web.manual_review import apply_review_actions
from v6_db_web.ocr_ingest import replay_ocr_json
from v6_db_web.phase2_validation import validate_phase2_replay


FIXTURE = ROOT / "tests" / "fixtures" / "daily_7_days_ocr.json"


def valid_record(order_no="GOU26080010", image_name=None):
    return {
        "imageName": image_name or f"{order_no}.png",
        "departmentCode": "4001",
        "orderNo": order_no,
        "category": "婚約",
        "orderDate": "2026.08.02",
        "details": [
            {
                "giftCode": "720001",
                "listPrice": 800,
                "quantity": 2,
                "totalAmount": 1600,
                "discountAmount": 1400,
            }
        ],
        "summaryTotals": {
            "quantity": 2,
            "totalAmount": 1600,
            "discountAmount": 1400,
        },
        "detailCodeColumnVisible": True,
        "voided": False,
        "statusLevel": "success",
        "errorReason": "",
    }


class OcrIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "orders_test.db"
        self.connection = connect(self.database)
        apply_migrations(self.connection, ROOT / "migrations")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def write_payload(self, records, name="orders.json", create_images=False):
        ocr = self.root / "run_fixture" / "ocr"
        data = ocr / "data"
        images = ocr / "input_images"
        data.mkdir(parents=True, exist_ok=True)
        images.mkdir(parents=True, exist_ok=True)
        path = data / name
        path.write_text(
            json.dumps({"version": 2, "orders": records}, ensure_ascii=False),
            encoding="utf-8",
        )
        if create_images:
            for record in records:
                image_name = record.get("imageName")
                if image_name:
                    (images / image_name).write_bytes(
                        f"image:{image_name}".encode("utf-8")
                    )
        return path

    def replay(self, records, **kwargs):
        path = self.write_payload(records, **kwargs)
        return replay_ocr_json(
            self.connection, path, "2026-07-27", "2026-08-02"
        )

    def count(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def version_count(self, order_no):
        return self.connection.execute(
            """
            SELECT COUNT(*)
            FROM order_versions v
            JOIN orders o ON o.id = v.order_id
            WHERE o.order_no = ?
            """,
            (order_no,),
        ).fetchone()[0]

    def test_fixture_creates_expected_job_items_and_results(self):
        summary = replay_ocr_json(
            self.connection, FIXTURE, "2026-07-27", "2026-08-02"
        )
        self.assertEqual(summary.total_count, 4)
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(summary.excluded_count, 2)
        results = {
            row["result_code"]
            for row in self.connection.execute(
                "SELECT result_code FROM scan_items WHERE scan_job_id = ?",
                (summary.scan_job_id,),
            )
        }
        self.assertEqual(
            results, {"new", "pending", "non_target_department", "voided"}
        )

    def test_new_order_is_inserted(self):
        summary = self.replay([valid_record()])
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(self.count("orders"), 1)
        self.assertEqual(self.count("order_versions"), 1)

    def test_same_order_rerun_does_not_add_version(self):
        record = valid_record()
        self.replay([record])
        summary = self.replay([record], name="same.json")
        self.assertEqual(summary.unchanged_count, 1)
        self.assertEqual(self.version_count(record["orderNo"]), 1)

    def test_quantity_change_creates_new_version(self):
        record = valid_record()
        self.replay([record])
        changed = copy.deepcopy(record)
        changed["details"][0]["quantity"] = 3
        changed["details"][0]["totalAmount"] = 2400
        changed["details"][0]["discountAmount"] = 2100
        changed["summaryTotals"] = {
            "quantity": 3,
            "totalAmount": 2400,
            "discountAmount": 2100,
        }
        summary = self.replay([changed], name="quantity_changed.json")
        self.assertEqual(summary.changed_count, 1)
        self.assertEqual(self.version_count(record["orderNo"]), 2)

    def test_discount_change_creates_new_version(self):
        record = valid_record()
        self.replay([record])
        changed = copy.deepcopy(record)
        changed["details"][0]["discountAmount"] = 1200
        changed["summaryTotals"]["discountAmount"] = 1200
        summary = self.replay([changed], name="discount_changed.json")
        self.assertEqual(summary.changed_count, 1)
        self.assertEqual(self.version_count(record["orderNo"]), 2)

    def test_multi_item_order_is_complete(self):
        record = valid_record()
        record["details"].append(
            {
                "giftCode": "720002",
                "listPrice": 500,
                "quantity": 4,
                "totalAmount": 2000,
                "discountAmount": 1800,
            }
        )
        record["summaryTotals"] = {
            "quantity": 6,
            "totalAmount": 3600,
            "discountAmount": 3200,
        }
        self.replay([record])
        self.assertEqual(self.count("order_items"), 2)

    def test_explicit_pending_enters_review_queue(self):
        record = valid_record("GOU26080011")
        record["statusLevel"] = "pending"
        record["errorReason"] = "圖片不完整"
        summary = self.replay([record])
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(self.count("review_queue"), 1)
        self.assertEqual(self.count("orders"), 0)

    def test_4001_without_details_enters_review_queue(self):
        record = valid_record("GOU26080012")
        record["details"] = []
        record["summaryTotals"] = None
        summary = self.replay([record])
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(self.count("review_queue"), 1)
        self.assertEqual(self.count("orders"), 0)

    def test_non_target_is_saved_but_not_official(self):
        record = valid_record("GOU26080013")
        record["departmentCode"] = "4100"
        record["details"] = []
        record["summaryTotals"] = None
        summary = self.replay([record])
        self.assertEqual(summary.excluded_count, 1)
        self.assertEqual(self.count("orders"), 1)
        self.assertEqual(self.count("official_orders"), 0)

    def test_voided_is_saved_but_not_official(self):
        record = valid_record("GOU26080014")
        record["voided"] = True
        record["details"] = []
        record["summaryTotals"] = None
        summary = self.replay([record])
        self.assertEqual(summary.excluded_count, 1)
        self.assertEqual(self.count("orders"), 1)
        self.assertEqual(self.count("official_orders"), 0)

    def test_official_order_uses_sr_store_and_department_is_preserved(self):
        self.replay([valid_record()])
        value = self.connection.execute(
            """
            SELECT s.store_code,s.store_name,s.status,o.department_code
            FROM orders o JOIN stores s ON s.id=o.store_id
            """
        ).fetchone()
        self.assertEqual(tuple(value), ("SR", "SR", "active", "4001"))
        self.assertEqual(self.count("stores"), 1)

    def test_confirmed_pending_order_uses_sr_and_resolves_review(self):
        record = valid_record("GOU26080031")
        record["statusLevel"] = "pending"
        record["errorReason"] = "待人工確認"
        self.replay([record])
        confirmed = copy.deepcopy(record)
        confirmed["statusLevel"] = "success"
        confirmed["errorReason"] = ""
        self.replay([confirmed], name="confirmed_pending.json")
        row = self.connection.execute(
            """
            SELECT r.status,s.store_code,o.department_code
            FROM review_queue r
            JOIN orders o ON o.order_no=r.order_no
            JOIN stores s ON s.id=o.store_id
            WHERE r.order_no='GOU26080031'
            """
        ).fetchone()
        self.assertEqual(tuple(row), ("resolved", "SR", "4001"))

    def test_product_name_update_does_not_create_order_version(self):
        record = valid_record()
        self.replay([record])
        self.connection.execute(
            "UPDATE products SET product_name = '測試中文品名'"
        )
        self.connection.commit()
        self.replay([record], name="product_name.json")
        self.assertEqual(self.version_count(record["orderNo"]), 1)

    def test_same_pending_rerun_does_not_duplicate_review(self):
        record = valid_record("GOU26080015")
        record["statusLevel"] = "pending"
        record["errorReason"] = "待人工確認"
        self.replay([record])
        self.replay([record], name="pending_again.json")
        self.assertEqual(self.count("review_queue"), 1)

    def test_pending_deduplicates_by_order_number_across_sources(self):
        record = valid_record("GOU26080025")
        record["statusLevel"] = "pending"
        record["errorReason"] = "待人工確認"
        self.connection.execute(
            """
            INSERT INTO review_queue(
                source_key, order_no, status, reason, raw_payload_json,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (
                'v5:GOU26080025', 'GOU26080025', 'pending',
                '既有待確認', '{}', '2026-07-01T00:00:00+00:00',
                '2026-07-01T00:00:00+00:00',
                '2026-07-01T00:00:00+00:00',
                '2026-07-01T00:00:00+00:00'
            )
            """
        )
        self.connection.commit()
        self.replay([record])
        self.assertEqual(self.count("review_queue"), 1)

    def test_single_failure_does_not_block_next_item(self):
        failed = valid_record("GOU26080016")
        good = valid_record("GOU26080017")

        def fault(stage, record):
            if stage == "after_order_write" and record.get("orderNo") == failed["orderNo"]:
                raise RuntimeError("simulated item failure")

        path = self.write_payload([failed, good])
        summary = replay_ocr_json(
            self.connection,
            path,
            "2026-07-27",
            "2026-08-02",
            fault_hook=fault,
        )
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.success_count, 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT id FROM orders WHERE order_no = ?", (failed["orderNo"],)
            ).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT id FROM orders WHERE order_no = ?", (good["orderNo"],)
            ).fetchone()
        )

    def test_transaction_failure_leaves_no_partial_order(self):
        record = valid_record("GOU26080018")

        def fault(stage, payload):
            raise RuntimeError("rollback check")

        path = self.write_payload([record])
        summary = replay_ocr_json(
            self.connection,
            path,
            "2026-07-27",
            "2026-08-02",
            fault_hook=fault,
        )
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(self.count("order_versions"), 0)
        self.assertEqual(self.count("order_items"), 0)
        self.assertEqual(self.count("scan_items"), 1)

    def test_batch_interrupt_rolls_back_current_order(self):
        record = valid_record("GOU26080027")

        def interrupt(stage, payload):
            raise KeyboardInterrupt("simulated process interruption")

        path = self.write_payload([record])
        with self.assertRaises(KeyboardInterrupt):
            replay_ocr_json(
                self.connection,
                path,
                "2026-07-27",
                "2026-08-02",
                fault_hook=interrupt,
            )
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(self.count("order_versions"), 0)
        self.assertEqual(self.count("order_items"), 0)
        job = self.connection.execute(
            "SELECT status FROM scan_jobs"
        ).fetchone()
        self.assertEqual(job["status"], "failed")

    def test_database_reopens_with_complete_ocr_data(self):
        self.replay([valid_record()])
        self.connection.close()
        self.connection = connect(self.database)
        self.assertEqual(self.count("orders"), 1)
        self.assertEqual(self.count("order_items"), 1)
        self.assertEqual(
            self.connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
        )

    def test_image_hash_deduplicates_screenshot(self):
        record = valid_record("GOU26080019")
        self.replay([record], create_images=True)
        self.replay([record], name="image_again.json", create_images=True)
        self.assertEqual(self.count("screenshots"), 1)

    def test_missing_image_is_recorded_without_batch_failure(self):
        summary = self.replay([valid_record("GOU26080020")])
        self.assertEqual(summary.success_count, 1)
        item = self.connection.execute(
            "SELECT image_status FROM scan_items"
        ).fetchone()
        self.assertEqual(item["image_status"], "missing")

    def test_image_filename_corrects_misread_order_number(self):
        record = valid_record("GOU26080021")
        record["orderNo"] = "0OU26080021"
        summary = self.replay([record])
        self.assertEqual(summary.success_count, 1)
        saved = self.connection.execute(
            "SELECT order_no, validation_json FROM scan_items"
        ).fetchone()
        self.assertEqual(saved["order_no"], "GOU26080021")
        self.assertIn("圖片檔名校正", saved["validation_json"])

    def test_pending_uses_corrected_number_but_preserves_raw_payload(self):
        record = valid_record("GOU26080026")
        record["orderNo"] = "0OU26080026"
        record["statusLevel"] = "pending"
        record["errorReason"] = "圖片不完整"
        self.replay([record])
        item = self.connection.execute(
            "SELECT order_no, raw_ocr_payload_json FROM scan_items"
        ).fetchone()
        review = self.connection.execute(
            "SELECT order_no, raw_payload_json FROM review_queue"
        ).fetchone()
        self.assertEqual(item["order_no"], "GOU26080026")
        self.assertEqual(review["order_no"], "GOU26080026")
        self.assertIn('"orderNo":"0OU26080026"', item["raw_ocr_payload_json"])
        self.assertIn('"rawOrderNo":"0OU26080026"', review["raw_payload_json"])

    def test_out_of_range_date_is_not_inferred_from_order_number(self):
        record = valid_record("GOU26080022")
        record["orderDate"] = "2025.08.02"
        summary = self.replay([record])
        self.assertEqual(summary.pending_count, 1)
        saved = self.connection.execute(
            """
            SELECT validation_json
            FROM scan_items
            WHERE order_no = ?
            """,
            ("GOU26080022",),
        ).fetchone()
        self.assertIn("保留 ERP 畫面的訂貨日期", saved["validation_json"])
        self.assertEqual(self.count("orders"), 0)

    def test_manual_exclusion_stays_excluded_on_future_replay(self):
        record = valid_record("GOU26080028")
        self.replay([record])
        result = apply_review_actions(
            self.connection,
            {
                "actions": [
                    {
                        "action": "exclude",
                        "orderNo": record["orderNo"],
                        "note": "使用者確認當成沒有",
                    }
                ]
            },
        )
        rerun = self.replay([record], name="excluded_again.json")
        row = self.connection.execute(
            "SELECT status, store_id FROM orders WHERE order_no = ?",
            (record["orderNo"],),
        ).fetchone()
        self.assertEqual(result.excluded, 1)
        self.assertEqual(rerun.excluded_count, 1)
        self.assertEqual(tuple(row), ("archived", None))
        self.assertEqual(self.count("official_orders"), 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM review_queue WHERE order_no = ?",
                (record["orderNo"],),
            ).fetchone()["status"],
            "excluded",
        )

    def test_manual_correction_writes_complete_order_and_resolves_review(self):
        pending = valid_record("GOU26080029")
        pending["statusLevel"] = "pending"
        pending["errorReason"] = "待人工確認"
        self.replay([pending], create_images=True)
        corrected = copy.deepcopy(pending)
        corrected["statusLevel"] = "success"
        corrected["errorReason"] = ""
        result = apply_review_actions(
            self.connection,
            {
                "actions": [
                    {
                        "action": "correct",
                        "orderNo": pending["orderNo"],
                        "correctedOrder": corrected,
                        "note": "人工對照原圖完成",
                    }
                ]
            },
        )
        row = self.connection.execute(
            """
            SELECT r.status, o.department_code, s.store_code
            FROM review_queue r
            JOIN orders o ON o.order_no = r.order_no
            JOIN stores s ON s.id = o.store_id
            WHERE r.order_no = ?
            """,
            (pending["orderNo"],),
        ).fetchone()
        self.assertEqual(result.corrected, 1)
        self.assertEqual(tuple(row), ("resolved", "4001", "SR"))
        self.assertEqual(self.count("order_items"), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE event_type='manual_review_corrected'"
            ).fetchone()[0],
            1,
        )

    def test_invalid_manual_correction_keeps_pending_without_partial_order(self):
        pending = valid_record("GOU26080030")
        pending["statusLevel"] = "pending"
        pending["errorReason"] = "待人工確認"
        self.replay([pending])
        broken = copy.deepcopy(pending)
        broken["statusLevel"] = "success"
        broken["details"] = []
        with self.assertRaisesRegex(ValueError, "仍未通過驗證"):
            apply_review_actions(
                self.connection,
                {
                    "actions": [
                        {
                            "action": "correct",
                            "orderNo": pending["orderNo"],
                            "correctedOrder": broken,
                        }
                    ]
                },
            )
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM review_queue"
            ).fetchone()["status"],
            "pending",
        )

    def test_missing_summary_quantity_uses_complete_details(self):
        record = valid_record("GOU26080023")
        record["summaryTotals"]["quantity"] = None
        summary = self.replay([record])
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.pending_count, 0)

    def test_target_order_date_outside_query_range_is_pending(self):
        record = valid_record("GOU26080031")
        record["orderDate"] = "2026.03.21"
        summary = self.replay([record], create_images=True)
        self.assertEqual(summary.success_count, 0)
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(self.count("orders"), 0)
        reason = self.connection.execute(
            "SELECT reason FROM review_queue WHERE order_no=?",
            (record["orderNo"],),
        ).fetchone()["reason"]
        self.assertIn("超出本次查詢範圍", reason)

    def test_conflicting_date_evidence_does_not_create_changed_version(self):
        record = valid_record("GOU26080033")
        self.replay([record])
        changed = copy.deepcopy(record)
        changed["orderDate"] = "2026.08.22"
        changed["dateEvidence"] = {
            "selectedDate": "2026-08-22",
            "suggestedDate": "2026-08-22",
            "candidates": ["2026-08-22", "2026-08-02"],
            "dedicatedCandidates": ["2026-08-22", "2026-08-22"],
            "mainRegionCandidates": ["2026-08-02"],
            "confidence": "review",
            "anomalyReasons": ["日期專用裁切與主區域OCR結果不同"],
            "orderNumberYYMM": "2026-08",
        }
        summary = self.replay([changed], name="date_conflict.json")
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(summary.changed_count, 0)
        self.assertEqual(self.version_count(record["orderNo"]), 1)
        saved_date = self.connection.execute(
            "SELECT order_date FROM orders WHERE order_no=?",
            (record["orderNo"],),
        ).fetchone()["order_date"]
        self.assertEqual(saved_date, "2026-08-02")

    def test_invalid_date_pending_keeps_complete_review_context(self):
        record = valid_record("GOU26080034")
        record["orderDate"] = "2026.02.30"
        record["dateEvidence"] = {
            "selectedDate": None,
            "suggestedDate": "2026-08-03",
            "candidates": ["2026-08-03"],
            "dedicatedCandidates": ["2026-08-03"],
            "mainRegionCandidates": [],
            "confidence": "review",
            "anomalyReasons": ["日期格式無效"],
            "orderNumberYYMM": "2026-08",
        }
        summary = self.replay([record], name="invalid_date.json", create_images=True)
        self.assertEqual(summary.pending_count, 1)
        payload = json.loads(
            self.connection.execute(
                "SELECT raw_payload_json FROM review_queue WHERE order_no=?",
                (record["orderNo"],),
            ).fetchone()["raw_payload_json"]
        )
        context = payload["reviewContext"]
        self.assertEqual(context["suggested_date"], "2026-08-03")
        self.assertEqual(context["query_date_from"], "2026-07-27")
        self.assertEqual(context["query_date_to"], "2026-08-02")
        self.assertTrue(context["anomaly_reasons"])

    def test_gou_yymm_mismatch_is_pending_without_guessing_date(self):
        record = valid_record("GOU26070001")
        summary = self.replay([record], name="yymm_mismatch.json", create_images=True)
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(self.count("orders"), 0)
        reason = self.connection.execute(
            "SELECT reason FROM review_queue WHERE order_no=?",
            (record["orderNo"],),
        ).fetchone()["reason"]
        self.assertIn("GOUYYMM", reason)

    def test_automatic_review_resolution_retains_image_for_seven_days(self):
        record = valid_record("GOU26080035")
        record["statusLevel"] = "pending"
        record["errorReason"] = "初次OCR待確認"
        self.replay([record], name="pending_image.json", create_images=True)
        confirmed = copy.deepcopy(record)
        confirmed["statusLevel"] = "success"
        confirmed["errorReason"] = ""
        self.replay([confirmed], name="auto_resolved.json", create_images=True)
        row = self.connection.execute(
            """
            SELECT rq.status,wpi.processed_at,wpi.retained_until,wpi.image_status
            FROM review_queue rq
            JOIN web_pending_images wpi ON wpi.review_queue_id=rq.id
            WHERE rq.order_no=?
            """,
            (record["orderNo"],),
        ).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["image_status"], "available")
        processed = datetime.fromisoformat(row["processed_at"])
        retained = datetime.fromisoformat(row["retained_until"])
        self.assertGreaterEqual((retained - processed).days, 7)

    def test_pending_image_is_copied_and_linked_portably(self):
        record = valid_record("GOU26080032")
        record["statusLevel"] = "pending"
        record["errorReason"] = "測試待確認"
        summary = self.replay([record], create_images=True)
        self.assertEqual(summary.pending_count, 1)
        row = self.connection.execute(
            """
            SELECT wpi.relative_path, wpi.image_status
            FROM web_pending_images wpi
            JOIN review_queue rq ON rq.id=wpi.review_queue_id
            WHERE rq.order_no=?
            """,
            (record["orderNo"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["image_status"], "available")
        self.assertFalse(Path(row["relative_path"]).is_absolute())
        self.assertTrue((self.database.parent / row["relative_path"]).is_file())

    def test_phase2_validation_accepts_two_identical_baseline_replays(self):
        record = valid_record("GOU26080024")
        self.replay([record], name="seed.json")
        first = self.replay([record], name="first_baseline.json")
        second = self.replay([record], name="second_baseline.json")
        report = validate_phase2_replay(
            self.connection, first.scan_job_id, second.scan_job_id
        )
        self.assertTrue(report["passed"], report["checks"])


if __name__ == "__main__":
    unittest.main()
