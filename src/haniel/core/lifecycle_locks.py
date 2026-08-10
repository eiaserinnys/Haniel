"""Cross-platform file locks used by lifecycle ownership and transactions."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path

from .deployment_errors import StableDeploymentError

logger = logging.getLogger(__name__)

CONFIG_TRANSACTION_WAIT_SECONDS = 30.0
SERIAL_FILE_LOCK_WAIT_SECONDS = 30.0


class LifecycleConflict(StableDeploymentError):
    """A stable lifecycle identity or lease conflict."""


class ConfigTransactionLockTimeout(StableDeploymentError):
    """A cross-process config transaction lock exceeded its bounded wait."""

    def __init__(self, timeout_seconds: float, holder: str | None = None) -> None:
        detail = f"config transaction lock was not acquired within {timeout_seconds:g}s"
        if holder:
            detail += f"; holder={holder}"
        super().__init__(
            "CONFIG_LOCK_TIMEOUT",
            detail,
        )


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
    """One bounded file-lock implementation for every serialized transaction."""

    _guard = threading.Lock()
    _local_locks: dict[str, threading.Lock] = {}
    DEFAULT_TIMEOUT_SECONDS = SERIAL_FILE_LOCK_WAIT_SECONDS
    MIN_BACKOFF_SECONDS = 0.01
    MAX_BACKOFF_SECONDS = 0.2

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float | None = None,
        operation: str = "serial",
    ) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.timeout_seconds = (
            self.DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        if self.timeout_seconds <= 0:
            raise ValueError("file lock timeout must be positive")
        self.operation = operation
        key = os.path.normcase(str(self.path))
        with self._guard:
            self._local_lock = self._local_locks.setdefault(key, threading.Lock())
        deadline = time.monotonic() + self.timeout_seconds
        if not self._local_lock.acquire(timeout=self.timeout_seconds):
            raise ConfigTransactionLockTimeout(
                self.timeout_seconds, self._read_holder_evidence()
            )
        self._handle = None
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0)
            self._acquire_os_lock(handle, deadline)
            self._handle = handle
            self._write_holder_evidence()
        except Exception:
            if handle is not None:
                handle.close()
            self._local_lock.release()
            raise

    def _try_lock(self, handle) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError as error:
                if isinstance(error, PermissionError) or getattr(
                    error, "winerror", None
                ) in {32, 33, 36}:
                    return False
                raise

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _acquire_os_lock(self, handle, deadline: float) -> None:
        backoff = self.MIN_BACKOFF_SECONDS
        waiting_logged = False
        while not self._try_lock(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigTransactionLockTimeout(
                    self.timeout_seconds, self._read_holder_evidence()
                )
            if not waiting_logged:
                logger.warning(
                    "File transaction %s is contended; waiting up to %gs",
                    self.operation,
                    self.timeout_seconds,
                )
                waiting_logged = True
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, self.MAX_BACKOFF_SECONDS)

    def _write_holder_evidence(self) -> None:
        assert self._handle is not None
        evidence = {
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "operation": self.operation,
            "acquired_monotonic": time.monotonic(),
        }
        payload = json.dumps(evidence, sort_keys=True).encode("utf-8")
        # Initialize byte zero only after owning its OS lock. Two Windows
        # contenders may both open a newly-created empty file; writing the
        # sentinel before locking lets the loser hit EACCES instead of wait.
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(b"0\n" + payload)
        self._handle.flush()

    def _read_holder_evidence(self) -> str | None:
        try:
            with self.path.open("rb") as handle:
                handle.seek(2)
                payload = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        operation = payload.get("operation", "unknown")
        pid = payload.get("pid", "unknown")
        thread = payload.get("thread", "unknown")
        return f"operation={operation},pid={pid},thread={thread}"

    def _clear_holder_evidence(self) -> None:
        if self._handle is None:
            return
        self._handle.seek(1)
        self._handle.truncate()
        self._handle.write(b"\n")
        self._handle.flush()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._handle is not None:
                self._clear_holder_evidence()
                FileLease._unlock(self._handle)
                self._handle.close()
                self._handle = None
        finally:
            self._local_lock.release()


class ConfigTransactionLock(AbstractContextManager["ConfigTransactionLock"]):
    """Cross-session file lock for one exact configuration identity."""

    DEFAULT_TIMEOUT_SECONDS = CONFIG_TRANSACTION_WAIT_SECONDS

    def __init__(
        self,
        config_path: Path,
        timeout_seconds: float | None = None,
        *,
        operation: str = "config_write",
    ) -> None:
        self.config_path = config_path.expanduser().resolve(strict=False)
        self.timeout_seconds = (
            self.DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        if self.timeout_seconds <= 0:
            raise ValueError("config transaction lock timeout must be positive")
        self._serial = SerialFileLock(
            self.lock_path(self.config_path),
            timeout_seconds=self.timeout_seconds,
            operation=operation,
        )

    @staticmethod
    def identity_path(config_path: Path) -> Path:
        return config_path.expanduser().resolve(strict=False)

    @classmethod
    def identity_key(cls, config_path: Path) -> str:
        return os.path.normcase(str(cls.identity_path(config_path)))

    @staticmethod
    def lock_path(config_path: Path) -> Path:
        resolved = config_path.expanduser().resolve(strict=False)
        return resolved.parent / ".haniel" / f"{resolved.name}.config.lock"

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._serial.__exit__(exc_type, exc, traceback)
