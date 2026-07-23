from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "components" / "incremental"))

from incremental_engine import process_orders


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-pattern", default=r"^GOU[0-9A-Z-]+\.png$")
    parser.add_argument("--run-at")
    args = parser.parse_args()

    input_path = Path(args.input)
    history_path = Path(args.history)
    if not history_path.exists():
        raise FileNotFoundError("Official order_history.json is missing; V5 cannot compare safely")

    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    history = json.loads(history_path.read_text(encoding="utf-8-sig"))
    run_at = args.run_at or datetime.now().astimezone().isoformat(timespec="seconds")
    current, _, events, summary = process_orders(
        payload=payload,
        history=history,
        initialize_baseline=False,
        run_at=run_at,
        source_image_pattern=args.source_pattern,
        target_department="4001",
    )
    result = {
        "summary": summary,
        "change_events": events,
        "current_status_counts": {},
    }
    for row in current.get("orders") or []:
        status = row.get("status") or ""
        result["current_status_counts"][status] = result["current_status_counts"].get(status, 0) + 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
