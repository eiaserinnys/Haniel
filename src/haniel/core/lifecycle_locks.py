"""Cross-platform file locks used by lifecycle ownership and transactions."""

from __future__ import annotations

import os
import threading
from contextlib import AbstractContextManager
from pathlib import Path


class LifecycleConflict(RuntimeError):
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
                raise LifecycleConflict(f"{conflict_code}: lease is held by {current}")
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
                    f"{conflict_code}: OS lease is already held"
                ) from error
            raise

    def _lock(self, handle) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise LifecycleConflict(
                    f"{self.conflict_code}: OS lease is already held"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LifecycleConflict(
                    f"{self.conflict_code}: OS lease is already held"
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


class ConfigTransactionLock(AbstractContextManager["ConfigTransactionLock"]):
    """Artifact-free cross-process lock for one configuration identity.

    POSIX locks the stable parent directory inode, so atomic replacement of the
    config file cannot bypass the lease. Windows uses a named kernel mutex for
    the canonical config path. Same-process reentrancy is owned by the caller.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve(strict=False)
        self._directory_handle: int | None = None
        self._windows_handle = None
        self._kernel32 = None
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()

    def _acquire_posix(self) -> None:
        import fcntl

        handle = os.open(self.config_path.parent, os.O_RDONLY)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except Exception:
            os.close(handle)
            raise
        self._directory_handle = handle

    def _acquire_windows(self) -> None:
        import ctypes
        import hashlib
        from ctypes import wintypes

        canonical = os.path.normcase(str(self.config_path))
        identity = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        name = f"Global\\HanielConfigWrite_{identity}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if result not in (0x00000000, 0x00000080):
            kernel32.CloseHandle(handle)
            raise OSError(f"WaitForSingleObject failed: {result}")
        self._kernel32 = kernel32
        self._windows_handle = handle

    def __exit__(self, exc_type, exc, traceback) -> None:
        if os.name == "nt":
            if self._windows_handle is not None and self._kernel32 is not None:
                import ctypes

                released = self._kernel32.ReleaseMutex(self._windows_handle)
                release_error = ctypes.get_last_error() if not released else None
                closed = self._kernel32.CloseHandle(self._windows_handle)
                close_error = ctypes.get_last_error() if not closed else None
                self._windows_handle = None
                if release_error is not None:
                    raise ctypes.WinError(release_error)
                if close_error is not None:
                    raise ctypes.WinError(close_error)
            return

        if self._directory_handle is not None:
            import fcntl

            try:
                fcntl.flock(self._directory_handle, fcntl.LOCK_UN)
            finally:
                os.close(self._directory_handle)
                self._directory_handle = None
