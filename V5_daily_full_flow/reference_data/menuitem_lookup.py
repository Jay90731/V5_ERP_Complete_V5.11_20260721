from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def normalize_code(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def read_cached_lookup(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    if not path.exists():
        return {}, {}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    products = document.get("products") or {}
    lookup = {
        str(code).strip(): str(name).strip()
        for code, name in products.items()
        if str(code).strip() and str(name).strip()
    }
    return lookup, document


def read_menuitem_xls(path: Path) -> dict[str, str]:
    import xlrd

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        sheet = workbook.sheet_by_index(0)
        header_row = None
        name_column = None
        code_column = None
        for row_index in range(min(10, sheet.nrows)):
            headers = [str(sheet.cell_value(row_index, col)).strip() for col in range(sheet.ncols)]
            if "品項名稱" in headers and "產品代碼" in headers:
                header_row = row_index
                name_column = headers.index("品項名稱")
                code_column = headers.index("產品代碼")
                break
        if header_row is None or name_column is None or code_column is None:
            raise ValueError("MENUITEM headers 品項名稱/產品代碼 were not found")

        products: dict[str, str] = {}
        for row_index in range(header_row + 1, sheet.nrows):
            code = normalize_code(sheet.cell_value(row_index, code_column))
            name = str(sheet.cell_value(row_index, name_column) or "").strip()
            if not code or not name:
                continue
            if code in products and products[code] != name:
                raise ValueError(f"Conflicting MENUITEM names for product code {code}")
            products[code] = name
        if not products:
            raise ValueError("MENUITEM contains no usable product rows")
        return products
    finally:
        workbook.release_resources()


def write_lookup(path: Path, source: Path, source_hash: str, products: dict[str, str]) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_file": source.name,
        "source_sha256": source_hash,
        "item_count": len(products),
        "products": dict(sorted(products.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_path, path)


def load_or_refresh(
    source_path: Path,
    lookup_path: Path,
    *,
    force: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    cached, document = read_cached_lookup(lookup_path)
    if not source_path.exists():
        if cached:
            return cached, {"status": "cached_source_missing", "error": "MENUITEM.XLS not found"}
        raise FileNotFoundError(f"MENUITEM.XLS not found: {source_path}")

    source_hash = file_sha256(source_path)
    if not force and cached and str(document.get("source_sha256") or "").upper() == source_hash:
        return cached, {"status": "unchanged", "source_sha256": source_hash, "item_count": len(cached)}

    try:
        products = read_menuitem_xls(source_path)
        write_lookup(lookup_path, source_path, source_hash, products)
        return products, {"status": "refreshed", "source_sha256": source_hash, "item_count": len(products)}
    except Exception as error:
        if cached:
            return cached, {
                "status": "cached_refresh_failed",
                "source_sha256": source_hash,
                "item_count": len(cached),
                "error": str(error),
            }
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("MENUITEM.XLS"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("product_lookup.json"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    products, status = load_or_refresh(args.input, args.output, force=args.force)
    print(json.dumps({**status, "output": str(args.output), "item_count": len(products)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
