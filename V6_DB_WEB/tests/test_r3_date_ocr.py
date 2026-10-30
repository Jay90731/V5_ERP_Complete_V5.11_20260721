from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "ocr"))

from extract_orders import refresh_order_date_evidence


FIXTURES = ROOT / "tests" / "fixtures" / "r4_date_ocr"
EXPECTED = {
    "GOU26080034.png": "2026.08.03",
    "GOU26080170.png": "2026.08.07",
    "GOU26080244.png": "2026.08.11",
    "GOU26080424.png": "2026.08.21",
    "GOU26080426.png": "2026.08.22",
}


def runtime_paths() -> tuple[Path, Path]:
    candidates = [ROOT]
    candidates.extend(
        ROOT.glob("erp_returns/*/V6_DB_WEB_DUAL_R2_*/")
    )
    for candidate in candidates:
        tesseract = candidate / "runtime" / "tesseract" / "tesseract.exe"
        tessdata = candidate / "runtime" / "tesseract" / "tessdata"
        if tesseract.is_file() and tessdata.is_dir():
            return tesseract, tessdata
    raise unittest.SkipTest("R3 date OCR runtime is not available")


class R4DateOcrRegressionTests(unittest.TestCase):
    def test_five_real_images_prioritize_dedicated_date_crop(self):
        tesseract, tessdata = runtime_paths()
        wrong_dates = {
            "GOU26080034.png": "2026.03.03",
            "GOU26080170.png": "2026.03.07",
            "GOU26080244.png": "2026.03.11",
            "GOU26080424.png": "2026.03.21",
            "GOU26080426.png": "2026.08.02",
        }
        for image_name, expected in EXPECTED.items():
            with self.subTest(image=image_name):
                order = {
                    "orderNo": Path(image_name).stem,
                    "orderDate": wrong_dates[image_name],
                    "rawText": {},
                }
                result = refresh_order_date_evidence(
                    order,
                    FIXTURES / image_name,
                    tesseract,
                    tessdata,
                    "2026-02-25",
                    "2026-08-25",
                    force=True,
                )
                self.assertEqual(result["orderDate"], expected)
                evidence = result["dateEvidence"]
                self.assertIn(expected.replace(".", "-"), evidence["candidates"])
                self.assertEqual(evidence["confidence"], "review")
                self.assertTrue(evidence["anomalyReasons"])


if __name__ == "__main__":
    unittest.main()
