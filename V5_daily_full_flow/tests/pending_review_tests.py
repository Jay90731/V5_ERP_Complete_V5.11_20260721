import argparse
import ctypes
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "pending_review.py"
SPEC = importlib.util.spec_from_file_location("pending_review_tests_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_entry(order_no):
    return {
        "order_no": order_no,
        "status": "待確認",
        "reason": "測試待確認",
        "source_images": [f"{order_no}.png"],
        "order": {
            "order_no": order_no,
            "department_code": "4001",
            "order_date": "2026-07-21",
            "source": {"source_path": "", "error": "測試待確認"},
        },
    }


def test_excel_marks_are_saved_by_order_number(directory):
    excel_path = directory / "current_orders.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "待確認"
    sheet.append(["狀態", "圖片名稱", "訂單編號", "部門代號", "訂貨日期", "原因", "處理結果", "處理備註"])
    sheet.append(["待確認", "GOU26079999.png", "GOU26079999", "4001", "2026-07-21", "測試", "重新辨識", "人工備註"])
    workbook.save(excel_path)
    workbook.close()
    current_path = directory / "current_orders.json"
    review_path = directory / "pending_review.json"
    write_json(current_path, {"orders": [pending_entry("GOU26079999")]})
    result = MODULE.command_sync(
        argparse.Namespace(
            excel=str(excel_path),
            review_json=str(review_path),
            current_orders=str(current_path),
        )
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    record = review["records"]["GOU26079999"]
    assert result["retry_requested"] == 1
    assert record["processing_result"] == "重新辨識"
    assert record["processing_note"] == "人工備註"


def test_unprocessed_record_survives_empty_current_batch(directory):
    review_path = directory / "pending_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    empty_current = directory / "empty_current.json"
    write_json(empty_current, {"orders": []})
    excel_path = directory / "next_run_without_excel.xlsx"
    result = MODULE.command_sync(
        argparse.Namespace(
            excel=str(excel_path),
            review_json=str(review_path),
            current_orders=str(empty_current),
        )
    )
    assert result["records"] == 1
    saved_review = json.loads(review_path.read_text(encoding="utf-8"))
    assert saved_review["records"]["GOU26079999"]["order_no"] == "GOU26079999"


def test_confirmed_exclusion_is_removed_from_batch(directory):
    review_path = directory / "exclude_review.json"
    next_path = directory / "exclude_next.json"
    report_path = directory / "exclude_report.json"
    ocr_path = directory / "orders.json"
    current_path = directory / "exclude_current.json"
    write_json(
        review_path,
        {
            "records": {
                "GOU26079998": {
                    "order_no": "GOU26079998",
                    "processing_result": "確認不納入",
                    "processing_note": "排除測試",
                    "state": "excluded",
                    "source_images": ["GOU26079998.png"],
                    "source_paths": [],
                }
            }
        },
    )
    write_json(
        ocr_path,
        {
            "orders": [
                {
                    "imageName": "GOU26079998.png",
                    "orderNo": "GOU26079998",
                    "statusLevel": "success",
                }
            ]
        },
    )
    write_json(current_path, {"orders": []})
    report = MODULE.command_apply(
        argparse.Namespace(
            review_json=str(review_path),
            output_review=str(next_path),
            current_orders=str(current_path),
            ocr_json=str(ocr_path),
            runs_root=str(directory),
            extractor=str(ROOT / "components" / "ocr" / "extract_orders.py"),
            tesseract="unused",
            tessdata="unused",
            report=str(report_path),
        )
    )
    payload = json.loads(ocr_path.read_text(encoding="utf-8"))
    assert report["removed_from_batch"] == 1
    assert payload["orders"] == []


def test_excel_lock_is_detected(directory):
    if os.name != "nt":
        return
    path = directory / "locked.xlsx"
    path.write_bytes(b"lock test")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert MODULE.is_excel_locked(path) is True
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    assert MODULE.is_excel_locked(path) is False


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        test_excel_marks_are_saved_by_order_number(directory)
        test_unprocessed_record_survives_empty_current_batch(directory)
        test_confirmed_exclusion_is_removed_from_batch(directory)
        test_excel_lock_is_detected(directory)
    print("Pending review tests passed: 4/4")
