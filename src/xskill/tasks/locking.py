"""Small re-entrant cross-process file lock for TaskScope transactions."""
from __future__ import annotations

import errno
import os
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_LOCAL = threading.local()
_WINDOWS_RETRY = threading.Event()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _is_windows_contention(error: OSError) -> bool:
    windows_error = getattr(error, "winerror", None)
    if windows_error is not None:
        return windows_error in {32, 33}
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def _lock_windows(lock_file) -> None:
    import msvcrt  # pylint: disable=import-error

    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    while True:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not _is_windows_contention(error):
                raise
            _WINDOWS_RETRY.wait(0.05)


@contextmanager
def task_file_lock(lock_path: Path):
    """Serialize one TaskScope across threads and worker processes."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    canonical = str(lock_path.resolve(strict=False))
    depths = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = {}
        _LOCAL.depths = depths
    with _thread_lock(lock_path):
        if depths.get(canonical, 0):
            depths[canonical] += 1
            try:
                yield
            finally:
                depths[canonical] -= 1
            return
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                _lock_windows(lock_file)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            depths[canonical] = 1
            try:
                yield
            finally:
                depths.pop(canonical, None)
                if os.name == "nt":
                    import msvcrt  # pylint: disable=import-error

                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
