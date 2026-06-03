"""Concurrency-safe file I/O helpers.

All disk writes that can race across cron processes (rebalancer, executor,
watchdog, telegram bot, quant subagent) should go through these helpers.
They use fcntl `.lock` sidecars + tmp-rename for atomic visibility:

  - `.lock` sidecar = a separate empty file we flock(LOCK_EX) on. Keeps
    the lock target distinct from the data file we mutate (matches the
    codebase convention used by notifications, watchdog log_daily,
    screener cache, etc.).
  - tmp + os.replace = atomic on POSIX. Readers never see a half-written
    file; they see either the old version or the new version.

The two main entry points:
  atomic_write_json(path, data) — overwrite path's contents wholesale
  read_modify_write_json(path, mutator, default=...) — lock, read, mutate,
    write back atomically. mutator is called with the loaded data (or
    `default` if missing/corrupt) and returns the new data to persist.
"""
from __future__ import annotations
import errno
import fcntl
import json
import os
from typing import Any, Callable, Optional


def _lock_path(target: str) -> str:
    return target + ".lock"


def _flock_or_continue(fd: int) -> None:
    """Acquire LOCK_EX on fd; swallow the ENOTSUP / EOPNOTSUPP that some
    filesystems return for locking — better to race than crash."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
            raise


def atomic_write_json(path: str, data: Any, *, indent: int = 2) -> None:
    """Lock-protected, atomic JSON overwrite. Creates parent dirs as needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(_lock_path(path), "w") as lk:
        _flock_or_continue(lk.fileno())
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=indent, default=str)
        os.replace(tmp, path)


def read_modify_write_json(
    path: str,
    mutator: Callable[[Any], Any],
    *,
    default: Optional[Any] = None,
    indent: int = 2,
) -> Any:
    """Lock the path, read JSON (or `default` if missing/corrupt), pass to
    mutator(data) → new_data, atomically write the result. Returns new_data.

    The whole read-modify-write is inside the lock so concurrent writers
    can't lose each other's changes.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(_lock_path(path), "w") as lk:
        _flock_or_continue(lk.fileno())

        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, ValueError, OSError):
                data = {} if default is None else default
        else:
            data = {} if default is None else default

        new_data = mutator(data)

        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new_data, f, indent=indent, default=str)
        os.replace(tmp, path)
        return new_data


def atomic_write_csv(path: str, df, **to_csv_kwargs) -> None:
    """Lock-protected, atomic CSV overwrite via pandas.to_csv.

    Writes to a tmp file then os.replace — readers either see the old
    version or the new one, never a half-flushed buffer. Same `.lock`
    sidecar convention as atomic_write_json.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(_lock_path(path), "w") as lk:
        _flock_or_continue(lk.fileno())
        tmp = path + ".tmp"
        df.to_csv(tmp, **to_csv_kwargs)
        os.replace(tmp, path)


def atomic_write_parquet(path: str, df) -> None:
    """Lock-protected, atomic Parquet overwrite via pandas.to_parquet.

    Same fcntl + tmp+rename pattern as atomic_write_json/csv. Used by
    data.fetch_ohlcv to keep the per-symbol OHLCV cache writes safe
    against concurrent cron jobs that all hit the same ticker batch.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(_lock_path(path), "w") as lk:
        _flock_or_continue(lk.fileno())
        tmp = path + ".tmp"
        df.to_parquet(tmp)
        os.replace(tmp, path)


def atomic_append_text(path: str, line: str) -> None:
    """Lock-protected text append. Adds a trailing newline if absent.

    For per-event log files (orders events, breaker hits) where readers
    don't expect transactional consistency — we just want to avoid two
    writers garbling each other's lines mid-flush.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not line.endswith("\n"):
        line += "\n"
    with open(_lock_path(path), "w") as lk:
        _flock_or_continue(lk.fileno())
        with open(path, "a") as f:
            f.write(line)
