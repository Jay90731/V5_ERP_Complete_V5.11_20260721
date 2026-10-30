import argparse
import csv
import json
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
TARGET_DEPARTMENT = "4001"
EXTRACTOR_VERSION = 25
DATE_EVIDENCE_VERSION = 1
OCR_PASS_SEPARATOR = "@@@PASS@@@"


def replace_text_with_retry(path: Path, text: str, retries: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(retries):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
CATEGORY_LABELS = {
    "1": "婚約",
    "2": "彌月",
    "3": "節慶",
    "9": "其他",
}
WINDOW_HINT_BOX = (0.20, 0.14, 0.80, 0.86)
FULL_SCREEN_ERP_BOX = (8 / 1936, 57 / 1056, 1054 / 1936, 766 / 1056)
FULL_SCREEN_DETAIL_REGION = (0.10, 0.49, 0.52, 0.90)
FULL_SCREEN_TOTALS_REGION = (0.235, 0.89, 0.51, 0.935)
FULL_SCREEN_VOID_REGION = (0.405, 0.10, 0.47, 0.20)
VOID_RED_PIXEL_THRESHOLD = 100
REGIONS = {
    "order_main": (0.18, 0.025, 0.90, 0.29),
}
FIELD_REGIONS = {
    "order_date": (0.18, 0.065, 0.43, 0.18),
    "department": (0.18, 0.175, 0.58, 0.26),
}
DATE_RETRY_REGIONS = (
    (0.245, 0.075, 0.410, 0.170),
    (0.220, 0.060, 0.440, 0.190),
    (0.260, 0.080, 0.380, 0.150),
)
MANUAL_HEADERS = [
    "啟用",
    "圖片",
    "訂單編號",
    "訂單分類",
    "日期",
    "禮盒代號",
    "定價",
    "數量",
    "總金額",
    "折後",
    "動作",
    "備註",
]


def normalize_digits(value: str) -> str:
    table = str.maketrans("０１２３４５６７８９ＯｏＩｌ｜，", "0123456789OoIl|,")
    text = str(value or "").translate(table)
    return (
        text.replace("O", "0")
        .replace("o", "0")
        .replace("I", "1")
        .replace("l", "1")
        .replace("|", "1")
    )


def normalize_text(value: str) -> str:
    text = normalize_digits(value)
    replacements = {
        "訂單編戰": "訂單編號",
        "訂單編碼": "訂單編號",
        "訂貨日期": "訂貨日期",
        "訂貨日": "訂貨日期",
        "部門代號": "部門代號",
        "部門代碼": "部門代號",
        "部門代": "部門代號",
        "訂單區分": "訂單區分",
        "禮盒代號": "禮盒代號",
        "禮盒代碼": "禮盒代號",
        "折扣後金額": "折扣後金額",
        "折扣後單價": "折扣後單價",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def only_digits(value: str) -> str:
    return re.sub(r"\D+", "", normalize_digits(value or ""))


def to_int(value):
    digits = only_digits(str(value or ""))
    if not digits:
        return None
    return int(digits)


def to_money(value):
    return to_int(value)


def normalize_date(value: str):
    text = normalize_digits(value)
    match = re.search(r"(20\d{2})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", text)
    if not match:
        compact = re.search(r"(20\d{2})\D{1,3}(\d{4,5})", text)
        if not compact:
            return None
        year, md = compact.groups()
        if len(md) == 5 and md.startswith("0"):
            md = md[:2] + md[-2:]
        else:
            md = md[-4:]
        return f"{int(year):04d}.{int(md[:2]):02d}.{int(md[2:]):02d}"
    year, month, day = match.groups()
    return f"{int(year):04d}.{int(month):02d}.{int(day):02d}"


def expand_box(box, width, height):
    x1, y1, x2, y2 = box
    return (
        max(0, int(width * x1)),
        max(0, int(height * y1)),
        min(width, int(width * x2)),
        min(height, int(height * y2)),
    )


def trim_plain_border(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    bbox = gray.point(lambda p: 255 if p < 245 else 0).getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    if right - left < image.width * 0.35 or bottom - top < image.height * 0.35:
        return image
    return image.crop((left, top, right, bottom))


def crop_order_window(image: Image.Image) -> Image.Image:
    if image.width < 1400:
        return trim_plain_border(image)
    # Current full-screen ERP captures use the 1936x1056 remote layout and
    # place the order child window at the upper-left.
    # Crop only during OCR; keep the stored screenshot intact for audit/use.
    hinted = image.crop(expand_box(FULL_SCREEN_ERP_BOX, image.width, image.height))
    return trim_plain_border(hinted)


def detect_void_stamp(image: Image.Image) -> bool:
    """Detect the red 作廢 stamp in the order form's upper-right corner."""
    crop = image.convert("RGB").crop(
        expand_box(FULL_SCREEN_VOID_REGION, image.width, image.height)
    )
    red_pixels = 0
    pixels = (
        crop.get_flattened_data()
        if hasattr(crop, "get_flattened_data")
        else crop.getdata()
    )
    for red, green, blue in pixels:
        if (
            red >= 170
            and red * 2 >= green * 3
            and red * 2 >= blue * 3
            and green <= 140
            and blue <= 140
        ):
            red_pixels += 1
            if red_pixels >= VOID_RED_PIXEL_THRESHOLD:
                return True
    return False


def image_has_void_stamp(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            return detect_void_stamp(image)
    except Exception:
        return False


def preprocess_for_ocr(image: Image.Image, threshold: bool) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    if threshold:
        gray = gray.point(lambda p: 255 if p > 170 else 0)
    scale = 2
    return gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)


def run_tesseract(image: Image.Image, tesseract: Path, tessdata: Path, lang: str, psm: str) -> str:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = Path(tmp_dir) / "ocr.png"
        output_base = Path(tmp_dir) / "out"
        image.save(input_path)
        command = [
            str(tesseract),
            str(input_path),
            str(output_base),
            "-l",
            lang,
            "--tessdata-dir",
            str(tessdata),
            "--psm",
            psm,
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_path = output_base.with_suffix(".txt")
        return out_path.read_text(encoding="utf-8", errors="ignore")


def ocr_region(
    window: Image.Image,
    box,
    tesseract: Path,
    tessdata: Path,
    normalize_output: bool = True,
) -> str:
    crop = window.crop(expand_box(box, window.width, window.height))
    chunks = []
    for threshold in (True, False):
        prepared = preprocess_for_ocr(crop, threshold)
        try:
            chunks.append(run_tesseract(prepared, tesseract, tessdata, "chi_tra+eng", "6"))
        except subprocess.CalledProcessError as exc:
            chunks.append(exc.stderr.decode("utf-8", errors="ignore"))
    combined = f"\n{OCR_PASS_SEPARATOR}\n".join(chunks).strip()
    return normalize_text(combined) if normalize_output else combined


def infer_order_no(text: str):
    candidates = []
    for match in re.finditer(r"(?:訂單編號|訂單編)\D{0,8}([A-Z0-9\s]{5,18})", text, re.I):
        candidates.append(match.group(1))
    for match in re.finditer(r"\b([A-Z]{2,4}\d{8,12})\b", text, re.I):
        candidates.append(match.group(1))
    for raw in candidates:
        normalized = clean_order_no(raw)
        if re.search(r"\d{8,12}$", normalized):
            return normalized
    return None


def clean_order_no(value):
    text = re.sub(r"\s+", "", str(value or "").upper())
    text = text.replace("0U", "OU").replace("G0U", "GOU").replace("6OU", "GOU")
    text = text.replace("GO0", "GOU").replace("G00", "GOU")
    digits = only_digits(text)
    if text.startswith("OU") and len(digits) >= 8:
        return "GOU" + digits[-8:]
    if text.startswith("GOU") and len(digits) >= 8:
        return "GOU" + digits[-8:]
    if re.fullmatch(r"[60O]{2,3}\d{8,9}", text) or re.fullmatch(r"\d{10,12}", text):
        return "GOU" + digits[-8:]
    return text


def infer_department(text: str):
    # Department is the gatekeeper: only a clearly labeled 4001 may enter Excel.
    # OCR often reads 部門 as 部站/部同, and 4100/4011 must not be rescued by a later
    # unrelated 4001 from the surrounding ERP screen.
    text = normalize_text(text)
    if re.search(r"(?:部門|部站|部同|部代)[^\n\r]{0,32}\bBD\b|商務開發", text, re.I):
        return "4011"
    if re.search(r"Web\s*shop|Webshop|L1NE|LINE", text, re.I):
        return "4100"
    label_pattern = r"(?:部門|部站|部同|部代|Bast|BBP|asrveae)[^\n\r0-9OIl|]{0,12}([4O][0-9OIl|]{3})"
    for match in re.finditer(label_pattern, text, re.I):
        digits = only_digits(match.group(1))
        if len(digits) == 4:
            return digits
    broken_label_pattern = r"(?:站|部|代號|BBP|spree|者站|導站)[^\n\r]{0,20}\b(40[01]0|4011|4100)\b"
    for match in re.finditer(broken_label_pattern, text, re.I):
        digits = only_digits(match.group(1))
        if len(digits) == 4:
            return digits
    if re.search(r"(?:部門|部站|部同|部代)[^\n\r]{0,28}Show\s*room|Showroom", text, re.I):
        return "4001"
    return None


def infer_department_field(text: str, fallback_text: str = ""):
    raw = str(text or "")
    normalized = normalize_text(raw)
    candidates = {
        match.group(1)
        for match in re.finditer(r"(?<!\d)(4\d{3})(?!\d)", normalized)
    }

    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        return None
    if re.search(r"(?:部門|部站|部同|部代)[^\n\r]{0,32}\bBD\b|商務開發", raw, re.I):
        return "4011"
    if re.search(r"(?:部門|部站|部同|部代)[^\n\r]{0,32}(?:Web\s*shop|Webshop)|L1NE|LINE", raw, re.I):
        return "4100"
    if re.search(r"(?:部門|部站|部同|部代)[^\n\r]{0,32}(?:Show\s*room|Showroom)", raw, re.I):
        return "4001"
    return infer_department(fallback_text or raw)


def infer_category(text: str):
    match = re.search(r"訂單區分\D{0,8}([1239])", text)
    if match:
        return CATEGORY_LABELS.get(match.group(1), "")
    for key, label in CATEGORY_LABELS.items():
        if label in text and re.search(rf"{key}\D{{0,4}}{label}", text):
            return label
    for label in CATEGORY_LABELS.values():
        if label in text:
            return label
    return ""


def infer_date(text: str):
    labeled = re.search(r"訂貨日期\D{0,12}(20\d{2}\D{1,3}\d{1,2}\D{1,3}\d{1,2})", text)
    if labeled:
        return normalize_date(labeled.group(1))
    return normalize_date(text)


def parsed_date(value):
    normalized = normalize_date(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y.%m.%d").date()
    except ValueError:
        return None


def date_in_query_range(value, date_from, date_to):
    order_date = parsed_date(value)
    start = parsed_date(date_from)
    end = parsed_date(date_to)
    return bool(order_date and start and end and start <= order_date <= end)


def strict_date_candidates(text, source):
    """Extract only literal numeric dates; OCR letters are never repaired."""
    translated = str(text or "").translate(
        str.maketrans("０１２３４５６７８９", "0123456789")
    )
    results = []
    for pass_index, chunk in enumerate(
        translated.split(OCR_PASS_SEPARATOR), start=1
    ):
        for match in re.finditer(
            r"(?<![0-9A-Za-z])(20[0-9]{2})\s*[/.-]\s*"
            r"([0-9]{1,2})\s*[/.-]\s*([0-9]{1,2})(?![0-9A-Za-z])",
            chunk,
        ):
            year, month, day = (int(value) for value in match.groups())
            try:
                candidate = datetime(year, month, day).date().isoformat()
            except ValueError:
                continue
            results.append(
                {
                    "date": candidate,
                    "source": source,
                    "pass": pass_index,
                    "text": match.group(0),
                }
            )
    return results


def has_ambiguous_date_token(text):
    translated = str(text or "").translate(
        str.maketrans("０１２３４５６７８９", "0123456789")
    )
    return bool(
        re.search(
            r"20[0-9]{2}\s*[/.-]\s*[0-9A-Za-z]{1,2}\s*[/.-]\s*"
            r"(?=[0-9A-Za-z]{1,3})(?=[0-9A-Za-z]*[A-Za-z])[0-9A-Za-z]{1,3}",
            translated,
        )
    )


def order_yymm(order_no):
    match = re.fullmatch(r"GOU([0-9]{2})([0-9]{2})[0-9A-Z-]*", str(order_no or ""))
    if not match:
        return None
    month = int(match.group(2))
    return f"20{match.group(1)}-{month:02d}" if 1 <= month <= 12 else None


def build_date_evidence(
    date_text_raw,
    main_text_raw,
    order_no,
    date_from=None,
    date_to=None,
    retry_attempts=None,
):
    details = strict_date_candidates(date_text_raw, "date_crop")
    for index, attempt in enumerate(retry_attempts or [], start=1):
        details.extend(
            strict_date_candidates(
                attempt.get("text"), f"date_crop_retry_{index}"
            )
        )
    details.extend(strict_date_candidates(main_text_raw, "main_region"))
    dedicated = [
        item["date"] for item in details if item["source"].startswith("date_crop")
    ]
    full_screen = [
        item["date"] for item in details if item["source"] == "main_region"
    ]
    dedicated_counts = Counter(dedicated)
    selected = dedicated[0] if dedicated else (full_screen[0] if full_screen else None)
    if dedicated_counts:
        ranked = dedicated_counts.most_common()
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            selected = ranked[0][0]

    unique_dedicated = list(dict.fromkeys(dedicated))
    unique_full = list(dict.fromkeys(full_screen))
    all_candidates = list(dict.fromkeys(dedicated + full_screen))
    reasons = []
    if not selected:
        reasons.append("無法取得日期OCR候選")
    if len(dedicated) < 2:
        reasons.append("日期專用裁切缺少多次辨識共識")
    if len(unique_dedicated) > 1:
        reasons.append("日期專用裁切OCR候選不一致")
    if selected and any(value != selected for value in unique_full):
        reasons.append("日期專用裁切與主區域OCR結果不同")
    if len(all_candidates) > 1:
        reasons.append("多次OCR的日期候選不一致")
    if has_ambiguous_date_token(date_text_raw) or has_ambiguous_date_token(main_text_raw):
        reasons.append("日期含英文字母或模糊字元")
    if selected and date_from and date_to and not date_in_query_range(
        selected, date_from, date_to
    ):
        reasons.append("訂貨日期超出本次查詢範圍")
    expected_yymm = order_yymm(order_no)
    if selected and expected_yymm and selected[:7] != expected_yymm:
        reasons.append("訂貨日期年月與訂單編號GOUYYMM不一致")
    dedicated_consensus = (
        len(dedicated) >= 2 and len(unique_dedicated) == 1
    )
    confidence = "trusted" if dedicated_consensus and not reasons else "review"
    return {
        "version": DATE_EVIDENCE_VERSION,
        "selectedDate": selected,
        "suggestedDate": selected,
        "candidates": all_candidates,
        "dedicatedCandidates": dedicated,
        "mainRegionCandidates": full_screen,
        "candidateDetails": details,
        "confidence": confidence,
        "anomalyReasons": list(dict.fromkeys(reasons)),
        "queryDateFrom": date_from,
        "queryDateTo": date_to,
        "orderNumberYYMM": expected_yymm,
    }


def retry_order_date_evidence(
    window: Image.Image,
    tesseract: Path,
    tessdata: Path,
):
    attempts = []
    for region in DATE_RETRY_REGIONS:
        text = ocr_region(
            window, region, tesseract, tessdata, normalize_output=False
        )
        attempts.append({"region": list(region), "text": text})
    return attempts


def refresh_order_date_evidence(
    order,
    path: Path,
    tesseract: Path,
    tessdata: Path,
    date_from: str | None,
    date_to: str | None,
    force: bool = False,
):
    current = order.get("dateEvidence") or {}
    if not force and current.get("version") == DATE_EVIDENCE_VERSION:
        return order
    raw = order.setdefault("rawText", {})
    main_raw = raw.get("mainRaw")
    date_raw = raw.get("dateRaw")
    with Image.open(path) as source:
        window = crop_order_window(source.convert("RGB"))
        if not main_raw or force:
            main_raw = ocr_region(
                window, REGIONS["order_main"], tesseract, tessdata,
                normalize_output=False,
            )
        if not date_raw or force:
            date_raw = ocr_region(
                window, FIELD_REGIONS["order_date"], tesseract, tessdata,
                normalize_output=False,
            )
        initial = build_date_evidence(
            date_raw, main_raw, order.get("orderNo"), date_from, date_to
        )
        retry_attempts = []
        if initial["confidence"] != "trusted":
            retry_attempts = retry_order_date_evidence(
                window, tesseract, tessdata
            )
    evidence = build_date_evidence(
        date_raw,
        main_raw,
        order.get("orderNo"),
        date_from,
        date_to,
        retry_attempts,
    )
    raw["mainRaw"] = main_raw
    raw["dateRaw"] = date_raw
    raw["dateRetry"] = retry_attempts
    order["dateEvidence"] = evidence
    if evidence["selectedDate"]:
        order["orderDate"] = evidence["selectedDate"].replace("-", ".")
    return order


def normalize_spaced_gift_codes(text: str):
    def collapse(match):
        joined = f"{match.group(1)}{match.group(2)}"
        return joined if len(joined) == 6 else match.group(0)

    normalized_lines = []
    for line in str(text or "").splitlines():
        product_start = line.find("$")
        prefix_end = min(product_start if product_start >= 0 else len(line), 32)
        prefix = line[:prefix_end]
        if not re.search(r"(?<!\d)7\d{5}(?!\d)", prefix):
            prefix = re.sub(
                r"(?<!\d)(7\d{0,4})\s+(\d{1,5})(?!\d)",
                collapse,
                prefix,
                count=1,
            )
        normalized_lines.append(prefix + line[prefix_end:])
    return "\n".join(normalized_lines)


def parse_detail_pass(text: str):
    lines = [normalize_text(line.strip()) for line in text.splitlines() if line.strip()]
    rows = []
    for line in lines:
        line = re.sub(r"(?<=\d)\s*,\s*(?=\d{3}\b)", ",", line)
        line = re.sub(r"(?<=\d)\s*\.\s*00\b", ".00", line)
        line = normalize_spaced_gift_codes(line)
        if not re.search(r"(?<!\d)7\d{5}(?!\d)", line):
            continue
        for match in re.finditer(r"(?<!\d)(7\d{5})(?!\d)", line):
            tail = line[match.end() :]
            # Tesseract occasionally reads a final digit 1 as ] or } in a
            # numeric cell (for example quantity 41 -> 4]). Repair only a
            # standalone numeric token; arithmetic validation still decides
            # whether the repaired value is usable.
            tail = re.sub(
                r"(?<!\S)(\d{1,5})[\]\}](?=\s|$)",
                lambda value: f"{value.group(1)}1",
                tail,
            )
            numbers = []
            for raw_number in re.findall(r"\d[\d,]*(?:\.\d+)?", tail):
                cleaned = raw_number.replace(",", "")
                if not cleaned:
                    continue
                numbers.append(int(float(cleaned)))
            if len(numbers) < 3:
                continue
            best = None
            for i in range(len(numbers) - 3):
                quantity, list_price, total = numbers[i : i + 3]
                if quantity <= 0 or list_price <= 0:
                    continue
                if quantity * list_price != total:
                    continue
                discount_amount = None
                if i + 4 < len(numbers):
                    candidate = numbers[i + 4]
                    if 0 <= candidate <= total:
                        discount_amount = candidate
                if discount_amount is None and i + 3 < len(numbers):
                    discount_unit = numbers[i + 3]
                    if 0 <= discount_unit <= list_price:
                        discount_amount = discount_unit * quantity
                if discount_amount is None:
                    discount_amount = total
                best = {
                    "giftCode": match.group(1),
                    "quantity": quantity,
                    "listPrice": list_price,
                    "totalAmount": total,
                    "discountAmount": discount_amount,
                }
                break
            if best is None:
                # Recover a damaged quantity cell only when adjacent price and
                # amount values imply an exact positive integer quantity.
                for i in range(len(numbers) - 1):
                    list_price, total = numbers[i : i + 2]
                    if list_price < 10 or total < list_price:
                        continue
                    if total % list_price != 0:
                        continue
                    quantity = total // list_price
                    if quantity <= 0 or quantity > 100000:
                        continue
                    discount_amount = None
                    if i + 3 < len(numbers):
                        candidate = numbers[i + 3]
                        if 0 <= candidate <= total:
                            discount_amount = candidate
                    if discount_amount is None and i + 2 < len(numbers):
                        discount_unit = numbers[i + 2]
                        if 0 <= discount_unit <= list_price:
                            discount_amount = discount_unit * quantity
                    if discount_amount is None:
                        discount_amount = total
                    best = {
                        "giftCode": match.group(1),
                        "quantity": quantity,
                        "listPrice": list_price,
                        "totalAmount": total,
                        "discountAmount": discount_amount,
                    }
                    break
            if best:
                rows.append(best)
    unique = []
    seen = set()
    for row in rows:
        key = (row["giftCode"], row["quantity"], row["listPrice"], row["totalAmount"], row["discountAmount"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def parse_detail_rows(text: str):
    passes = [value for value in str(text or "").split(OCR_PASS_SEPARATOR) if value.strip()]
    merged = {}
    order = []
    for value in passes:
        for row in parse_detail_pass(value):
            code = row.get("giftCode")
            if not code:
                continue
            if code not in merged:
                order.append(code)
                merged[code] = row
                continue
            current = merged[code]
            current_score = sum(current.get(key) not in (None, "") for key in current)
            new_score = sum(row.get(key) not in (None, "") for key in row)
            if new_score > current_score:
                merged[code] = row
    return [merged[code] for code in order]


def parse_summary_totals(text: str):
    candidates = []
    for value in str(text or "").split(OCR_PASS_SEPARATOR):
        normalized = normalize_digits(value)
        normalized = re.sub(r"(?<=\d)\s*,\s*(?=\d{3}\b)", ",", normalized)
        numbers = []
        for raw_number in re.findall(r"\d[\d,]*", normalized):
            cleaned = raw_number.replace(",", "")
            if cleaned:
                numbers.append(int(cleaned))
        for index in range(max(0, len(numbers) - 5), len(numbers) - 2):
            quantity, total_amount, discount_amount = numbers[index : index + 3]
            if quantity > 0 and total_amount >= discount_amount >= 0 and total_amount >= quantity:
                candidates.append(
                    {
                        "quantity": quantity,
                        "totalAmount": total_amount,
                        "discountAmount": discount_amount,
                    }
                )
        if len(numbers) >= 2:
            total_amount, discount_amount = numbers[-2:]
            if total_amount >= discount_amount >= 0:
                candidates.append(
                    {
                        "quantity": None,
                        "totalAmount": total_amount,
                        "discountAmount": discount_amount,
                    }
                )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item["quantity"] is not None, item["totalAmount"]),
    )


def detail_code_column_visible(text: str) -> bool:
    raw = str(text or "")
    normalized = normalize_spaced_gift_codes(normalize_text(raw))
    return bool(
        re.search(r"(?<!\d)7\d{5}(?!\d)", normalized)
        or re.search(r"禮.{0,4}(?:代號|代碼)", raw)
    )


def image_signature(path: Path):
    stat = path.stat()
    return {"imageSize": stat.st_size, "imageMtime": stat.st_mtime}


def load_previous(cache_path: Path):
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {item.get("sourcePath"): item for item in data.get("orders", []) if item.get("sourcePath")}


def ensure_manual_table(path: Path):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANUAL_HEADERS)
        writer.writeheader()


def load_manual_rows(path: Path):
    ensure_manual_table(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def is_enabled(value):
    text = str(value or "").strip().lower()
    return text not in {"0", "false", "no", "n", "否", "停用"}


def normalize_category(value):
    text = str(value or "").strip()
    if text in CATEGORY_LABELS:
        return CATEGORY_LABELS[text]
    return text


def normalize_order_no(value):
    if not value:
        return None
    return clean_order_no(value)


def correction_matches(order, row):
    image_name = str(row.get("圖片") or "").strip()
    order_no = normalize_order_no(row.get("訂單編號"))
    if image_name and image_name == order.get("imageName"):
        return True
    if order_no and order_no == order.get("orderNo"):
        return True
    return False


def detail_from_correction(row):
    gift = only_digits(row.get("禮盒代號"))
    if not gift:
        return None
    detail = {
        "giftCode": gift,
        "listPrice": to_money(row.get("定價")),
        "quantity": to_int(row.get("數量")),
        "totalAmount": to_money(row.get("總金額")),
        "discountAmount": to_money(row.get("折後")),
    }
    if detail["discountAmount"] is None and detail["totalAmount"] is not None:
        detail["discountAmount"] = detail["totalAmount"]
    return detail


def apply_manual_corrections(orders, rows):
    notes = []
    for raw in rows:
        if not is_enabled(raw.get("啟用")):
            continue
        action = str(raw.get("動作") or "upsert").strip().lower() or "upsert"
        matches = [order for order in orders if correction_matches(order, raw)]
        if not matches:
            notes.append(f"找不到修正目標：圖片={raw.get('圖片') or ''} 訂單={raw.get('訂單編號') or ''}")
            continue
        for order in matches:
            order.setdefault("manualCorrections", [])
            if raw.get("訂單編號"):
                order["orderNo"] = normalize_order_no(raw.get("訂單編號"))
            if raw.get("訂單分類"):
                order["category"] = normalize_category(raw.get("訂單分類"))
            if raw.get("日期"):
                order["orderDate"] = normalize_date(raw.get("日期")) or str(raw.get("日期")).strip()
            if action in {"skip", "skip_order", "exclude", "略過"}:
                order["manualExcluded"] = True
                order["manualCorrections"].append(raw.get("備註") or "人工略過")
                continue
            detail = detail_from_correction(raw)
            if not detail:
                order["manualCorrections"].append(raw.get("備註") or "人工修正主檔")
                continue
            gift_code = detail["giftCode"]
            if action in {"delete", "remove", "刪除"}:
                order["details"] = [item for item in order.get("details", []) if item.get("giftCode") != gift_code]
                order["manualCorrections"].append(raw.get("備註") or f"人工刪除 {gift_code}")
                continue
            order["details"] = [item for item in order.get("details", []) if item.get("giftCode") != gift_code]
            order.setdefault("details", []).append(detail)
            order["manualCorrections"].append(raw.get("備註") or f"人工修正 {gift_code}")
    return notes


def validate_order(order):
    reasons = []
    failed = False

    if order.get("manualExcluded"):
        order["included"] = False
        order["statusLabel"] = "成功"
        order["statusLevel"] = "success"
        order["errorReason"] = "人工設定略過"
        return order

    if order.get("voided"):
        order["included"] = False
        order["statusLabel"] = "成功"
        order["statusLevel"] = "success"
        order["errorReason"] = "作廢訂單，依規則不納入Excel"
        return order

    department = order.get("departmentCode")
    if not department:
        reasons.append("部門代號未辨識")
    elif department != TARGET_DEPARTMENT:
        order["included"] = False
        order["statusLabel"] = "成功"
        order["statusLevel"] = "success"
        order["errorReason"] = f"非{TARGET_DEPARTMENT}，依規則不納入Excel"
        return order

    if not order.get("orderNo"):
        reasons.append("訂單編號未辨識")
    if not order.get("category"):
        reasons.append("訂單分類未辨識")
    if not order.get("orderDate"):
        reasons.append("訂貨日期未辨識")
    date_evidence = order.get("dateEvidence")
    if isinstance(date_evidence, dict):
        reasons.extend(date_evidence.get("anomalyReasons") or [])

    details = order.get("details") or []
    if not details:
        if order.get("detailCodeColumnVisible") is False:
            reasons.append("禮盒代號欄未顯示，請將明細水平捲軸拉到最左")
        else:
            reasons.append("訂貨明細未辨識")
    for detail in details:
        missing = [key for key in ("giftCode", "listPrice", "quantity", "totalAmount", "discountAmount") if detail.get(key) in (None, "")]
        if missing:
            reasons.append(f"{detail.get('giftCode') or '明細'} 欄位不完整")
            continue
        if detail["quantity"] * detail["listPrice"] != detail["totalAmount"]:
            reasons.append(f"{detail['giftCode']} 數量x定價不等於總金額")
        if detail["discountAmount"] > detail["totalAmount"]:
            reasons.append(f"{detail['giftCode']} 折後大於總金額")

    summary_totals = order.get("summaryTotals")
    if details and summary_totals:
        actual_totals = {
            "quantity": sum(detail.get("quantity") or 0 for detail in details),
            "totalAmount": sum(detail.get("totalAmount") or 0 for detail in details),
            "discountAmount": sum(detail.get("discountAmount") or 0 for detail in details),
        }
        mismatched = [
            label
            for key, label in (
                ("quantity", "數量"),
                ("totalAmount", "總金額"),
                ("discountAmount", "折後"),
            )
            if summary_totals.get(key) is not None and actual_totals[key] != summary_totals[key]
        ]
        if mismatched:
            reasons.append(f"明細加總與畫面合計不符：{'、'.join(mismatched)}")

    if order.get("ocrError"):
        failed = True
        reasons.append(order["ocrError"])

    order["included"] = not reasons
    order["statusLabel"] = "失敗" if failed else ("待確認" if reasons else "成功")
    order["statusLevel"] = "failed" if failed else ("pending" if reasons else "success")
    if order.get("manualCorrections"):
        reasons.append("已套用人工修正")
    order["errorReason"] = "；".join(dict.fromkeys(reasons))
    return order


def extract_image(
    path: Path,
    tesseract: Path,
    tessdata: Path,
    date_from: str | None = None,
    date_to: str | None = None,
):
    signature = image_signature(path)
    order = {
        "imageName": path.name,
        "sourcePath": str(path),
        **signature,
        "departmentCode": None,
        "orderNo": None,
        "category": "",
        "orderDate": None,
        "details": [],
        "summaryTotals": None,
        "layout": None,
        "detailCodeColumnVisible": None,
        "voided": False,
        "rawText": {},
    }
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            order["voided"] = detect_void_stamp(image)
            layout = "maximized"
            window = crop_order_window(image)
            regions = REGIONS
            field_regions = FIELD_REGIONS
            main_raw = ocr_region(
                window, regions["order_main"], tesseract, tessdata,
                normalize_output=False,
            )
            date_raw = ocr_region(
                window, field_regions["order_date"], tesseract, tessdata,
                normalize_output=False,
            )
            main_text = normalize_text(main_raw)
            date_text = normalize_text(date_raw)
            department_text = ocr_region(window, field_regions["department"], tesseract, tessdata)
            main_text = "\n".join([main_text, date_text, department_text])
            detail_text = ocr_region(image, FULL_SCREEN_DETAIL_REGION, tesseract, tessdata)
            totals_text = ocr_region(image, FULL_SCREEN_TOTALS_REGION, tesseract, tessdata)
            all_text = "\n".join([main_text, detail_text, totals_text])
            order_no = infer_order_no(main_text)
            date_evidence = build_date_evidence(
                date_raw,
                main_raw,
                order_no,
                date_from,
                date_to,
            )
            retry_attempts = []
            if date_evidence["confidence"] != "trusted":
                retry_attempts = retry_order_date_evidence(
                    window, tesseract, tessdata
                )
                date_evidence = build_date_evidence(
                    date_raw,
                    main_raw,
                    order_no,
                    date_from,
                    date_to,
                    retry_attempts,
                )
            order.update(
                {
                    "departmentCode": infer_department_field(department_text, main_text),
                    "orderNo": order_no,
                    "category": infer_category(main_text),
                    "orderDate": (
                        date_evidence["selectedDate"].replace("-", ".")
                        if date_evidence["selectedDate"] else None
                    ),
                    "dateEvidence": date_evidence,
                    "details": parse_detail_rows(detail_text),
                    "summaryTotals": parse_summary_totals(totals_text),
                    "layout": layout,
                    "detailCodeColumnVisible": detail_code_column_visible(detail_text),
                    "rawText": {
                        "main": main_text,
                        "date": date_text,
                        "mainRaw": main_raw,
                        "dateRaw": date_raw,
                        "dateRetry": retry_attempts,
                        "department": department_text,
                        "details": detail_text,
                        "totals": totals_text,
                        "all": all_text,
                        "layout": layout,
                    },
                }
            )
    except Exception as exc:
        order["ocrError"] = f"OCR失敗：{exc}"
    return order


def reparse_previous(path: Path, previous):
    raw = previous.get("rawText") or {}
    main_text = normalize_text(raw.get("main") or raw.get("all") or "")
    department_text = normalize_text(raw.get("department") or "")
    detail_text = normalize_text(raw.get("details") or raw.get("all") or "")
    totals_text = normalize_text(raw.get("totals") or "")
    order = {
        "imageName": path.name,
        "sourcePath": str(path),
        **image_signature(path),
        "departmentCode": infer_department_field(department_text or main_text, main_text),
        "orderNo": infer_order_no(main_text),
        "category": infer_category(main_text),
        "orderDate": infer_date(main_text),
        "details": parse_detail_rows(detail_text),
        "summaryTotals": parse_summary_totals(totals_text) or previous.get("summaryTotals"),
        "layout": previous.get("layout") or raw.get("layout") or "windowed",
        "detailCodeColumnVisible": previous.get(
            "detailCodeColumnVisible", detail_code_column_visible(detail_text)
        ),
        "voided": bool(previous.get("voided")),
        "rawText": raw,
    }
    if previous.get("ocrError"):
        order["ocrError"] = previous["ocrError"]
    return order


def can_reparse_from_raw(path: Path):
    try:
        with Image.open(path) as image:
            return image.width >= 1400
    except Exception:
        return False


def is_cache_hit(previous, path: Path):
    if not previous:
        return False
    sig = image_signature(path)
    previous_version = int(previous.get("extractorVersion") or 0)
    raw = previous.get("rawText") or {}
    raw_details = str(raw.get("details") or raw.get("all") or "")
    pending_needs_v20 = (
        previous.get("statusLabel") == "待確認"
        and (
            bool(re.search(r"(?<!\d)7\d{0,4}\s+\d{1,5}(?!\d)", raw_details))
            or (
                "部門代號未辨識" in str(previous.get("errorReason") or "")
                and bool(re.search(r"(?<!\d)4010(?!\d)", str(raw.get("department") or "")))
            )
        )
    )
    legacy_compatible = (
        previous_version == 21
        and (
            previous.get("statusLabel") == "成功"
            or (
                previous.get("statusLabel") == "待確認"
                and previous.get("detailCodeColumnVisible") is False
            )
        )
    ) or (
        previous_version == 20
        and (
            previous.get("statusLabel") == "成功"
            or (
                previous.get("statusLabel") == "待確認"
                and previous.get("detailCodeColumnVisible") is False
            )
        )
    ) or (
        previous_version == 19
        and previous.get("statusLabel") in {"成功", "待確認"}
        and not pending_needs_v20
    ) or (
        previous_version == 18 and previous.get("statusLabel") in {"成功", "待確認"}
    ) or (previous_version in {16, 17} and (
        (previous.get("statusLabel") == "成功" and len(previous.get("details") or []) < 4)
        or (
            previous.get("statusLabel") == "待確認"
            and previous.get("detailCodeColumnVisible") is False
        )
    ))
    version_compatible = (
        previous_version == EXTRACTOR_VERSION
        or legacy_compatible
    )
    return (
        version_compatible
        and previous.get("imageSize") == sig["imageSize"]
        and previous.get("imageMtime") == sig["imageMtime"]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--tesseract", required=True)
    parser.add_argument("--tessdata", required=True)
    parser.add_argument("--input-pattern", default="*")
    parser.add_argument("--progress-file")
    parser.add_argument("--stop-file")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    args = parser.parse_args()

    if bool(args.date_from) != bool(args.date_to):
        parser.error("--date-from and --date-to must be provided together")
    if args.date_from and not date_in_query_range(
        args.date_from, args.date_from, args.date_to
    ):
        parser.error("invalid OCR query date range")

    root = Path(args.root)
    input_dir = root / "input_images"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "orders.json"
    manual_path = data_dir / "manual_corrections.csv"

    previous_by_path = load_previous(output_path)
    image_paths = sorted(
        path
        for path in input_dir.glob(args.input_pattern)
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    orders = []
    processed = 0
    cached = 0
    stopped = False
    for index, path in enumerate(image_paths, start=1):
        if args.stop_file and Path(args.stop_file).exists():
            stopped = True
            break
        prev = previous_by_path.get(str(path))
        signature = image_signature(path)
        voided = image_has_void_stamp(path)
        unchanged = bool(
            prev
            and prev.get("imageSize") == signature["imageSize"]
            and prev.get("imageMtime") == signature["imageMtime"]
        )
        if voided and unchanged:
            # The stamp is decisive, so a cached order does not need another
            # slow OCR pass just to be excluded from the workbook.
            order = dict(prev)
            cached += 1
        elif prev and is_cache_hit(prev, path):
            order = dict(prev)
            cached += 1
        elif (
            prev
            and int(prev.get("extractorVersion") or 0) in {21, 22, EXTRACTOR_VERSION}
            and prev.get("rawText")
            and prev.get("imageSize") == image_signature(path)["imageSize"]
            and can_reparse_from_raw(path)
        ):
            order = reparse_previous(path, prev)
            processed += 1
        else:
            order = extract_image(
                path,
                Path(args.tesseract),
                Path(args.tessdata),
                args.date_from,
                args.date_to,
            )
            processed += 1
        order = refresh_order_date_evidence(
            order,
            path,
            Path(args.tesseract),
            Path(args.tessdata),
            args.date_from,
            args.date_to,
        )
        order["sourcePath"] = str(path)
        order["imageName"] = path.name
        order["extractorVersion"] = EXTRACTOR_VERSION
        order.update(signature)
        order["voided"] = voided
        order.pop("included", None)
        order.pop("statusLabel", None)
        order.pop("statusLevel", None)
        order.pop("errorReason", None)
        orders.append(order)
        if args.progress_file:
            progress_path = Path(args.progress_file)
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            replace_text_with_retry(
                progress_path,
                json.dumps(
                    {
                        "current": index,
                        "total": len(image_paths),
                        "image": path.name,
                        "processed": processed,
                        "cached": cached,
                        "stopped": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

    manual_rows = load_manual_rows(manual_path)
    correction_notes = apply_manual_corrections(orders, manual_rows)
    orders = [validate_order(order) for order in orders]
    included_rows = sum(len(order.get("details") or []) for order in orders if order.get("included"))

    payload = {
        "version": 2,
        "targetDepartment": TARGET_DEPARTMENT,
        "orders": orders,
        "manualCorrectionFile": str(manual_path),
        "manualCorrectionNotes": correction_notes,
        "summary": {
            "images": len(image_paths),
            "processed": processed,
            "cached": cached,
            "includedDetailRows": included_rows,
            "pending": sum(1 for order in orders if order.get("statusLabel") == "待確認"),
            "failed": sum(1 for order in orders if order.get("statusLabel") == "失敗"),
            "completedImages": len(orders),
            "completed": not stopped and len(orders) == len(image_paths),
            "stopped": stopped,
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Images: {len(image_paths)}")
    print(f"OCR processed: {processed}")
    print(f"Cached: {cached}")
    print(f"Included detail rows: {included_rows}")
    print(f"Pending: {payload['summary']['pending']}")
    print(f"Failed: {payload['summary']['failed']}")
    print(f"Saved: {output_path}")
    if stopped:
        if args.progress_file:
            replace_text_with_retry(
                Path(args.progress_file),
                json.dumps(
                    {
                        "current": len(orders),
                        "total": len(image_paths),
                        "processed": processed,
                        "cached": cached,
                        "stopped": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
