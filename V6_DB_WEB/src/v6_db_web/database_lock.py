from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DatabaseWriteLockError(RuntimeError):
    pass


_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_HANDLES: dict[str, object] = {}
_DEPTH: dict[str, int] = {}


def lock_path_for_database(database: str | Path) -> Path:
    path = Path(database).resolve()
    return path.with_name(f"{path.name}.write.lock")


def _thread_lock(key: str) -> threading.RLock:
    with _REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lock_file(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def database_write_lock(
    database: str | Path,
    owner: str = "V6 writer",
) -> Iterator[Path]:
    lock_path = lock_path_for_database(database)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(lock_path))
    local_lock = _thread_lock(key)
    if not local_lock.acquire(blocking=False):
        raise DatabaseWriteLockError("資料庫正在更新，請稍後重試")
    try:
        depth = _DEPTH.get(key, 0)
        if depth == 0:
            handle = lock_path.open("a+b")
            try:
                _lock_file(handle)
            except (OSError, BlockingIOError) as exc:
                handle.close()
                raise DatabaseWriteLockError(
                    f"資料庫正在更新，請稍後重試（目前工作：{owner}）"
                ) from exc
            _HANDLES[key] = handle
        _DEPTH[key] = depth + 1
        yield lock_path
    finally:
        depth = _DEPTH.get(key, 0) - 1
        if depth <= 0:
            handle = _HANDLES.pop(key, None)
            _DEPTH.pop(key, None)
            if handle is not None:
                try:
                    _unlock_file(handle)
                finally:
                    handle.close()
        else:
            _DEPTH[key] = depth
        local_lock.release()
