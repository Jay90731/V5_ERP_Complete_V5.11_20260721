import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "ocr" / "extract_orders.py"
SPEC = importlib.util.spec_from_file_location("extract_orders", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_damaged_quantity_is_recovered_from_exact_arithmetic():
    text = """1 1 705297 $660 product 4? 660 31,020 540.00 25,380"""
    assert MODULE.parse_detail_rows(text) == [
        {
            "giftCode": "705297",
            "quantity": 47,
            "listPrice": 660,
            "totalAmount": 31020,
            "discountAmount": 25380,
        }
    ]


def test_spaced_thousands_separator_stays_one_amount():
    text = """total 1,144 1,075,360 732, 160"""
    assert MODULE.parse_summary_totals(text) == {
        "quantity": 1144,
        "totalAmount": 1075360,
        "discountAmount": 732160,
    }


def test_cached_raw_totals_are_reparsed():
    with tempfile.TemporaryDirectory() as directory:
        image_path = Path(directory) / "GOU26070362.png"
        MODULE.Image.new("RGB", (1400, 900), "white").save(image_path)
        previous = {
            "rawText": {
                "main": "GOU26070362 2026/07/20 4001 3",
                "department": "4001",
                "details": "705406 product 1,144 940 1,075,360 640.00 732,160",
                "totals": "total 1,144 1,075,360 732, 160",
            },
            "summaryTotals": {
                "quantity": 1144,
                "totalAmount": 1075360,
                "discountAmount": 732,
            },
        }
        reparsed = MODULE.reparse_previous(image_path, previous)
        assert reparsed["summaryTotals"]["discountAmount"] == 732160


if __name__ == "__main__":
    test_damaged_quantity_is_recovered_from_exact_arithmetic()
    test_spaced_thousands_separator_stays_one_amount()
    test_cached_raw_totals_are_reparsed()
    print("OCR numeric regression tests passed: 3/3")
