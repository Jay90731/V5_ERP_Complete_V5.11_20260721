from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-images", type=int)
    parser.add_argument("--source-pattern", default=r"^GOU[0-9A-Z-]+\.png$")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    pattern = re.compile(args.source_pattern, re.IGNORECASE)
    orders = [
        order
        for order in payload.get("orders") or []
        if pattern.search(str(order.get("imageName") or ""))
    ]
    image_names = [str(order.get("imageName") or "") for order in orders]
    duplicate_images = sorted({name for name in image_names if image_names.count(name) > 1})
    source_summary = payload.get("summary") or {}
    failed = int(source_summary.get("failed") or 0)
    completed_flag = source_summary.get("completed")
    completed_images = int(source_summary.get("completedImages") or len(orders))
    errors: list[str] = []

    if not orders:
        errors.append("OCR output contains no GOU orders")
    if duplicate_images:
        errors.append(f"Duplicate OCR image records: {','.join(duplicate_images[:10])}")
    if args.expected_images is not None and len(orders) != args.expected_images:
        errors.append(f"OCR order count {len(orders)} does not match screenshot count {args.expected_images}")
    if args.expected_images is not None and completed_images != args.expected_images:
        errors.append(f"OCR completed image count {completed_images} does not match screenshot count {args.expected_images}")
    if completed_flag is False:
        errors.append("OCR stage did not complete")
    if failed > 0:
        errors.append(f"OCR contains {failed} failed records")

    department_4001 = 0
    non_4001 = 0
    unknown_department = 0
    pending = 0
    for order in orders:
        department = str(order.get("departmentCode") or "").strip()
        if department == "4001":
            department_4001 += 1
        elif department:
            non_4001 += 1
        else:
            unknown_department += 1
        if str(order.get("statusLevel") or "").lower() != "success":
            pending += 1

    result = {
        "valid": not errors,
        "total_orders": len(orders),
        "unique_images": len(set(image_names)),
        "department_4001": department_4001,
        "non_4001": non_4001,
        "unknown_department": unknown_department,
        "pending": pending,
        "failed": failed,
        "errors": errors,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
