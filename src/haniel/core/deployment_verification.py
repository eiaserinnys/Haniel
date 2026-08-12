"""Bounded deployment retries independent of recovery policy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from .deployment_command_runner import CommandRunner, CommandSpec
from .deployment_errors import StableDeploymentError
from .release_manifest import RetrySpec

logger = logging.getLogger(__name__)

RetryWaiter = Callable[[float], bool]


def blocking_retry_waiter(delay_seconds: float) -> bool:
    """Wait for a retry delay when no interrupt source is available."""

    time.sleep(delay_seconds)
    return False


def run_with_retry(
    operation: Callable[[], None],
    *,
    operation_label: str,
    policy: RetrySpec,
    retry_waiter: RetryWaiter = blocking_retry_waiter,
    exhausted_error_code: str,
    interrupted_error_code: str,
    remaining_grace: float | None = None,
    retry_if: Callable[[Exception], bool] = lambda _error: True,
) -> float:
    """Run one operation with bounded backoff and return its remaining budget."""

    if remaining_grace is None:
        remaining_grace = policy.total_grace_seconds
    attempt = 1
    while True:
        if retry_waiter(0):
            raise StableDeploymentError(
                interrupted_error_code,
                f"shutdown requested before {operation_label}",
            )
        try:
            operation()
            return remaining_grace
        except Exception as error:
            if not retry_if(error):
                raise
            if attempt >= policy.max_attempts or remaining_grace <= 0:
                raise StableDeploymentError(
                    exhausted_error_code,
                    f"{operation_label} failed after {attempt} attempt(s): {error}",
                ) from error

            delay = min(
                policy.initial_backoff_seconds * (2 ** (attempt - 1)),
                policy.max_backoff_seconds,
                remaining_grace,
            )
            logger.warning(
                "%s failed on attempt %d/%d; retrying in %.1fs: %s",
                operation_label,
                attempt,
                policy.max_attempts,
                delay,
                error,
            )
            if retry_waiter(delay):
                raise StableDeploymentError(
                    interrupted_error_code,
                    f"shutdown requested while waiting to retry {operation_label} "
                    f"after attempt {attempt}",
                ) from error
            remaining_grace -= delay
            attempt += 1


def verify_with_retry(
    commands: Sequence[CommandSpec],
    environment: dict[str, str],
    *,
    command_runner: CommandRunner,
    policy: RetrySpec,
    retry_waiter: RetryWaiter = blocking_retry_waiter,
) -> None:
    """Run each verify command with one shared, bounded backoff budget."""

    remaining_grace = policy.total_grace_seconds
    for command in commands:
        remaining_grace = run_with_retry(
            lambda command=command: command_runner(command, environment),
            operation_label=f"post-start verify command {command.name!r}",
            policy=policy,
            retry_waiter=retry_waiter,
            exhausted_error_code="POST_VERIFY_RETRIES_EXHAUSTED",
            interrupted_error_code="VERIFY_RETRY_INTERRUPTED",
            remaining_grace=remaining_grace,
        )
