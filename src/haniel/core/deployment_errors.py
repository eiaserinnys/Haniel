"""Stable, bounded error contracts for release lifecycle boundaries."""

from __future__ import annotations

from collections.abc import Iterator

from .safety_redaction import bounded_redact_text

_ERROR_MESSAGE_MAX_CHARS = 16384

KNOWN_DEPLOYMENT_ERROR_CODES = frozenset(
    {
        "HANDOVER_FAILED",
        "COMMAND_NOT_FOUND",
        "COMMAND_START_FAILED",
        "COMMAND_TIMEOUT",
        "COMMAND_EXIT_NONZERO",
        "COMMAND_RESULT_TOO_LARGE",
        "COMMAND_RESULT_INVALID",
        "PROVENANCE_PROBE_FAILED",
        "OPERATION_MISMATCH",
        "TARGET_IDENTITY_MISMATCH",
        "MANIFEST_IDENTITY_MISMATCH",
        "PULL_TIMEOUT",
        "PULL_FAILED",
        "PREFLIGHT_FAILED",
        "QUIESCENCE_REQUIRED",
        "BACKUP_CREATE_FAILED",
        "BACKUP_VERIFY_FAILED",
        "JOURNAL_GATE_FAILED",
        "APPLY_FAILED",
        "AMBIGUOUS_COMMIT_STATE",
        "POST_VERIFY_FAILED",
        "RECOVERY_FAILED",
        "CONFIG_GENERATION_CHANGED",
        "CONFIG_DIGEST_MISMATCH",
        "CONFIG_DIGEST_REQUIRED",
        "CONFIG_RELOAD_FAILED",
        "CONFIG_RELOAD_UNSAFE",
        "SERVICE_ENV_FILE_REQUIRED",
        "SERVICE_ENV_FILE_INVALID",
        "SERVICE_ENV_FILE_CHANGED",
        "LIFECYCLE_OWNER_REQUIRED",
        "LIFECYCLE_OWNER_MISSING",
        "LIFECYCLE_OWNER_ACTIVE",
        "LIFECYCLE_OWNER_CONFLICT",
        "REQUEST_IDENTITY_CONFLICT",
        "REQUEST_IN_PROGRESS",
        "DEPLOYMENT_LEASE_CONFLICT",
        "RUNTIME_OWNER_LOST",
        "REQUEST_TIMEOUT",
        "OWNER_START_FAILED",
        "MALFORMED_REQUEST",
        "REQUEST_HANDLER_FAILED",
        "UNSUPPORTED_REQUEST_KIND",
        "EXPECTED_INSTANCE_MISMATCH",
        "DATABASE_COMMAND_FAILED",
        "STARTUP_UPDATE_FAILED",
        "POLL_CYCLE_FAILED",
        "AUTO_DEPLOY_FAILED",
        "CONFIG_LOCK_TIMEOUT",
        "CONFIG_SEMANTIC_INVALID",
        "CONFIG_IDENTITY_DRIFT",
        "CONFIG_WRITE_FAILED",
        "RELEASE_IDENTITY_UNAVAILABLE",
        "RELEASE_ACTIVATION_REQUIRED",
    }
)


class StableDeploymentError(RuntimeError):
    """Operational release failure with a durable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in KNOWN_DEPLOYMENT_ERROR_CODES:
            raise ValueError(f"unregistered deployment error code: {code}")
        self.code = code
        self.detail = bounded_redact_text(message, max_chars=_ERROR_MESSAGE_MAX_CHARS)
        super().__init__(f"{code}: {self.detail}")


def _error_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current
        recovery = getattr(current, "recovery_error", None)
        if isinstance(recovery, BaseException):
            pending.append(recovery)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def stable_deployment_error_code(
    error: BaseException,
    *,
    default: str = "HANDOVER_FAILED",
) -> str:
    """Return one stable code from typed metadata without parsing prose."""

    direct_code = getattr(error, "code", None)
    if isinstance(direct_code, str) and direct_code:
        return direct_code
    if getattr(error, "recovery_error", None) is not None:
        return "RECOVERY_FAILED"
    chain = tuple(_error_chain(error))
    for current in chain[1:]:
        code = getattr(current, "code", None)
        if isinstance(code, str) and code:
            return code
    return default
