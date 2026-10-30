from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def connect_existing(
    database_path: str | Path, *, query_only: bool = False
) -> sqlite3.Connection:
    """Open an existing SQLite database without ever creating a path or file."""
    path = Path(database_path).resolve()
    if not path.parent.is_dir():
        raise FileNotFoundError(f"SQLite data 資料夾不存在：{path.parent}")
    if not path.is_file():
        raise FileNotFoundError(f"SQLite 不存在：{path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"SQLite 是 0 位元組空檔案：{path}")
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rw", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if query_only:
        connection.execute("PRAGMA query_only = ON")
    else:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def checkpoint(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
