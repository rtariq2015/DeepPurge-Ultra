"""Bounded activity history, rotating logs and a cross-process operation lock."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import tempfile

from .cleanup import xdg_path


class OperationBusyError(BlockingIOError):
    """Another process owns the operation lock; no backend action was started."""


def state_directory() -> Path:
    root = xdg_path("XDG_STATE_HOME", ".local/state") / "deep-purge-ultra"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("Activity directory must be a real directory owned by the current user")
    return root


def secure_open(path: Path, flags: int):
    # Validate before a requested truncation and never block on an unexpected FIFO.
    descriptor = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW |
                         os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise OSError(f"Refusing an unsafe state file: {path}")
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class History:
    def __init__(self):
        self.root = state_directory()

    def read(self) -> list[dict]:
        path = self.root / "activity.jsonl"
        try:
            with os.fdopen(secure_open(path, os.O_RDONLY), encoding="utf-8", errors="replace") as stream:
                entries = []
                for line in stream:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue
                    entries = entries[-200:]
                return entries
        except FileNotFoundError:
            return []

    def append(self, app_name: str, action: str, result):
        entries = self.read()
        entries.append({
            "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "app": app_name, "action": action, "status": result.status,
            "summary": result.summary, "details": list(result.details),
        })
        descriptor, filename = tempfile.mkstemp(prefix=".activity-", suffix=".tmp", dir=self.root)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for entry in entries[-200:]:
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / "activity.jsonl")
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def operation_lock(self):
        descriptor = secure_open(self.root / "operation.lock", os.O_CREAT | os.O_RDWR)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OperationBusyError("Another DeepPurge operation owns the lock") from error
            yield
        finally:
            os.close(descriptor)


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        path = state_directory() / "deep-purge.log"
        os.close(secure_open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY))
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError as error:
        logging.warning("File logging unavailable: %s", error)
