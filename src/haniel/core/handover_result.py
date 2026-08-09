"""Stable bounded result envelope for manifest handovers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .deployment import DeploymentError
from .lifecycle_control import LifecycleControl
from .release_staging import ReleaseStagingError

Operation = Literal["fresh_install", "upgrade"]


@dataclass(frozen=True)
class HandoverResult:
    schema_version: str
    ok: bool
    request_id: str
    release_id: str | None
    operation: Operation
    phase: str
    previous_head: str | None
    target_head: str | None
    journal_path: str | None
    backup_path: str | None
    recovered: bool
    retryable: bool
    error: dict[str, str] | None = None
    config_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_handover_result(
    control: LifecycleControl,
    *,
    request_id: str,
    operation: Operation,
    phase: str,
    previous_head: str | None,
    target_head: str | None,
    release_id: str | None,
    ok: bool,
    recovered: bool,
    retryable: bool,
    error: dict[str, str] | None = None,
    config_digest: str | None = None,
) -> HandoverResult:
    repo = None
    request_path = control.request_path(request_id)
    if request_path.exists():
        payload = json.loads(request_path.read_text(encoding="utf-8"))["payload"]
        repo = payload.get("repo")
    journal = (
        control.config_dir / ".haniel" / "deployments" / f"{repo}.json"
        if repo
        else None
    )
    return HandoverResult(
        schema_version="haniel.handover.result.v1",
        ok=ok,
        request_id=request_id,
        release_id=release_id,
        operation=operation,
        phase=phase,
        previous_head=previous_head,
        target_head=target_head,
        journal_path=str(journal) if journal else None,
        backup_path=None,
        recovered=recovered,
        retryable=retryable,
        error=error,
        config_digest=config_digest,
    )


def handover_error_code(error: Exception) -> str:
    if isinstance(error, DeploymentError) and error.recovery_error:
        return "RECOVERY_FAILED"
    message = str(error)
    for code in (
        "OPERATION_MISMATCH",
        "CONFIG_DIGEST_MISMATCH",
        "SERVICE_ENV_FILE_CHANGED",
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
        "LIFECYCLE_OWNER_REQUIRED",
        "LIFECYCLE_OWNER_MISSING",
        "LIFECYCLE_OWNER_CONFLICT",
        "REQUEST_IDENTITY_CONFLICT",
        "REQUEST_IN_PROGRESS",
        "DEPLOYMENT_LEASE_CONFLICT",
        "RUNTIME_OWNER_LOST",
        "REQUEST_TIMEOUT",
        "OWNER_START_FAILED",
        "CONFIG_DIGEST_REQUIRED",
        "CONFIG_RELOAD_FAILED",
        "CONFIG_RELOAD_UNSAFE",
        "SERVICE_ENV_FILE_REQUIRED",
        "SERVICE_ENV_FILE_INVALID",
    ):
        if code in message:
            return code
    if isinstance(error, ReleaseStagingError):
        return "PULL_FAILED"
    return "HANDOVER_FAILED"


def request_error_code(error: Exception) -> str:
    message = str(error)
    if "MALFORMED_REQUEST" in message:
        return "MALFORMED_REQUEST"
    if "REQUEST_IDENTITY_CONFLICT" in message:
        return "REQUEST_IDENTITY_CONFLICT"
    phase_code = handover_error_code(error)
    if phase_code != "HANDOVER_FAILED":
        return phase_code
    return "REQUEST_HANDLER_FAILED"
