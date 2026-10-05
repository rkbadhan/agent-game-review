"""Atomic JSON writes and reentrant locks shared by CLI and workspace writers."""
from contextlib import contextmanager
import json
import errno
import os
from pathlib import Path
import tempfile
import threading
import time

_locks = {}
_guard = threading.Lock()
_held = threading.local()


def sharing_retry(action):
    """Windows readers/renames can briefly conflict at atomic replacement."""
    deadline = time.monotonic() + 1
    while True:
        try:
            return action()
        except PermissionError as exc:
            transient = getattr(exc, "winerror", None) in (5, 32, 33) or (os.name == "nt" and exc.errno == errno.EACCES)
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def read_json(path):
    path = Path(path)
    return json.loads(sharing_retry(lambda: path.read_text(encoding="utf-8")))


@contextmanager
def file_lock(path):
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "paths", set())
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            # Windows byte-range locks prohibit reading the locked byte.
            # File metadata can be checked before acquiring the lock safely.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(0.01)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            _held.paths = held | {key}
            try:
                yield
            finally:
                _held.paths = held
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        sharing_retry(lambda: os.replace(name, path))
    finally:
        if os.path.exists(name):
            os.unlink(name)
