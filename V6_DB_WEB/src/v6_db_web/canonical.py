from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_order_no(value: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(value).upper())


def normalize_date(value: Any) -> str:
    text = normalize_text(value)
    digits = re.sub(r"\D", "", text)
    candidates = [text]
    if len(digits) == 8:
        candidates.append(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
    for candidate in candidates:
        normalized = re.sub(r"[./]", "-", candidate)
        try:
            return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Invalid order date: {value!r}")


def normalize_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        return int(value)
    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()
    text = text.replace(",", "").replace("$", "").replace("NT", "")
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ValueError(f"{field_name} is empty")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not numeric: {value!r}") from exc
    if number != number.to_integral_value():
        raise ValueError(f"{field_name} is not an integer: {value!r}")
    return int(number)


def normalize_detail(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "gift_code": normalize_text(raw.get("gift_code", raw.get("giftCode"))),
        "list_price": normalize_integer(
            raw.get("list_price", raw.get("listPrice")), "list_price"
        ),
        "quantity": normalize_integer(raw.get("quantity"), "quantity"),
        "total_amount": normalize_integer(
            raw.get("total_amount", raw.get("totalAmount")), "total_amount"
        ),
        "discount_amount": normalize_integer(
            raw.get("discount_amount", raw.get("discountAmount")),
            "discount_amount",
        ),
    }


def normalize_order(raw: dict[str, Any]) -> dict[str, Any]:
    order_no = normalize_order_no(raw.get("order_no", raw.get("orderNo")))
    if not order_no:
        raise ValueError("order_no is empty")
    details = [normalize_detail(item) for item in raw.get("details", [])]
    return {
        "order_no": order_no,
        "order_date": normalize_date(raw.get("order_date", raw.get("orderDate"))),
        "category": normalize_text(raw.get("category")),
        "department_code": normalize_text(
            raw.get("department_code", raw.get("departmentCode"))
        ),
        "is_voided": bool(raw.get("voided", raw.get("is_voided", False))),
        "details": details,
    }


def canonical_content(order: dict[str, Any]) -> dict[str, Any]:
    sortable_details = sorted(
        order["details"],
        key=lambda item: (
            item["gift_code"],
            item["list_price"],
            item["quantity"],
            item["total_amount"],
            item["discount_amount"],
        ),
    )
    return {
        "order_date": order["order_date"],
        "category": order["category"],
        "department_code": order["department_code"],
        "is_voided": order["is_voided"],
        "details": sortable_details,
    }


def content_hash(order: dict[str, Any]) -> str:
    payload = json.dumps(
        canonical_content(order),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def item_hash(item: dict[str, Any]) -> str:
    payload = json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

