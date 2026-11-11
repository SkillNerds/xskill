"""Small re-entrant cross-process file lock for TaskScope transactions."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
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


@contextmanager
def _root_lifecycle_lock(state_root: Path, skill_root: Path, *, blocking: bool = False, kind="writer"):
    """Own one native writer lifecycle; unlike task locks this is not reentrant.

    The stable file is deliberately outside skill repositories. It is never
    unlinked, since unlinking a lock file allows another process to lock a
    different inode while the previous owner is still draining its threads.
    """
    key = os.path.normcase(str(Path(skill_root).expanduser().resolve()))
    digest = hashlib.sha256(key.encode()).hexdigest()
    # Homes may share a skill library. The lock location must converge even
    # when their registry databases and state directories differ.
    path = Path(key).parent / ".xskill-library-locks" / f"{digest}.{kind}.lock"
    def check_path():
        for entry in (*path.parents, path):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RuntimeError(f"linked/reparse library lock path is unsupported: {entry}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise RuntimeError(f"hardlinked library lock path is unsupported: {entry}")
    check_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    check_path()
    thread_lock = _thread_lock(path)
    if not thread_lock.acquire(blocking=blocking):
        raise RuntimeError("skill library writer is still running (run lock busy)")
    canonical = str(path)
    owned = getattr(_LOCAL, "run_owned", None)
    if owned is None:
        owned = _LOCAL.run_owned = set()
    if canonical in owned:
        thread_lock.release()
        raise RuntimeError("skill library writer is still running (run lock busy)")
    try:
        with path.open("a+b") as lock_file:
            try:
                if os.name == "nt":
                    import msvcrt
                    if blocking:
                        _lock_windows(lock_file)
                    else:
                        lock_file.seek(0, os.SEEK_END)
                        if lock_file.tell() == 0:
                            lock_file.write(b"\0")
                            lock_file.flush()
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                    fcntl.flock(lock_file.fileno(), flags)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or _is_windows_contention(exc):
                    raise RuntimeError("skill library writer is still running (run lock busy)") from exc
                raise
            owned.add(canonical)
            try:
                yield
            finally:
                owned.remove(canonical)
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        thread_lock.release()


def root_run_lock(state_root: Path, skill_root: Path, *, blocking: bool = False):
    """Own the canonical library writer independent of the caller's home."""
    return _root_lifecycle_lock(state_root, skill_root, blocking=blocking)


def root_host_lock(state_root: Path, skill_root: Path, *, blocking: bool = False):
    """Separate host admission lock, allowing its child native writer to run."""
    return _root_lifecycle_lock(state_root, skill_root, blocking=blocking, kind="host")
