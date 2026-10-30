from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from v6_db_web.phase3_pipeline import (
    Phase3Pipeline,
    RunState,
    atomic_json,
    capture_image_paths,
    capture_arguments_for_mode,
    load_config,
    query_range_for_mode,
    reconcile_sequential_capture_names,
    run_phase3,
)
from v6_db_web.phase3_validation import validate_phase3_runs


FIXTURE = ROOT / "tests" / "fixtures" / "daily_7_days_ocr.json"


class Phase3PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        shutil.copytree(ROOT / "migrations", self.root / "migrations")
        self.config_path = self.root / "phase3_config.json"
        self.write_config("data/orders_test.db")

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, database_path):
        payload = {
            "version": "V6_PHASE3_TEST",
            "database_path": database_path,
            "runs_path": "runs",
            "control_path": "control",
            "erp_exe_path": "E:/MIS/GI_System.exe",
            "capture_script": "components/capture.ps1",
            "ocr_script": "components/extract_orders.py",
            "powershell_candidates": ["powershell.exe"],
            "tesseract_candidates": ["tesseract.exe"],
            "tessdata_candidates": ["tessdata"],
            "daily_range_days": 7,
            "minimum_wait_seconds": 2,
            "maximum_wait_seconds": 5,
            "maximum_orders": 2000,
        }
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def latest_state(self):
        latest = (self.root / "latest_run_path.txt").read_text(
            encoding="utf-8"
        )
        return json.loads(
            (Path(latest) / "run_state.json").read_text(encoding="utf-8")
        )

    def database_value(self, sql):
        connection = sqlite3.connect(self.root / "data" / "orders_test.db")
        try:
            return connection.execute(sql).fetchone()[0]
        finally:
            connection.close()

    def test_atomic_json_replaces_existing_file_under_concurrent_writes(self):
        target = self.root / "state" / "run_state.json"
        atomic_json(target, {"writer": -1, "values": []})

        def write(index):
            atomic_json(target, {"writer": index, "values": [index] * 10})

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(80)))

        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["values"], [payload["writer"]] * 10)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_resume_clears_stale_stop_flag_before_loading_state(self):
        config = load_config(self.config_path)
        state = RunState.create(
            config.runs_root,
            "erp_daily_7_days",
            "2026-07-28",
            "2026-08-03",
        )
        state.payload["status"] = "stopped"
        state.save()
        stop_flag = config.control_root / "STOP_V6_NOW.flag"
        stop_flag.parent.mkdir(parents=True, exist_ok=True)
        stop_flag.write_text("old stop", encoding="utf-8")

        pipeline = Phase3Pipeline(config, resume=True)
        pipeline.initialize()

        self.assertFalse(stop_flag.exists())
        self.assertEqual(pipeline.run_state.payload["status"], "running")

    def test_formal_database_path_is_rejected(self):
        self.write_config("data/orders.db")
        with self.assertRaisesRegex(ValueError, "orders_test.db"):
            load_config(self.config_path)
        self.assertFalse((self.root / "data" / "orders.db").exists())

    def test_dual_modes_use_expected_dates_and_capture_arguments(self):
        self.assertEqual(
            query_range_for_mode("daily_7_days", date(2026, 8, 4)),
            ("2026-07-29", "2026-08-04"),
        )
        self.assertEqual(
            query_range_for_mode("monthly_6_months", date(2026, 8, 4)),
            ("2026-02-04", "2026-08-04"),
        )
        self.assertEqual(
            capture_arguments_for_mode("daily_7_days"),
            ["-ShortRangeDays", "7"],
        )
        self.assertEqual(
            capture_arguments_for_mode("monthly_6_months"),
            ["-FullSixMonths"],
        )
        self.assertEqual(
            query_range_for_mode("daily_7_days", date(2026, 8, 3)),
            ("2026-07-28", "2026-08-03"),
        )
        with self.assertRaisesRegex(ValueError, "Unsupported pipeline mode"):
            capture_arguments_for_mode("history_monthly")

    def test_both_modes_record_job_type_and_rerun_without_versions(self):
        ranges = {
            "daily_7_days": ("2026-07-29", "2026-08-04"),
            "monthly_6_months": ("2026-02-04", "2026-08-04"),
        }
        version_counts = []
        job_types = []
        for mode, (date_from, date_to) in ranges.items():
            for _ in range(2):
                state = run_phase3(
                    self.config_path,
                    replay_ocr=FIXTURE,
                    date_from=date_from,
                    date_to=date_to,
                    mode=mode,
                )
                self.assertEqual(state["run_mode"], mode)
                version_counts.append(
                    self.database_value("SELECT COUNT(*) FROM order_versions")
                )
                connection = sqlite3.connect(
                    self.root / "data" / "orders_test.db"
                )
                try:
                    job_types.append(
                        connection.execute(
                            "SELECT job_type FROM scan_jobs WHERE id = ?",
                            (state["scan_job_id"],),
                        ).fetchone()[0]
                    )
                finally:
                    connection.close()
        self.assertEqual(
            job_types,
            [
                "daily_7_days",
                "daily_7_days",
                "monthly_6_months",
                "monthly_6_months",
            ],
        )
        self.assertGreater(version_counts[0], 0)
        self.assertEqual(version_counts, [version_counts[0]] * 4)

    def test_launcher_exposes_two_independent_mode_buttons(self):
        launcher = (ROOT / "phase3_launcher.ps1").read_text(
            encoding="utf-8-sig"
        )
        worker = (ROOT / "phase3_worker.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("開始每日 7 天流程", launcher)
        self.assertIn("開始最近 6 個月流程", launcher)
        self.assertIn('Start-Worker -Mode "daily_7_days"', launcher)
        self.assertIn('Start-Worker -Mode "monthly_6_months"', launcher)
        self.assertIn('[string]$Mode = "daily_7_days"', worker)
        self.assertIn('"--mode"', worker)
        self.assertNotIn("history", launcher.lower())
        self.assertNotIn("history", worker.lower())

    def run_capture_guard(self, expression: str):
        helper = ROOT / "components" / "erp_capture" / "capture_guard_helpers.ps1"
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f". '{helper}'; {expression} | ConvertTo-Json -Compress",
        ]
        result = subprocess.run(command, capture_output=True, check=True)
        return json.loads(result.stdout.decode("utf-8-sig"))

    def test_actual_first_query_result_accepts_non_0001_and_cross_month(self):
        non_0001 = self.run_capture_guard(
            "Get-QueryFirstOrderDecision "
            "-DetectedOrders @('GOU26080219','GOU26080220') "
            "-SelectedOrder GOU26080219 -DetectionReliable $true"
        )
        cross_month = self.run_capture_guard(
            "Get-QueryFirstOrderDecision "
            "-DetectedOrders @('GOU26073199','GOU26080002') "
            "-SelectedOrder GOU26073199 -DetectionReliable $true"
        )
        self.assertEqual(non_0001["Action"], "start")
        self.assertEqual(non_0001["OrderNumber"], "GOU26080219")
        self.assertEqual(cross_month["Action"], "start")
        self.assertEqual(cross_month["OrderNumber"], "GOU26073199")

    def test_both_modes_accept_a_non_0001_first_order(self):
        for mode, order_no in (
            ("daily_7_days", "GOU26080219"),
            ("monthly_6_months", "GOU26070611"),
        ):
            decision = self.run_capture_guard(
                "Get-QueryFirstOrderDecision "
                f"-DetectedOrders @('{order_no}') -SelectedOrder {order_no} "
                "-DetectionReliable $false -ClickSucceeded $true "
                "-FocusMatched $true"
            )
            self.assertEqual(decision["Action"], "start", mode)
            self.assertEqual(decision["OrderNumber"], order_no, mode)

    def test_unreliable_list_ocr_does_not_block_known_header_order(self):
        decision = self.run_capture_guard(
            "Get-QueryFirstOrderDecision "
            "-DetectedOrders @() -SelectedOrder GOU26080219 "
            "-DetectionReliable $false -ClickSucceeded $true "
            "-FocusMatched $true"
        )
        self.assertEqual(decision["Action"], "start")
        self.assertTrue(decision["OrderNumberKnown"])
        self.assertEqual(decision["OrderNumber"], "GOU26080219")

    def test_unreadable_list_and_header_capture_provisional_first_image(self):
        decision = self.run_capture_guard(
            "Get-QueryFirstOrderDecision "
            "-DetectedOrders @() -SelectedOrder '' "
            "-DetectionReliable $false -ClickSucceeded $true "
            "-FocusMatched $true -ProvisionalOrder seq_0001"
        )
        self.assertEqual(decision["Action"], "provisional")
        self.assertFalse(decision["OrderNumberKnown"])
        self.assertEqual(decision["OrderNumber"], "seq_0001")

    def test_zero_query_results_complete_without_selecting_stale_order(self):
        decision = self.run_capture_guard(
            "Get-QueryFirstOrderDecision -DetectedOrders @() "
            "-SelectedOrder '' -DetectionReliable $true"
        )
        self.assertEqual(decision["Action"], "empty")
        self.assertEqual(decision["Reason"], "query_returned_zero_orders")

    def test_list_ocr_mismatch_is_only_auxiliary(self):
        decision = self.run_capture_guard(
            "Get-QueryFirstOrderDecision "
            "-DetectedOrders @('GOU26080219','GOU26080220') "
            "-SelectedOrder GOU26080220 -DetectionReliable $true"
        )
        self.assertEqual(decision["Action"], "start")
        self.assertEqual(decision["OrderNumber"], "GOU26080220")

    def test_first_row_click_or_focus_failure_still_fails(self):
        click = self.run_capture_guard(
            "Get-QueryFirstOrderDecision -DetectedOrders @('GOU26080219') "
            "-SelectedOrder '' -DetectionReliable $false "
            "-ClickSucceeded $false -FocusMatched $true"
        )
        focus = self.run_capture_guard(
            "Get-QueryFirstOrderDecision -DetectedOrders @('GOU26080219') "
            "-SelectedOrder '' -DetectionReliable $false "
            "-ClickSucceeded $true -FocusMatched $false"
        )
        self.assertEqual(click["Reason"], "first_query_result_click_failed")
        self.assertEqual(focus["Reason"], "first_query_result_focus_failed")

    def make_capture(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 700), "white").save(path, "PNG")

    def test_sequential_capture_is_validated_and_renamed_after_ocr(self):
        batch = self.root / "batch"
        capture = batch / "captures" / "seq_0001.png"
        ocr_input = self.root / "run" / "ocr" / "input_images" / "seq_0001.png"
        self.make_capture(capture)
        self.make_capture(ocr_input)
        ocr_json = self.root / "run" / "ocr" / "data" / "orders.json"
        ocr_json.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            ocr_json,
            {
                "orders": [
                    {
                        "imageName": "seq_0001.png",
                        "sourcePath": str(ocr_input),
                        "orderNo": "GOU26080219",
                    }
                ],
                "summary": {"completed": True, "completedImages": 1},
            },
        )

        self.assertEqual([path.name for path in capture_image_paths(capture.parent)], ["seq_0001.png"])
        self.assertEqual(reconcile_sequential_capture_names(ocr_json, batch), 1)
        payload = json.loads(ocr_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["orders"][0]["imageName"], "GOU26080219.png")
        self.assertTrue((capture.parent / "GOU26080219.png").is_file())
        self.assertTrue((ocr_input.parent / "GOU26080219.png").is_file())
        self.assertFalse(capture.exists())

    def test_damaged_capture_is_rejected(self):
        captures = self.root / "damaged" / "captures"
        captures.mkdir(parents=True)
        (captures / "seq_0001.png").write_bytes(b"not-a-png" * 2000)
        with self.assertRaisesRegex(ValueError, "截圖損壞"):
            capture_image_paths(captures)

    def test_unreadable_sequential_ocr_enters_review_queue(self):
        image = self.root / "pending_source" / "seq_0001.png"
        self.make_capture(image)
        payload = self.root / "seq_pending.json"
        payload.write_text(
            json.dumps(
                {
                    "version": 2,
                    "orders": [
                        {
                            "imageName": image.name,
                            "sourcePath": str(image),
                            "orderNo": None,
                            "statusLevel": "pending",
                            "errorReason": "訂單編號未辨識",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state = run_phase3(
            self.config_path,
            replay_ocr=payload,
            date_from="2026-07-29",
            date_to="2026-08-04",
            mode="daily_7_days",
        )
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["failed_count"], 0)
        self.assertEqual(
            self.database_value(
                "SELECT COUNT(*) FROM review_queue WHERE status='pending'"
            ),
            1,
        )

    def test_zero_order_replay_is_valid_for_both_modes(self):
        empty = self.root / "empty_orders.json"
        empty.write_text(
            json.dumps(
                {"version": 2, "targetDepartment": "4001", "orders": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for mode, date_from in (
            ("daily_7_days", "2026-07-28"),
            ("monthly_6_months", "2026-02-03"),
        ):
            state = run_phase3(
                self.config_path,
                replay_ocr=empty,
                date_from=date_from,
                date_to="2026-08-03",
                mode=mode,
            )
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["screenshot_count"], 0)
            connection = sqlite3.connect(self.root / "data" / "orders_test.db")
            try:
                job = connection.execute(
                    "SELECT job_type,total_count,failed_count FROM scan_jobs WHERE id=?",
                    (state["scan_job_id"],),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(job, (mode, 0, 0))

    def test_active_runtime_contains_only_two_modes_and_one_database(self):
        pipeline = (ROOT / "src" / "v6_db_web" / "phase3_pipeline.py").read_text(
            encoding="utf-8-sig"
        )
        cli = (ROOT / "src" / "v6_db_web" / "cli.py").read_text(
            encoding="utf-8-sig"
        )
        config = json.loads((ROOT / "phase3_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["database_path"].replace("\\", "/"), "data/orders.db")
        self.assertNotIn("history_monthly", pipeline)
        self.assertNotIn("formal_history", pipeline)
        self.assertNotIn("history-scan", cli)
        self.assertNotIn("import-formal-history-month", cli)
        self.assertFalse((ROOT / "src" / "v6_db_web" / "history_scan.py").exists())
        self.assertFalse((ROOT / "src" / "v6_db_web" / "history_validation.py").exists())

    def test_replay_runs_complete_pipeline_and_writes_state(self):
        state = run_phase3(
            self.config_path,
            replay_ocr=FIXTURE,
            date_from="2026-07-27",
            date_to="2026-08-02",
        )
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["screenshot_count"], 4)
        self.assertEqual(state["ocr_count"], 4)
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["failed_count"], 0)
        self.assertIsNotNone(state["scan_job_id"])
        self.assertTrue(Path(state["summary_path"]).is_file())
        self.assertEqual(
            [stage["status"] for stage in state["stages"]],
            [
                "skipped",
                "skipped",
                "skipped",
                "skipped",
                "completed",
                "completed",
            ],
        )
        self.assertEqual(self.latest_state()["run_id"], state["run_id"])

    def test_same_data_twice_does_not_add_versions_or_reviews(self):
        run_phase3(
            self.config_path,
            replay_ocr=FIXTURE,
            date_from="2026-07-27",
            date_to="2026-08-02",
        )
        first = run_phase3(
            self.config_path,
            replay_ocr=FIXTURE,
            date_from="2026-07-27",
            date_to="2026-08-02",
        )
        versions_after_first = self.database_value(
            "SELECT COUNT(*) FROM order_versions"
        )
        second = run_phase3(
            self.config_path,
            replay_ocr=FIXTURE,
            date_from="2026-07-27",
            date_to="2026-08-02",
        )
        versions_after_second = self.database_value(
            "SELECT COUNT(*) FROM order_versions"
        )
        reviews = self.database_value(
            "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
        )
        connection = sqlite3.connect(
            self.root / "data" / "orders_test.db"
        )
        try:
            second_job = connection.execute(
                """
                SELECT success_count, unchanged_count, changed_count,
                       pending_count, excluded_count, failed_count
                FROM scan_jobs WHERE id = ?
                """,
                (second["scan_job_id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(versions_after_first, versions_after_second)
        self.assertEqual(reviews, 1)
        self.assertEqual(second_job, (0, 1, 0, 1, 2, 0))
        self.assertNotEqual(first["run_id"], second["run_id"])
        report = validate_phase3_runs(
            Path(first["summary_path"]).parent,
            Path(second["summary_path"]).parent,
        )
        self.assertTrue(report["passed"], report["checks"])

    def test_database_remains_valid_after_pipeline(self):
        run_phase3(
            self.config_path,
            replay_ocr=FIXTURE,
            date_from="2026-07-27",
            date_to="2026-08-02",
        )
        connection = sqlite3.connect(
            self.root / "data" / "orders_test.db"
        )
        try:
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                len(connection.execute("PRAGMA foreign_key_check").fetchall()),
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM official_orders WHERE store_id IS NOT NULL"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_capture_queries_then_maximizes_before_selecting_first_order(self):
        script = (
            ROOT
            / "components"
            / "erp_capture"
            / "capture_erp_v4_full_batch.ps1"
        ).read_text(encoding="utf-8-sig")
        query = script.index('Invoke-Step -Name "query_gou_orders"')
        maximize = script.index(
            'Invoke-Step -Name "maximize_order_window"', query
        )
        select_first = script.index(
            'Invoke-Step -Name "select_first_query_result"', maximize
        )
        self.assertLess(query, maximize)
        self.assertLess(maximize, select_first)
        self.assertIn("WM_MDIMAXIMIZE", script)
        self.assertIn("$minimumWidth = 1700", script)
        self.assertIn("$minimumHeight = 800", script)
        self.assertIn("Select-FirstQueryResult", script)
        self.assertIn("query_returned_zero_orders", script)
        self.assertIn('ProvisionalOrder "seq_0001"', script)
        self.assertIn("Screenshot for $OrderNumber is damaged or unreadable", script)
        self.assertNotIn("query_result_detection_unreliable", script)
        self.assertNotIn("Get-ExpectedMonthFirstOrder", script)
        self.assertNotIn("scroll_and_verify_month_first_order", script)
        self.assertNotIn("GOUYYMM0001", script)

    def test_r3_maximize_diagnostics_and_manual_fallback_are_wired(self):
        capture = (
            ROOT
            / "components"
            / "erp_capture"
            / "capture_erp_v4_full_batch.ps1"
        ).read_text(encoding="utf-8-sig")
        pipeline = (
            ROOT / "src" / "v6_db_web" / "phase3_pipeline.py"
        ).read_text(encoding="utf-8-sig")
        launcher = (ROOT / "phase3_launcher.ps1").read_text(
            encoding="utf-8-sig"
        )
        environment = (ROOT / "check_phase3_environment.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("Get-WindowParentHierarchy", capture)
        self.assertIn("ClassName -match '(?i)MDICLIENT'", capture)
        self.assertIn("WM_MDIMAXIMIZE", capture)
        self.assertIn("WM_SYSCOMMAND", capture)
        self.assertIn("SC_MAXIMIZE", capture)
        self.assertIn("TITLE_BAR_DOUBLE_CLICK", capture)
        self.assertIn("CALCULATED_MAXIMIZE_BUTTON", capture)
        self.assertIn(
            "Please manually maximize the ERP order child window.", capture
        )
        self.assertIn("ManualMaximizeRequestPath", capture)
        self.assertIn("ManualMaximizeContinueFlagPath", capture)
        self.assertIn('"-ManualMaximizeRequestPath"', pipeline)
        self.assertIn('"-ManualMaximizeContinueFlagPath"', pipeline)
        self.assertIn("MANUAL_MAXIMIZE_REQUIRED.flag", launcher)
        self.assertIn("manualMaximizeContinue", launcher)
        self.assertIn('Get-Date -Format "yyyyMMdd_HHmmss"', environment)

    def test_r4_targets_order_list_control_without_down_sendkeys(self):
        capture = (
            ROOT
            / "components"
            / "erp_capture"
            / "capture_erp_v4_full_batch.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("V6-PHASE3-R4", capture)
        self.assertIn("Write-ErpProcessDiagnostic", capture)
        self.assertIn("WindowFromPoint", capture)
        self.assertIn("$arrowX = 18", capture)
        self.assertIn("AttachThreadInput", capture)
        self.assertIn("SetFocus", capture)
        self.assertIn("Post-KeyToOrderListControl", capture)
        self.assertIn("WM_KEYDOWN", capture)
        self.assertIn("WM_KEYUP", capture)
        self.assertIn("VK_DOWN", capture)
        self.assertIn("Click-NextOrderRowFallback", capture)
        self.assertIn("ORDER TRANSITION VERIFIED", capture)
        self.assertNotIn('SendWait("{DOWN}")', capture)

    def test_r41_atomic_state_resume_and_environment_report_are_wired(self):
        capture = (
            ROOT
            / "components"
            / "erp_capture"
            / "capture_erp_v4_full_batch.ps1"
        ).read_text(encoding="utf-8-sig")
        worker = (ROOT / "phase3_worker.ps1").read_text(
            encoding="utf-8-sig"
        )
        pipeline = (
            ROOT / "src" / "v6_db_web" / "phase3_pipeline.py"
        ).read_text(encoding="utf-8-sig")

        state_writer = capture[
            capture.index("function Save-BatchState") :
            capture.index("function Write-BatchSummary")
        ]
        self.assertIn("MoveFileEx", state_writer)
        self.assertIn("WeddingOrderV6BatchState_", state_writer)
        self.assertIn("for ($attempt = 1; $attempt -le 12", state_writer)
        self.assertNotIn("Move-Item", state_writer)
        self.assertIn("Clear-StopFlag", capture)
        self.assertIn("phase3_environment_auto_", worker)
        self.assertIn("check-phase3", worker)
        self.assertGreaterEqual(worker.count("Clear-ControlFlag -Path $StopFlag"), 2)
        self.assertGreaterEqual(pipeline.count("clear_control_flag(self.stop_flag)"), 3)


if __name__ == "__main__":
    unittest.main()
