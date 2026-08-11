"""Bounded post-start verification independent of deployment recovery policy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from .deployment_command_runner import CommandRunner, CommandSpec
from .deployment_errors import StableDeploymentError
from .release_manifest import VerifyRetrySpec

logger = logging.getLogger(__name__)

RetryWaiter = Callable[[float], bool]


def blocking_retry_waiter(delay_seconds: float) -> bool:
    """Wait for a retry delay when no interrupt source is available."""

    time.sleep(delay_seconds)
    return False


def verify_with_retry(
    commands: Sequence[CommandSpec],
    environment: dict[str, str],
    *,
    command_runner: CommandRunner,
    policy: VerifyRetrySpec,
    retry_waiter: RetryWaiter = blocking_retry_waiter,
) -> None:
    """Run each verify command with one shared, bounded backoff budget."""

    remaining_grace = policy.total_grace_seconds
    for command in commands:
        attempt = 1
        while True:
            if retry_waiter(0):
                raise StableDeploymentError(
                    "VERIFY_RETRY_INTERRUPTED",
                    f"shutdown requested before verify command {command.name!r}",
                )
            try:
                command_runner(command, environment)
                break
            except Exception as error:
                if attempt >= policy.max_attempts or remaining_grace <= 0:
                    raise StableDeploymentError(
                        "POST_VERIFY_RETRIES_EXHAUSTED",
                        f"verify command {command.name!r} failed after "
                        f"{attempt} attempt(s): {error}",
                    ) from error

                delay = min(
                    policy.initial_backoff_seconds * (2 ** (attempt - 1)),
                    policy.max_backoff_seconds,
                    remaining_grace,
                )
                logger.warning(
                    "Post-start verify command %s failed on attempt %d/%d; "
                    "retrying in %.1fs: %s",
                    command.name,
                    attempt,
                    policy.max_attempts,
                    delay,
                    error,
                )
                if retry_waiter(delay):
                    raise StableDeploymentError(
                        "VERIFY_RETRY_INTERRUPTED",
                        f"shutdown requested while waiting to retry verify command "
                        f"{command.name!r} after attempt {attempt}",
                    ) from error
                remaining_grace -= delay
                attempt += 1
