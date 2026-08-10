"""Resident ownership, request identity, and stable deploy error contracts."""

from __future__ import annotations

from typing import Any

from .lifecycle_control import LifecycleConflict
from .lifecycle_storage import read_json
from .deployment_errors import stable_deployment_error_code


def deployment_error_code(error: Exception) -> str:
    return stable_deployment_error_code(error)


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
            "REQUEST_IDENTITY_CONFLICT", "request kind is not a deployment"
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
            "REQUEST_IDENTITY_CONFLICT", "deployment request identity changed"
        )
    if payload["kind"] == "runtime-handover":
        if payload.get("target_ref") != target_head:
            raise LifecycleConflict(
                "REQUEST_IDENTITY_CONFLICT", "runtime target identity changed"
            )
        return
    journal = state_store.read(repo_name)
    if (
        journal is None
        or journal.get("request_id") != request_id
        or journal.get("target_head") != target_head
    ):
        raise LifecycleConflict(
            "REQUEST_IDENTITY_CONFLICT",
            "staged target journal identity changed",
        )


def require_resident_owner(
    lifecycle: Any,
    owner_instance: Any,
    request_id: str | None,
) -> None:
    if lifecycle is None or not request_id or not isinstance(owner_instance, str):
        raise LifecycleConflict(
            "LIFECYCLE_OWNER_REQUIRED",
            "manifest deployment requires resident ownership",
        )
    owner = lifecycle.read_active_owner()
    if owner.get("instance_id") != owner_instance:
        raise LifecycleConflict(
            "LIFECYCLE_OWNER_CONFLICT",
            "runner is not the active resident owner",
        )
