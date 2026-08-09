"""Resident ownership, request identity, and stable deploy error contracts."""

from __future__ import annotations

from typing import Any

from .lifecycle_control import LifecycleConflict
from .lifecycle_storage import read_json


def deployment_error_code(error: Exception) -> str:
    if getattr(error, "recovery_error", None) is not None:
        return "RECOVERY_FAILED"
    message = str(error)
    for code in (
        "OPERATION_MISMATCH",
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
        "LIFECYCLE_OWNER_CONFLICT",
        "DEPLOYMENT_LEASE_CONFLICT",
        "RUNTIME_OWNER_LOST",
        "CONFIG_DIGEST_REQUIRED",
        "CONFIG_DIGEST_MISMATCH",
        "CONFIG_RELOAD_FAILED",
        "CONFIG_RELOAD_UNSAFE",
        "SERVICE_ENV_FILE_REQUIRED",
        "SERVICE_ENV_FILE_INVALID",
        "SERVICE_ENV_FILE_CHANGED",
    ):
        if code in message:
            return code
    return "HANDOVER_FAILED"


def validate_lifecycle_request(
    lifecycle: Any,
    state_store: Any,
    *,
    request_id: str,
    repo_name: str,
    target_head: str,
    expected_operation: str,
    config_digest: str | None = None,
) -> None:
    path = lifecycle.request_path(request_id)
    if not path.exists():
        return
    request = read_json(path)
    payload = request.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") not in {
        "handover",
        "runtime-handover",
    }:
        raise LifecycleConflict(
            "REQUEST_IDENTITY_CONFLICT: request kind is not a deployment"
        )
    stored_config_digest = payload.get("config_digest")
    if (
        payload.get("repo") != repo_name
        or payload.get("expected_operation") != expected_operation
        or (
            (stored_config_digest is not None or config_digest is not None)
            and stored_config_digest != config_digest
        )
    ):
        raise LifecycleConflict(
            "REQUEST_IDENTITY_CONFLICT: deployment request identity changed"
        )
    if payload["kind"] == "runtime-handover":
        if payload.get("target_ref") != target_head:
            raise LifecycleConflict(
                "REQUEST_IDENTITY_CONFLICT: runtime target identity changed"
            )
        return
    journal = state_store.read(repo_name)
    if (
        journal is None
        or journal.get("request_id") != request_id
        or journal.get("target_head") != target_head
    ):
        raise LifecycleConflict(
            "REQUEST_IDENTITY_CONFLICT: staged target journal identity changed"
        )


def require_resident_owner(
    lifecycle: Any,
    owner_instance: Any,
    request_id: str | None,
) -> None:
    if lifecycle is None or not request_id or not isinstance(owner_instance, str):
        raise LifecycleConflict(
            "LIFECYCLE_OWNER_REQUIRED: manifest deployment requires resident ownership"
        )
    owner = lifecycle.read_active_owner()
    if owner.get("instance_id") != owner_instance:
        raise LifecycleConflict(
            "LIFECYCLE_OWNER_CONFLICT: runner is not the active resident owner"
        )
