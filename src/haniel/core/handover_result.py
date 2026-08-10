"""Stable bounded result envelope for manifest handovers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .deployment_errors import stable_deployment_error_code
from .lifecycle_control import LifecycleControl

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
    return stable_deployment_error_code(error)


def request_error_code(error: Exception) -> str:
    phase_code = handover_error_code(error)
    if phase_code != "HANDOVER_FAILED":
        return phase_code
    return "REQUEST_HANDLER_FAILED"
