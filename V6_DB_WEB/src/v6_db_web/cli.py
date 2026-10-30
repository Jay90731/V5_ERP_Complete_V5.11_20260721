from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import checkpoint, connect
from .database_lock import database_write_lock
from .importer import import_v5
from .formal_database import (
    FORMAL_CONFIRMATION,
    RESTORE_CONFIRMATION,
    apply_formal_store_migration,
    create_formal_backup,
    import_formal_v5,
    initialize_formal_database,
    reject_formal_path,
    restore_formal_database,
    validate_formal_database,
    write_formal_report,
)
from .migrations import apply_migrations
from .manual_review import apply_review_actions
from .ocr_ingest import replay_ocr_json
from .phase2_validation import (
    validate_phase2_replay,
    write_phase2_report,
)
from .phase3_pipeline import run_phase3
from .phase3_validation import (
    validate_phase3_runs,
    write_phase3_report,
)
from .phase3_environment import (
    check_phase3_environment,
    write_environment_report,
)
from .validation import validate_v5_import, write_report


ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"


def migrate_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner="cli:migrate"):
        connection = connect(args.db)
        try:
            result = apply_migrations(connection, MIGRATIONS)
            checkpoint(connection)
        finally:
            connection.close()
    print(json.dumps({"database": str(Path(args.db).resolve()), "migrations": result}, ensure_ascii=False, indent=2))
    return 0


def import_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner="cli:import-v5"):
        connection = connect(args.db)
        try:
            apply_migrations(connection, MIGRATIONS)
            stats = import_v5(connection, args.source)
            validation = validate_v5_import(connection, args.source)
            report = {
                "import": stats.as_dict(),
                **validation,
            }
            json_path, markdown_path = write_report(args.report, report)
            checkpoint(connection)
        finally:
            connection.close()
    print(
        json.dumps(
            {
                "database": str(Path(args.db).resolve()),
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": report["passed"],
                "import": stats.as_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


def validate_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner="cli:validate-v5"):
        connection = connect(args.db)
        try:
            apply_migrations(connection, MIGRATIONS)
            report = validate_v5_import(connection, args.source)
            json_path, markdown_path = write_report(args.report, report)
            checkpoint(connection)
        finally:
            connection.close()
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": report["passed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


def replay_ocr_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner=f"cli:replay:{args.job_type}"):
        connection = connect(args.db)
        try:
            apply_migrations(connection, MIGRATIONS)
            summary = replay_ocr_json(
                connection,
                args.input,
                args.date_from,
                args.date_to,
                job_type=args.job_type,
            )
            checkpoint(connection)
        finally:
            connection.close()
    output = summary.as_dict()
    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output["report"] = str(report_path)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def validate_phase2_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner="cli:validate-phase2"):
        connection = connect(args.db)
        try:
            apply_migrations(connection, MIGRATIONS)
            report = validate_phase2_replay(
                connection, args.first_job, args.second_job
            )
            json_path, markdown_path = write_phase2_report(args.report, report)
            checkpoint(connection)
        finally:
            connection.close()
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": report["passed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


def apply_review_command(args: argparse.Namespace) -> int:
    reject_formal_path(args.db, ROOT)
    with database_write_lock(args.db, owner="cli:apply-review"):
        connection = connect(args.db)
        try:
            apply_migrations(connection, MIGRATIONS)
            summary = apply_review_actions(connection, args.input)
            checkpoint(connection)
        finally:
            connection.close()
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0


def apply_formal_review_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:apply-formal-review"):
        backup = create_formal_backup(
            args.db,
            ROOT,
            backup_type="pre_update",
            notes="Before manual review actions",
        )
        connection = connect(args.db)
        try:
            summary = apply_review_actions(connection, args.input)
            checkpoint(connection)
        finally:
            connection.close()
    print(
        json.dumps(
            {"backup": backup, "review_actions": summary.as_dict()},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_daily_erp_command(args: argparse.Namespace) -> int:
    state = run_phase3(
        args.config,
        resume=args.resume,
        replay_ocr=args.replay_ocr,
        date_from=args.date_from,
        date_to=args.date_to,
        mode=args.mode,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if state["status"] == "completed":
        return 0
    if state["status"] == "stopped":
        return 2
    return 1


def validate_phase3_command(args: argparse.Namespace) -> int:
    report = validate_phase3_runs(args.first_run, args.second_run)
    json_path, markdown_path = write_phase3_report(args.report, report)
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": report["passed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


def check_phase3_command(args: argparse.Namespace) -> int:
    report = check_phase3_environment(args.config)
    json_path, text_path = write_environment_report(args.report, report)
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_text": str(text_path),
                "ready_for_replay": report["ready_for_replay"],
                "ready_for_erp_test": report["ready_for_erp_test"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ready_for_erp_test"] else 1


def initialize_formal_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:initialize-formal"):
        result = initialize_formal_database(
            args.db, MIGRATIONS, ROOT, args.confirm
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def import_formal_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:import-formal-v5"):
        result = import_formal_v5(args.db, args.source, ROOT)
    json_path, markdown_path = write_formal_report(args.report, result)
    print(
        json.dumps(
            {
                "database": str(Path(args.db).resolve()),
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": result["validation"]["passed"],
                "import": result["import"],
                "backup": result["backup"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["validation"]["passed"] else 1


def validate_formal_command(args: argparse.Namespace) -> int:
    result = validate_formal_database(args.db, args.source, ROOT)
    json_path, markdown_path = write_formal_report(args.report, result)
    print(
        json.dumps(
            {
                "report_json": str(json_path),
                "report_markdown": str(markdown_path),
                "passed": result["passed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["passed"] else 1


def backup_formal_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:backup-formal"):
        result = create_formal_backup(
            args.db, ROOT, backup_type="manual", notes=args.notes or "Manual backup"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def apply_formal_migrations_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:apply-formal-migrations"):
        result = apply_formal_store_migration(args.db, MIGRATIONS, ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def restore_formal_command(args: argparse.Namespace) -> int:
    with database_write_lock(args.db, owner="cli:restore-formal"):
        result = restore_formal_database(
            args.db, args.backup, ROOT, args.confirm
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="婚約訂單管理系統 V6 database tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="建立或升級 SQLite schema")
    migrate.add_argument("--db", required=True)
    migrate.set_defaults(handler=migrate_command)

    import_v5_parser = subparsers.add_parser(
        "import-v5", help="匯入 V5.10 order_history 與待確認資料"
    )
    import_v5_parser.add_argument("--db", required=True)
    import_v5_parser.add_argument("--source", required=True)
    import_v5_parser.add_argument("--report", required=True)
    import_v5_parser.set_defaults(handler=import_command)

    validate = subparsers.add_parser("validate-v5", help="驗證 V5.10 與 SQLite")
    validate.add_argument("--db", required=True)
    validate.add_argument("--source", required=True)
    validate.add_argument("--report", required=True)
    validate.set_defaults(handler=validate_command)

    replay = subparsers.add_parser(
        "replay-ocr", help="重播 OCR JSON 並透過共用服務寫入 SQLite"
    )
    replay.add_argument("--db", required=True)
    replay.add_argument("--input", required=True)
    replay.add_argument("--date-from", required=True)
    replay.add_argument("--date-to", required=True)
    replay.add_argument(
        "--job-type",
        default="daily_7_days",
        choices=[
            "daily_7_days",
            "monthly_6_months",
            "single_order",
        ],
    )
    replay.add_argument("--report")
    replay.set_defaults(handler=replay_ocr_command)

    phase2 = subparsers.add_parser(
        "validate-phase2", help="驗證兩次每日7天OCR重播及SQLite完整性"
    )
    phase2.add_argument("--db", required=True)
    phase2.add_argument("--first-job", required=True, type=int)
    phase2.add_argument("--second-job", required=True, type=int)
    phase2.add_argument("--report", required=True)
    phase2.set_defaults(handler=validate_phase2_command)

    review = subparsers.add_parser(
        "apply-review", help="將人工排除或修正結果寫回測試 SQLite"
    )
    review.add_argument("--db", required=True)
    review.add_argument("--input", required=True)
    review.set_defaults(handler=apply_review_command)

    daily = subparsers.add_parser(
        "run-daily-erp",
        help="執行V6第三階段ERP最近7天截圖、OCR及SQLite流程",
    )
    daily.add_argument("--config", required=True)
    daily.add_argument("--resume", action="store_true")
    daily.add_argument(
        "--replay-ocr",
        help="整合測試用：略過ERP及OCR，使用既有orders.json",
    )
    daily.add_argument("--date-from")
    daily.add_argument("--date-to")
    daily.add_argument(
        "--mode",
        default="daily_7_days",
        choices=["daily_7_days", "monthly_6_months"],
    )
    daily.set_defaults(handler=run_daily_erp_command)

    phase3 = subparsers.add_parser(
        "validate-phase3",
        help="驗證兩次V6第三階段批次及SQLite完整性",
    )
    phase3.add_argument("--first-run", required=True)
    phase3.add_argument("--second-run", required=True)
    phase3.add_argument("--report", required=True)
    phase3.set_defaults(handler=validate_phase3_command)

    environment = subparsers.add_parser(
        "check-phase3", help="檢查V6第三階段ERP電腦環境"
    )
    environment.add_argument("--config", required=True)
    environment.add_argument("--report", required=True)
    environment.set_defaults(handler=check_phase3_command)

    initialize_formal = subparsers.add_parser(
        "initialize-formal", help="以 migrations 全新建立正式 orders.db"
    )
    initialize_formal.add_argument("--db", required=True)
    initialize_formal.add_argument("--confirm", required=True)
    initialize_formal.set_defaults(handler=initialize_formal_command)

    import_formal = subparsers.add_parser(
        "import-formal-v5", help="備份後將 V5.10 六個月基準匯入正式資料庫"
    )
    import_formal.add_argument("--db", required=True)
    import_formal.add_argument("--source", required=True)
    import_formal.add_argument("--report", required=True)
    import_formal.set_defaults(handler=import_formal_command)

    validate_formal = subparsers.add_parser(
        "validate-formal", help="唯讀驗證正式 orders.db"
    )
    validate_formal.add_argument("--db", required=True)
    validate_formal.add_argument("--source", required=True)
    validate_formal.add_argument("--report", required=True)
    validate_formal.set_defaults(handler=validate_formal_command)

    backup_formal = subparsers.add_parser(
        "backup-formal", help="建立並驗證正式資料庫備份"
    )
    backup_formal.add_argument("--db", required=True)
    backup_formal.add_argument("--notes")
    backup_formal.set_defaults(handler=backup_formal_command)

    apply_formal = subparsers.add_parser(
        "apply-formal-migrations",
        help="備份、套用正式 migration，並驗證 SR 門市規則",
    )
    apply_formal.add_argument("--db", required=True)
    apply_formal.set_defaults(handler=apply_formal_migrations_command)

    formal_review = subparsers.add_parser(
        "apply-formal-review",
        help="備份後將人工排除或修正結果寫回正式 SQLite",
    )
    formal_review.add_argument("--db", required=True)
    formal_review.add_argument("--input", required=True)
    formal_review.set_defaults(handler=apply_formal_review_command)

    restore_formal = subparsers.add_parser(
        "restore-formal", help="確認後原子還原正式資料庫"
    )
    restore_formal.add_argument("--db", required=True)
    restore_formal.add_argument("--backup", required=True)
    restore_formal.add_argument("--confirm", required=True)
    restore_formal.set_defaults(handler=restore_formal_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))
