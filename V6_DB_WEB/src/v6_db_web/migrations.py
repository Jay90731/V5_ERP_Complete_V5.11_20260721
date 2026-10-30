from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS database_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_sql_statements(script: str):
    buffer: list[str] = []
    for line in script.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    trailing = "".join(buffer).strip()
    if trailing:
        raise ValueError("Migration contains an incomplete SQL statement")


def migration_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    files = sorted(root.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise FileNotFoundError(f"No migration SQL files found in {root}")
    return files


def apply_migrations(
    connection: sqlite3.Connection,
    directory: str | Path,
) -> list[dict[str, object]]:
    connection.execute(MIGRATION_TABLE_SQL)
    connection.commit()
    applied: list[dict[str, object]] = []

    for path in migration_files(directory):
        version = int(path.name.split("_", 1)[0])
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT name, checksum FROM database_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if existing:
            if existing["name"] != path.name or existing["checksum"] != checksum:
                raise RuntimeError(
                    f"Applied migration {version} does not match {path.name}"
                )
            applied.append(
                {"version": version, "name": path.name, "status": "already_applied"}
            )
            continue

        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in iter_sql_statements(sql):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO database_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (version, path.name, checksum, utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        applied.append({"version": version, "name": path.name, "status": "applied"})

    return applied

