"""Per-service lifecycle locks shared by commands and deployments.

One reentrant lock exists per service name. Reentrancy lets a single thread
nest runner-level sections (restart inside stop+start, reload inside a config
transaction) with the process-manager section that actually spawns or stops
the child. Names are acquired in sorted order so callers that hold several
services at once cannot deadlock each other.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterable, Iterator


class ServiceLockRegistry:
    """Own the per-service reentrant locks for one runner process."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _lock_for(self, name: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._locks[name] = lock
            return lock

    def acquire(self, names: Iterable[str] | str) -> list[threading.RLock]:
        """Acquire every named lock in sorted order and return the held locks."""
        if isinstance(names, str):
            names = (names,)
        held: list[threading.RLock] = []
        try:
            for name in sorted(set(names)):
                lock = self._lock_for(name)
                lock.acquire()
                held.append(lock)
        except BaseException:
            self.release(held)
            raise
        return held

    @staticmethod
    def release(held: list[threading.RLock]) -> None:
        """Release locks taken by :meth:`acquire`, newest first."""
        for lock in reversed(held):
            lock.release()

    @contextmanager
    def hold(self, names: Iterable[str] | str) -> Iterator[None]:
        """Hold one or more service locks for the duration of the block."""
        held = self.acquire(names)
        try:
            yield
        finally:
            self.release(held)

    @contextmanager
    def try_hold(self, name: str) -> Iterator[bool]:
        """Hold one service lock only if it is free for this thread."""
        lock = self._lock_for(name)
        if not lock.acquire(blocking=False):
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
