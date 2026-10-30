from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
OCR_COMPONENT = ROOT / "components" / "ocr"
if str(OCR_COMPONENT) not in sys.path:
    sys.path.insert(0, str(OCR_COMPONENT))

from extract_orders import (  # noqa: E402
    DATE_EVIDENCE_VERSION,
    EXTRACTOR_VERSION,
    build_date_evidence,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reprocess R4 date evidence from stored raw OCR candidates"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--image-dir", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    image_dir = Path(args.image_dir).resolve()
    report_path = Path(args.report).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    records = payload.get("orders")
    if not isinstance(records, list):
        raise ValueError("OCR JSON does not contain an orders array")

    seen_names: set[str] = set()
    seen_hashes: set[str] = set()
    reason_counts: Counter[str] = Counter()
    target_review = 0
    missing_raw = []
    image_errors = []

    for index, record in enumerate(records, start=1):
        image_name = str(record.get("imageName") or "").strip()
        image_path = image_dir / image_name
        if not image_name or not image_path.is_file():
            image_errors.append(f"{image_name or f'row_{index}'}: missing")
            continue
        if image_name in seen_names:
            image_errors.append(f"{image_name}: duplicate filename")
            continue
        seen_names.add(image_name)
        digest = sha256_file(image_path)
        if digest in seen_hashes:
            image_errors.append(f"{image_name}: duplicate SHA-256")
            continue
        seen_hashes.add(digest)
        try:
            with Image.open(image_path) as image:
                image.verify()
        except Exception as exc:  # pragma: no cover - real-file diagnostic
            image_errors.append(f"{image_name}: unreadable ({exc})")
            continue

        raw = record.get("rawText")
        if not isinstance(raw, dict):
            raw = {}
        date_raw = raw.get("dateRaw") or raw.get("date")
        main_raw = raw.get("mainRaw") or raw.get("main")
        if not date_raw or not main_raw:
            missing_raw.append(image_name)
        evidence = build_date_evidence(
            date_raw,
            main_raw,
            record.get("orderNo") or Path(image_name).stem,
            args.date_from,
            args.date_to,
        )
        evidence["sourceImageSha256"] = digest
        evidence["reprocessedFromStoredRawOcr"] = True
        record["dateEvidence"] = evidence
        record["extractorVersion"] = EXTRACTOR_VERSION
        if evidence.get("selectedDate"):
            record["orderDate"] = evidence["selectedDate"].replace("-", ".")
        reason_counts.update(evidence.get("anomalyReasons") or [])
        if (
            str(record.get("departmentCode") or "").strip() == "4001"
            and evidence.get("anomalyReasons")
        ):
            target_review += 1

    if image_errors:
        raise RuntimeError(
            "Image verification failed: " + "; ".join(image_errors[:20])
        )
    if missing_raw:
        raise RuntimeError(
            "Stored raw OCR candidates missing for: " + ", ".join(missing_raw[:20])
        )

    payload["version"] = max(int(payload.get("version") or 0), 4)
    payload["r4DateReprocessing"] = {
        "processedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sourceFile": str(input_path),
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
        "dateEvidenceVersion": DATE_EVIDENCE_VERSION,
        "extractorVersion": EXTRACTOR_VERSION,
        "recordCount": len(records),
        "imageCount": len(seen_names),
        "uniqueImageHashes": len(seen_hashes),
        "targetDateReviewCount": target_review,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "passed": len(records) == len(seen_names) == len(seen_hashes),
        **payload["r4DateReprocessing"],
        "reasonCounts": dict(reason_counts),
        "outputFile": str(output_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
