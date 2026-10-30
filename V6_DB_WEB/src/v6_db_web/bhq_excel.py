from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SHEET_NAME = "REPORT"
EXPECTED_HEADERS = (
    "訂貨日期",
    "訂單編號",
    "類型",
    "交貨日期",
    "客戶名稱",
    "訂金",
    "總金額",
    "產品代碼",
    "產品名稱",
    "數量",
    "來源別",
    "交貨方式",
)

# Stable BHQ codes taken from the actual 2026-07 report.  They are deliberately
# explicit: an unknown source must be reviewed instead of being guessed.
BHQ_STORE_CODES = {
    "安和店Anhe": "ANHE",
    "忠孝ZX": "ZX",
    "新光南西NH": "NH",
    "高島屋TAK": "TAK",
    "漢神HS": "HS",
    "中壢CL": "CL",
    "新光台南TN": "TN",
    "新光台中TC": "TC",
    "新光A4": "A4",
    "復興BR4": "BR4",
    "巨蛋DOME": "DOME",
    "高雄KS": "KS",
    "天母TM": "TM",
    "板橋遠百BCFE": "BCFE",
    "新竹巨城HCBC": "HCBC",
    "台中遠百TCFE": "TCFE",
    "微風南京BN": "BN",
    "新光天母TMMK": "TMMK",
}


class BhqValidationError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalized_date(value: Any, field: str, row_no: int) -> str:
    raw = text(value)
    for pattern in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    raise BhqValidationError(f"第 {row_no} 列 {field} 無效：{raw!r}")


def integer_value(value: Any, field: str, row_no: int) -> int:
    raw = text(value).replace(",", "")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise BhqValidationError(f"第 {row_no} 列 {field} 無效：{raw!r}") from exc
    if not number.is_finite():
        raise BhqValidationError(f"第 {row_no} 列 {field} 無效：{raw!r}")
    integral = number.to_integral_value()
    if number != integral:
        raise BhqValidationError(f"第 {row_no} 列 {field} 必須是整數：{raw!r}")
    return int(integral)


def parse_bhq_xls(path: str | Path) -> dict[str, Any]:
    try:
        import xlrd
    except ImportError as exc:
        raise RuntimeError("解析 BHQ .xls 需要 xlrd") from exc

    source = Path(path).resolve()
    # Fully load the small export so Windows does not keep the source .xls open
    # after parsing.  The watcher must never prevent BHQ from replacing or
    # archiving a completed export.
    workbook = xlrd.open_workbook(source, on_demand=False)
    if SHEET_NAME not in workbook.sheet_names():
        raise BhqValidationError(f"找不到工作表 {SHEET_NAME}")
    sheet = workbook.sheet_by_name(SHEET_NAME)
    headers = tuple(text(sheet.cell_value(1, col)) for col in range(sheet.ncols))
    if headers != EXPECTED_HEADERS:
        raise BhqValidationError(
            f"REPORT 第 2 列欄位不符：expected={EXPECTED_HEADERS!r}, actual={headers!r}"
        )

    orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
    empty_product_code_rows: list[int] = []
    source_row_count = 0
    anomalies: list[dict[str, Any]] = []

    for row_index in range(2, sheet.nrows):
        values = [sheet.cell_value(row_index, col) for col in range(sheet.ncols)]
        if not any(text(value) for value in values):
            continue
        row_no = row_index + 1
        source_row_count += 1
        order_no = text(values[1])
        if not order_no:
            anomalies.append({"row": row_no, "reason": "訂單編號空白"})
            continue
        source_store_name = text(values[10])
        store_code = BHQ_STORE_CODES.get(source_store_name)
        if store_code is None:
            anomalies.append(
                {"row": row_no, "order_no": order_no, "reason": f"未知門市：{source_store_name}"}
            )

        header = {
            "order_date": normalized_date(values[0], "訂貨日期", row_no),
            "order_no": order_no,
            "category": text(values[2]),
            "discounted_amount": integer_value(values[6], "總金額", row_no),
            "source_store_name": source_store_name,
            "store_code": store_code,
            "source_system": "BHQ",
        }
        order = orders.get(order_no)
        if order is None:
            order = {**header, "source_rows": [], "items": []}
            orders[order_no] = order
        else:
            inconsistent = [
                key
                for key in ("order_date", "category", "discounted_amount", "source_store_name")
                if order[key] != header[key]
            ]
            if inconsistent:
                anomalies.append(
                    {
                        "row": row_no,
                        "order_no": order_no,
                        "reason": "同訂單欄位不一致：" + ", ".join(inconsistent),
                    }
                )
        order["source_rows"].append(row_no)

        product_code = text(values[7])
        if not product_code:
            empty_product_code_rows.append(row_no)
            continue
        quantity = integer_value(values[9], "數量", row_no)
        order["items"].append(
            {
                "line_no": len(order["items"]) + 1,
                "source_row": row_no,
                "product_code": product_code,
                "product_name": text(values[8]),
                "quantity": quantity,
            }
        )

    invalid_orders = {item.get("order_no") for item in anomalies if item.get("order_no")}
    store_counts: dict[str, dict[str, Any]] = {}
    for order in orders.values():
        entry = store_counts.setdefault(
            order["source_store_name"],
            {"store_code": order["store_code"], "row_count": 0, "order_count": 0},
        )
        entry["row_count"] += len(order["source_rows"])
        entry["order_count"] += 1

    duplicate_product_rows = sum(
        len(order["items"])
        - len({item["product_code"] for item in order["items"]})
        for order in orders.values()
    )
    result = {
        "source_file": str(source),
        "source_filename": source.name,
        "file_sha256": file_sha256(source),
        "sheet_name": SHEET_NAME,
        "source_row_count": source_row_count,
        "order_count": len(orders),
        "valid_item_count": sum(len(order["items"]) for order in orders.values()),
        "empty_product_code_count": len(empty_product_code_rows),
        "empty_product_code_rows": empty_product_code_rows,
        "store_count": len(store_counts),
        "stores": store_counts,
        "duplicate_product_code_row_count": duplicate_product_rows,
        "anomaly_count": len(anomalies),
        "invalid_order_count": len(invalid_orders),
        "anomalies": anomalies,
        "orders": list(orders.values()),
    }
    workbook.release_resources()
    return result


def dry_run_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "orders"}


def write_dry_run_report(result: dict[str, Any], output: str | Path) -> Path:
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return destination
