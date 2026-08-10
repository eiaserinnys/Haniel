"""Cross-platform file locks used by lifecycle ownership and transactions."""

from __future__ import annotations

import os
import threading
from contextlib import AbstractContextManager
from pathlib import Path

from .deployment_errors import StableDeploymentError


class LifecycleConflict(StableDeploymentError):
    """A stable lifecycle identity or lease conflict."""


def _is_windows_lease_contention(error: Exception) -> bool:
    return (
        os.name == "nt"
        and isinstance(error, PermissionError)
        and getattr(error, "winerror", None) in {32, 33}
    )


class FileLease:
    """A non-blocking lifetime lease whose contents carry no identity contract."""

    _guard = threading.Lock()
    _active: dict[str, str] = {}

    def __init__(self, path: Path, holder: str, conflict_code: str) -> None:
        self.path = path
        self.holder = holder
        self.conflict_code = conflict_code
        self._handle = None
        handle = None
        key = str(path.resolve(strict=False))
        with self._guard:
            current = self._active.get(key)
            if current is not None:
                raise LifecycleConflict(conflict_code, f"lease is held by {current}")
            self._active[key] = holder
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            handle = path.open("r+b")
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            self._lock(handle)
            self._handle = handle
        except Exception as error:
            if handle is not None:
                handle.close()
            with self._guard:
                self._active.pop(key, None)
            if _is_windows_lease_contention(error):
                raise LifecycleConflict(
                    conflict_code, "OS lease is already held"
                ) from error
            raise

    def _lock(self, handle) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise LifecycleConflict(
                    self.conflict_code, "OS lease is already held"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LifecycleConflict(
                    self.conflict_code, "OS lease is already held"
                ) from error

    @staticmethod
    def _unlock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        if self._handle is None:
            return
        key = str(self.path.resolve(strict=False))
        try:
            self._unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None
            with self._guard:
                self._active.pop(key, None)


class SerialFileLock(AbstractContextManager["SerialFileLock"]):
    """A blocking cross-process transaction lock with same-process serialization."""

    _guard = threading.Lock()
    _local_locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        key = str(path.resolve(strict=False))
        with self._guard:
            self._local_lock = self._local_locks.setdefault(key, threading.Lock())
        self._local_lock.acquire()
        self._handle = None
        handle = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            self._lock(handle)
            self._handle = handle
        except Exception:
            if handle is not None:
                handle.close()
            self._local_lock.release()
            raise

    @staticmethod
    def _lock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._handle is not None:
                FileLease._unlock(self._handle)
                self._handle.close()
                self._handle = None
        finally:
            self._local_lock.release()
