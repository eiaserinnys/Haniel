"""Bounded joins for daemon threads during process shutdown."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS = 5.0


def join_thread_with_timeout(
    thread: threading.Thread | None,
    *,
    name: str,
    timeout: float = DEFAULT_THREAD_JOIN_TIMEOUT_SECONDS,
) -> bool:
    """Wait briefly for a worker and let process exit reclaim a stuck daemon."""
    if thread is None or thread is threading.current_thread() or not thread.is_alive():
        return True

    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.warning(
            "%s did not stop within %.1fs; continuing shutdown",
            name,
            timeout,
        )
        return False
    return True
