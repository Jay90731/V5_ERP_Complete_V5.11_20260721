from __future__ import annotations

import copy
import json
from pathlib import Path


def detail(code: str, price: int, quantity: int, discount: int | None = None):
    total = price * quantity
    return {
        "gift_code": code,
        "list_price": price,
        "quantity": quantity,
        "total_amount": total,
        "discount_amount": total if discount is None else discount,
    }


def order(
    order_no: str,
    order_date: str,
    department: str,
    details: list[dict],
    category: str = "婚約",
):
    return {
        "order_no": order_no,
        "category": category,
        "order_date": order_date,
        "department_code": department,
        "detail_count": len(details),
        "details": details,
        "voided": False,
        "excel_eligible": department == "4001",
        "source": {"image_name": f"{order_no}.png", "status": "success"},
    }


def history_entry(payload: dict, version_id: str = "source-v1"):
    return {
        "current_version_id": version_id,
        "current_version": copy.deepcopy(payload),
        "versions": [
            {
                "version_id": version_id,
                "effective_at": "2026-07-16T08:00:00+08:00",
                "change_type": "基準建立",
                "order": copy.deepcopy(payload),
            }
        ],
        "created_at": "2026-07-16T08:00:00+08:00",
        "updated_at": "2026-07-16T08:00:00+08:00",
    }


def write_fixture(root: Path):
    state = root / "order_incremental_engine" / "state"
    state.mkdir(parents=True)
    first = order(
        "GOU26070001",
        "2026-07-01",
        "4001",
        [detail("700001", 900, 2, 1700), detail("700002", 600, 3, 1500)],
    )
    second = order(
        "GOU26070002",
        "2026-07-02",
        "4100",
        [detail("700003", 500, 4, 1800)],
        category="其他",
    )
    history = {
        "schema_version": 1,
        "orders": {
            first["order_no"]: history_entry(first),
            second["order_no"]: history_entry(second),
        },
    }
    pending = {
        "order_no": "GOU26070003",
        "status": "待確認",
        "reason": "測試待確認",
        "source_images": ["GOU26070003.png"],
        "order": {
            "order_no": "GOU26070003",
            "department_code": "4001",
            "order_date": "2026-07-03",
        },
    }
    current = {"schema_version": 1, "orders": [pending]}
    (state / "order_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (state / "current_orders.json").write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return state


def load_history(state: Path):
    return json.loads((state / "order_history.json").read_text(encoding="utf-8"))


def save_history(state: Path, value):
    (state / "order_history.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )

