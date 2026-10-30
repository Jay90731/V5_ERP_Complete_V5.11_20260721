"""Read-only reconciliation and export tools. No migration/import/promotion command."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from v6_db_web.database_lock import database_write_lock  # noqa: E402

R3_ROOT = Path(r"C:\Users\auntstella\Desktop\V6_DB_WEB_DUAL_R3_20260825_165102")
FIXED_ROOT = Path(r"C:\Users\auntstella\Desktop\V6_DB_WEB")
MUTABLE_DIRECTORIES = ("data", "storage", "runs", "control", "backups")
CONFIGURATION_FILES = ("phase3_config.json", "phase3_labels.json", "formal_database_modes.json",
                       "bhq_watcher_formal.json", "latest_run_path.txt")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest().upper()


def encode(value):
    if isinstance(value, bytes):
        return {"blob_base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(type(value).__name__)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode)


def row_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest().upper()


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def read_connection(path):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Existing nonempty database required: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def inventory(database, projection=None):
    """Hash every existing row; projection preserves original columns across migration."""
    database = Path(database).resolve()
    connection = read_connection(database)
    try:
        connection.execute("BEGIN")
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        schemas = {row["name"]: row["sql"] for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )}
        tables = {}
        for name, sql in schemas.items():
            info = list(connection.execute(f"PRAGMA table_info({ident(name)})"))
            columns = [row["name"] for row in info]
            pk = [row["name"] for row in sorted(info, key=lambda r: r["pk"]) if row["pk"]]
            if projection and name in projection["tables"]:
                original = projection["tables"][name]
                columns = original["columns"]
                pk = original["primary_key"]
                missing = set(columns) - {row["name"] for row in info}
                if missing:
                    tables[name] = {"error": "missing_original_columns", "missing": sorted(missing)}
                    continue
            rows = {}
            timestamps = {}
            for row in connection.execute(f"SELECT {','.join(map(ident, columns))} FROM {ident(name)}"):
                values = dict(row)
                fingerprint = row_hash(values)
                key = canonical([values[column] for column in pk]) if pk else fingerprint
                if not pk:
                    rows[key] = rows.get(key, 0) + 1
                else:
                    if key in rows:
                        raise RuntimeError(f"Duplicate primary key in {name}: {key}")
                    rows[key] = fingerprint
                for column in columns:
                    if column.endswith("_at") and values[column] is not None:
                        timestamps[column] = max(timestamps.get(column, ""), str(values[column]))
            tables[name] = {
                "columns": columns, "primary_key": pk, "schema_sql": sql,
                "count": sum(rows.values()) if not pk else len(rows),
                "rows": rows, "rows_sha256": row_hash(rows), "latest_timestamps": timestamps,
            }
        migrations = [dict(row) for row in connection.execute(
            "SELECT * FROM database_migrations ORDER BY version"
        )] if "database_migrations" in schemas else []
        marker_row = connection.execute(
            "SELECT setting_value FROM system_settings WHERE setting_key='database_mode'"
        ).fetchone() if "system_settings" in schemas else None
        natural = {"orders": {}, "versions": {}}
        if "orders" in schemas and "order_versions" in schemas:
            for row in connection.execute(
                "SELECT o.order_no,o.order_date,o.category,o.status,o.is_voided,"
                "v.content_hash,v.source_system FROM orders o LEFT JOIN order_versions v ON v.id=o.current_version_id"
            ):
                natural["orders"][row["order_no"]] = dict(row)
            for row in connection.execute(
                "SELECT o.order_no,v.version_no,v.content_hash,v.source_system "
                "FROM order_versions v JOIN orders o ON o.id=v.order_id"
            ):
                natural["versions"][canonical([row["order_no"], row["version_no"]])] = dict(row)
        running_jobs = 0
        if "scan_jobs" in schemas:
            running_jobs = connection.execute("SELECT COUNT(*) FROM scan_jobs WHERE status='running'").fetchone()[0]
        return {
            "captured_at": now(), "database": str(database), "file_sha256": digest(database),
            "integrity_check": integrity, "foreign_key_errors": foreign,
            "database_mode": marker_row[0] if marker_row else None,
            "migrations": migrations, "running_scan_jobs": running_jobs,
            "tables": tables, "natural_keys": natural,
            "logical_sha256": row_hash({name: value.get("rows_sha256", value) for name, value in tables.items()}),
            "projected_original_columns": bool(projection),
        }
    finally:
        connection.close()


def compare(target, reference):
    result = {"status": "comparison_only_no_merge", "target_database": target["database"],
              "reference_database": reference["database"], "tables": {}, "orders_and_versions": {}}
    for name in sorted(set(target["tables"]) | set(reference["tables"])):
        left, right = target["tables"].get(name, {}), reference["tables"].get(name, {})
        a, b = left.get("rows", {}), right.get("rows", {})
        same_columns = left.get("columns") == right.get("columns")
        result["tables"][name] = {
            "target_count": left.get("count", 0), "reference_count": right.get("count", 0),
            "same_columns": same_columns,
            "target_only_keys": sorted(set(a) - set(b)),
            "reference_only_keys": sorted(set(b) - set(a)),
            "different_row_keys": sorted(key for key in set(a) & set(b) if a[key] != b[key]) if same_columns else None,
            "target_latest_timestamps": left.get("latest_timestamps", {}),
            "reference_latest_timestamps": right.get("latest_timestamps", {}),
        }
    for kind in ("orders", "versions"):
        a, b = target.get("natural_keys", {}).get(kind, {}), reference.get("natural_keys", {}).get(kind, {})
        result["orders_and_versions"][kind] = {
            "target_only": sorted(set(a) - set(b)),
            "reference_only": sorted(set(b) - set(a)),
            "conflicts": [{"key": key, "target": a[key], "reference": b[key]}
                          for key in sorted(set(a) & set(b)) if a[key] != b[key]],
        }
    result["note"] = "IDs/timestamps/counts are evidence only, not a merge policy. Target latest R3 remains the base."
    return result


def retention(original, candidate):
    checks = {}
    for name, old in original["tables"].items():
        new = candidate["tables"].get(name, {})
        if old["columns"] != new.get("columns") or old["primary_key"] != new.get("primary_key"):
            checks[name] = {"passed": False, "reason": "candidate_must_use_original_column_projection"}
            continue
        old_rows, new_rows = old["rows"], new.get("rows", {})
        missing = sorted(set(old_rows) - set(new_rows))
        changed = sorted(key for key in set(old_rows) & set(new_rows)
                         if (old_rows[key] != new_rows[key] if old["primary_key"] else old_rows[key] > new_rows[key]))
        checks[name] = {"passed": not missing and not changed, "missing_original_keys": missing,
                        "changed_original_keys": changed, "additional_rows": new.get("count", 0) - old["count"]}
    return {"passed": all(value["passed"] for value in checks.values()), "tables": checks,
            "scope": "Every original table/row/column retained; additions allowed; no implicit conflict resolution."}


def files_under(root, directories):
    root = Path(root).resolve()
    files = {}
    for relative in directories:
        directory = root / relative
        if not directory.exists():
            continue
        if root not in directory.resolve().parents:
            raise ValueError(f"Asset directory escapes source: {directory}")
        for path in directory.rglob("*"):
            if root not in path.resolve().parents:
                raise ValueError(f"Asset path escapes source: {path}")
            if not path.is_file() or path.name.endswith((".write.lock", "-wal", "-shm", "-journal")):
                continue
            if path.resolve() == root / "data" / "orders.db":
                continue
            files[path.relative_to(root).as_posix()] = {"sha256": digest(path), "bytes": path.stat().st_size}
    return files


def configuration_files(root):
    root = Path(root).resolve()
    files = {}
    for name in CONFIGURATION_FILES:
        path = root / name
        if not path.exists():
            continue
        if root not in path.resolve().parents or not path.is_file():
            raise ValueError(f"Configuration path escapes source or is not a file: {path}")
        files[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
    return files


def write_report(path, value):
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("Evidence output must be a .json report, never a database path")
    if path.exists():
        raise FileExistsError(f"Report will not be overwritten: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def capture(source_root, destination):
    """Future maintenance capture; only writes a new external evidence directory."""
    source_root, destination = Path(source_root).resolve(), Path(destination).resolve()
    source = source_root / "data" / "orders.db"
    if destination == source_root or source_root in destination.parents or destination in source_root.parents:
        raise ValueError("Capture output must be outside the R3 project")
    if destination == FIXED_ROOT.resolve() or FIXED_ROOT.resolve() in destination.parents:
        raise ValueError("Capture cannot create the future formal project/database")
    if destination.exists():
        raise FileExistsError("Capture directory already exists; refusing overwrite")
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError(source)
    if source_root not in source.resolve().parents:
        raise ValueError("Source database resolves outside the R3 project")
    with database_write_lock(source, "R3 read-only maintenance capture"):
        before = inventory(source)
        if before["integrity_check"] != ["ok"] or before["foreign_key_errors"]:
            raise RuntimeError("R3 SQLite health failed; no valid capture created")
        if before["database_mode"] != "formal" or before["running_scan_jobs"]:
            raise RuntimeError("R3 formal marker/running scan check failed")
        original_assets = files_under(source_root, MUTABLE_DIRECTORIES)
        original_configs = configuration_files(source_root)
        destination.mkdir(parents=True, exist_ok=False)
        copied = destination / "latest_r3.sqlite"
        connection = read_connection(source)
        try:
            with closing(sqlite3.connect(copied)) as output:
                connection.backup(output)
        finally:
            connection.close()
        snapshot = inventory(copied)
        for relative in {**original_assets, **original_configs}:
            target = destination / "assets" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_root / relative, target)
        after = inventory(source)
        after_assets = files_under(source_root, MUTABLE_DIRECTORIES)
        copied_assets = files_under(destination / "assets", MUTABLE_DIRECTORIES)
        after_configs = configuration_files(source_root)
        copied_configs = configuration_files(destination / "assets")
        passed = (
            before["logical_sha256"] == after["logical_sha256"] == snapshot["logical_sha256"]
            and before["file_sha256"] == after["file_sha256"]
            and original_assets == after_assets == copied_assets
            and original_configs == after_configs == copied_configs
            and snapshot["integrity_check"] == ["ok"] and not snapshot["foreign_key_errors"]
        )
        write_report(destination / "r3_inventory.json", snapshot)
        write_report(destination / "assets_manifest.json", original_assets)
        write_report(destination / "configuration_manifest.json", original_configs)
        report = {"status": "verified_capture" if passed else "failed_capture_do_not_use",
                  "captured_at": now(), "source": str(source), "snapshot": str(copied),
                  "snapshot_sha256": digest(copied), "source_sha256_before": before["file_sha256"],
                  "source_sha256_after": after["file_sha256"], "source_logical_unchanged":
                  before["logical_sha256"] == after["logical_sha256"], "asset_count": len(original_assets),
                  "assets_verified": original_assets == after_assets == copied_assets,
                  "configuration_count": len(original_configs),
                  "configurations_verified": original_configs == after_configs == copied_configs,
                  "source_backup_records_modified": False,
                  "note": "External capture audit only. Existing formal backup_records mechanism is used later on approved candidate, not on protected R3."}
        write_report(destination / "CAPTURE_REPORT.json", report)
        if not passed:
            raise RuntimeError("R3 changed during capture; retain failed evidence and capture again after maintenance stop")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory")
    inv.add_argument("--database", type=Path, required=True)
    inv.add_argument("--output", type=Path, required=True)
    inv.add_argument("--original-inventory", type=Path)
    cmp = commands.add_parser("compare")
    cmp.add_argument("--target-inventory", type=Path, required=True)
    cmp.add_argument("--reference-inventory", type=Path, required=True)
    cmp.add_argument("--output", type=Path, required=True)
    keep = commands.add_parser("retention")
    keep.add_argument("--original-inventory", type=Path, required=True)
    keep.add_argument("--candidate-inventory", type=Path, required=True)
    keep.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("capture-r3")
    export.add_argument("--output-directory", type=Path, required=True)
    export.add_argument("--maintenance-confirmed", action="store_true")
    args = parser.parse_args()
    read_json = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    if args.command == "capture-r3":
        if not args.maintenance_confirmed:
            raise PermissionError("Stop ERP/web/manual review/cleanup first; explicit maintenance confirmation required")
        result = capture(R3_ROOT, args.output_directory)
    elif args.command == "inventory":
        result = inventory(args.database, read_json(args.original_inventory) if args.original_inventory else None)
        write_report(args.output, result)
    elif args.command == "compare":
        result = compare(read_json(args.target_inventory), read_json(args.reference_inventory))
        write_report(args.output, result)
    else:
        result = retention(read_json(args.original_inventory), read_json(args.candidate_inventory))
        write_report(args.output, result)
    print(json.dumps({"command": args.command, "status": result.get("status", "reported"),
                      "passed": result.get("passed"), "output": str(getattr(args, "output", ""))}, ensure_ascii=False))
    return 1 if result.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
