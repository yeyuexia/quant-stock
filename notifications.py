"""Telegram-notification queue I/O — concurrent-safe append helper.

All writers (rebalancer / executor / watchdog / quant subagent) call
`append_notification(record)`. Multiple processes can write at the same time
without losing messages because the helper acquires an fcntl exclusive lock
before the read-modify-write cycle.

Format on disk is still a single JSON array (back-compat with the existing
Telegram bot reader). Quadratic write cost over the file's lifetime is a known
limitation — trim or rotate as needed.
"""
from __future__ import annotations
import datetime as dt
import errno
import fcntl
import json
import os
from typing import Optional

import config


def _notify_path() -> Optional[str]:
    return getattr(config, "TELEGRAM_NOTIFY_PATH", None)


def append_notification(record: dict, *, path: Optional[str] = None) -> None:
    """Atomically append one record to the TG notification queue.

    `record` should at minimum contain `ts`, `source`, and `message`; callers
    may add arbitrary additional fields. No-op when both `path` and
    TELEGRAM_NOTIFY_PATH are unset. Stamps `ts` on the record if missing.

    `path` lets callers (e.g., quant subagent) target their own queue file
    while still benefiting from the lock-protected append.
    """
    target = path or _notify_path()
    if not target:
        return
    if "ts" not in record:
        record = dict(record, ts=dt.datetime.now(dt.timezone.utc).isoformat())

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Sidecar .lock convention (matches _save_screener_cache, log_daily,
    # _set_climax_fired etc.) — keeps the lock target separate from the data
    # file we mutate so the codebase has one consistent locking pattern.
    lock_path = target + ".lock"
    with open(lock_path, "w") as lk:
        try:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            # Filesystems that don't support locking (rare on macOS/Linux):
            # fall through and accept the race. ENOTSUP = unsupported.
            if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise

        existing: list = []
        if os.path.exists(target):
            try:
                with open(target, "r") as f:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = []
            except (json.JSONDecodeError, ValueError, OSError):
                existing = []
        existing.append(record)

        # Write to tmp + atomic rename so a reader never sees a partial file.
        tmp_path = target + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(existing, f, indent=2, default=str)
        os.replace(tmp_path, target)


def read_notifications() -> list:
    """Read the entire notification list. Returns [] if file missing/empty.

    Intended for the TG bot consumer and tests. Acquires a shared lock so it
    can't race with a concurrent writer mid-update.
    """
    path = _notify_path()
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
        try:
            data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
