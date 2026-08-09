"""Single resident-owned handover boundary for every manifest release caller."""

from __future__ import annotations

import subprocess
import sys
import time
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .deployment import DeploymentError, DeploymentStateStore
from .git import activate_repo_target, get_head, reset_repo_to
from .handover_result import (
    HandoverResult,
    Operation,
    build_handover_result,
    handover_error_code,
)
from .handover_config import (
    BoundServiceEnvironment,
    handover_config_digest,
    require_handover_config_digest,
)
from .lifecycle_control import LifecycleConflict, LifecycleControl
from .release_staging import (
    ReleaseIdentityError,
    StagedRelease,
    initial_clone_path,
    stage_release,
)
from .release_manifest import resolve_manifest_service_environment
from .runner_deployment import run_manifest_deployment
from .safety_redaction import bounded_redact_text

if TYPE_CHECKING:
    from .runner import ServiceRunner


def _runner_config_guard(function):
    """Keep one resident handover on a single applied config snapshot."""

    @wraps(function)
    def guarded(runner, *args, **kwargs):
        with runner._config_reload_lock:
            return function(runner, *args, **kwargs)

    return guarded


def execute_manifest_handover_once(
    config_path: Path,
    repo_name: str,
    target_ref: str,
    expected_operation: Operation,
    request_id: str,
    start_owner: bool,
    wait_timeout: float,
) -> HandoverResult:
    """Submit one immutable request to the resident owner and await terminal ack."""
    control = LifecycleControl(config_path)
    config_digest = handover_config_digest(config_path)
    payload = {
        "kind": "handover",
        "repo": repo_name,
        "target_ref": target_ref,
        "expected_operation": expected_operation,
        "config_digest": config_digest,
    }
    try:
        control.read_active_owner()
    except LifecycleConflict as owner_error:
        if expected_operation == "upgrade":
            raise LifecycleConflict(
                "LIFECYCLE_OWNER_REQUIRED: upgrade requires an existing resident owner"
            ) from owner_error
        if not start_owner:
            raise LifecycleConflict(
                "LIFECYCLE_OWNER_MISSING: fresh_install requires --start-owner"
            ) from owner_error
        control.submit_request(request_id, payload)
        try:
            _start_resident_owner(config_path, request_id)
            _wait_for_owner(control, wait_timeout)
        except Exception as error:
            control.cancel_request(
                request_id,
                code="OWNER_START_FAILED",
                message="resident owner failed to start",
            )
            raise RuntimeError(
                "OWNER_START_FAILED: resident owner failed to start"
            ) from error
    else:
        control.submit_request(request_id, payload)
    try:
        result = _wait_for_terminal(control, request_id, wait_timeout)
    except TimeoutError as error:
        try:
            control.cancel_request(
                request_id,
                code="REQUEST_TIMEOUT",
                message="request did not reach terminal state before timeout",
            )
        except LifecycleConflict as cancel_error:
            raise TimeoutError(
                f"REQUEST_IN_PROGRESS: request {request_id} was accepted but did not "
                "reach terminal state"
            ) from cancel_error
        raise TimeoutError(
            f"REQUEST_TIMEOUT: request {request_id} did not reach terminal state"
        ) from error
    terminal = result["terminal"]
    if terminal.get("schema_version") == "haniel.lifecycle.cancelled.v1":
        error = terminal.get("error") or {}
        code = str(error.get("code", "REQUEST_CANCELLED"))
        raise RuntimeError(f"{code}: lifecycle request was cancelled")
    return HandoverResult(**terminal)


def stop(
    config_path: Path,
    *,
    expected_instance: str,
    wait_timeout: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Public stop(expected_instance) boundary; never targets a PID directly."""
    control = LifecycleControl(config_path)
    resolved_request = request_id or f"stop-{uuid4()}"
    control.request_stop(
        expected_instance=expected_instance, request_id=resolved_request
    )
    return _wait_for_terminal(control, resolved_request, wait_timeout)["terminal"]


def _resolve_bound_manifest_environment(
    runner: "ServiceRunner",
    repo_name: str,
    affected: list[str],
    manifest: Any,
    config_digest: str | None,
    service_environment_bindings: dict[str, BoundServiceEnvironment] | None = None,
) -> dict[str, str]:
    if manifest.requires_service_env_file and config_digest is None:
        raise ReleaseIdentityError(
            "CONFIG_DIGEST_REQUIRED: manifest service environment requires "
            "a config-bound detached probe"
        )
    if config_digest is not None:
        assert runner.config_path is not None
        require_handover_config_digest(runner.config_path, config_digest)
    binding = (
        service_environment_bindings.get(manifest.environment_service)
        if service_environment_bindings is not None
        and manifest.environment_service is not None
        else None
    )
    if (
        config_digest is not None
        and manifest.requires_service_env_file
        and binding is None
    ):
        raise ValueError(
            "SERVICE_ENV_FILE_CHANGED: request config has no bound service env file"
        )
    return resolve_manifest_service_environment(
        runner,
        repo_name,
        affected,
        manifest.environment_service,
        requires_env_file=manifest.requires_service_env_file,
        expected_env_path=binding.path if binding is not None else None,
        expected_env_sha256=binding.sha256 if binding is not None else None,
    ).child_environment()


@_runner_config_guard
def probe_manifest_target(
    runner: "ServiceRunner",
    repo_name: str,
    *,
    target_ref: str,
    expected_operation: Operation,
    request_id: str,
    source_repo_path: Path | None = None,
    config_digest: str | None = None,
    service_environment_bindings: dict[str, BoundServiceEnvironment] | None = None,
) -> StagedRelease:
    """Run and clean a detached probe, returning immutable target evidence."""
    state = runner._repo_states[repo_name]
    manifest_path = state.config.release_manifest
    if manifest_path is None:
        raise ValueError(f"release manifest is not configured for {repo_name}")
    repo_path = source_repo_path or (runner.config_dir / state.config.path).resolve()
    affected = runner.get_affected_services(repo_name)
    with stage_release(
        repo_path=repo_path,
        staging_root=runner.config_dir / ".haniel" / "staging",
        repo_name=repo_name,
        branch=state.config.branch,
        manifest_path=manifest_path,
        request_id=request_id,
        expected_operation=expected_operation,
        target_ref=target_ref,
        service_environment_resolver=lambda manifest: (
            _resolve_bound_manifest_environment(
                runner,
                repo_name,
                affected,
                manifest,
                config_digest,
                service_environment_bindings,
            )
        ),
    ) as staged:
        if staged.manifest.requires_service_env_file and config_digest is None:
            raise ReleaseIdentityError(
                "CONFIG_DIGEST_REQUIRED: manifest service environment requires "
                "a config-bound detached probe"
            )
        return staged


def execute_owner_handover(
    runner: "ServiceRunner",
    *,
    control: LifecycleControl,
    repo_name: str,
    target_ref: str,
    expected_operation: Operation,
    request_id: str,
    config_digest: str | None = None,
) -> HandoverResult:
    """Execute target staging, live checkout, deployment, and terminal result."""
    with (
        control.acquire_deployment(repo_name, request_id) as lease,
        runner._config_reload_lock,
    ):
        existing = control.read_result(request_id)
        if lease.attached and existing.get("terminal"):
            return HandoverResult(**existing["terminal"])

        reload_plan = (
            runner.prepare_handover_config(repo_name, config_digest)
            if config_digest is not None
            else None
        )

        control.ack(
            request_id,
            "accepted",
            {
                "repo": repo_name,
                "owner_instance": getattr(runner, "lifecycle_instance_id", None),
                "config_digest": config_digest,
            },
        )
        previous_head: str | None = None
        staged: StagedRelease | None = None
        live_changed = False
        try:
            state = runner._repo_states[repo_name]
            repo_path = (runner.config_dir / state.config.path).resolve()
            deferred_clone = initial_clone_path(repo_path)
            initial_install = not repo_path.exists()
            if initial_install:
                if expected_operation != "fresh_install":
                    raise RuntimeError(
                        "PULL_FAILED: upgrade requires an existing live checkout"
                    )
                if not deferred_clone.exists():
                    raise RuntimeError(
                        "PULL_FAILED: deferred initial clone does not exist"
                    )
                source_repo_path = deferred_clone
            else:
                previous_head = get_head(repo_path)
                source_repo_path = repo_path
            manifest_identity = state.config.release_manifest
            if manifest_identity is None:
                raise ValueError(f"release manifest is not configured for {repo_name}")
            journal_store = DeploymentStateStore(
                runner.config_dir / ".haniel" / "deployments"
            )
            journal_attempt_id = journal_store.begin_handover(
                repo_name,
                previous_head=previous_head or "absent",
                target_ref=target_ref,
                manifest_identity=manifest_identity,
                request_id=request_id,
                expected_operation=expected_operation,
                config_digest=config_digest,
            )
            staged = probe_manifest_target(
                runner,
                repo_name,
                target_ref=target_ref,
                expected_operation=expected_operation,
                request_id=request_id,
                source_repo_path=source_repo_path,
                config_digest=config_digest,
                service_environment_bindings=(
                    reload_plan.service_environment_map()
                    if reload_plan is not None
                    else None
                ),
            )
            journal_store.bind_handover_target(
                repo_name,
                request_id=request_id,
                target_head=staged.target_head,
                release_id=staged.manifest.release_id,
                manifest_digest=staged.manifest_digest,
            )
            if config_digest is not None:
                require_handover_config_digest(runner.config_path, config_digest)
            if initial_install:
                deferred_clone.replace(repo_path)
                live_changed = True
            try:
                activate_repo_target(
                    repo_path,
                    staged.target_head,
                    strategy=getattr(state.config, "pull_strategy", None) or "merge",
                )
            except Exception:
                if (
                    expected_operation == "upgrade"
                    and previous_head is not None
                    and get_head(repo_path) != previous_head
                ):
                    reset_repo_to(repo_path, previous_head)
                    state.last_head = get_head(repo_path)
                raise
            live_changed = True
            actual_head = get_head(repo_path)
            if actual_head != staged.target_head:
                raise RuntimeError(
                    "PULL_FAILED: live checkout does not match the staged target"
                )
            state.last_head = actual_head
            affected = (
                list(reload_plan.new_affected)
                if reload_plan is not None
                else runner.get_affected_services(repo_name)
            )
            run_manifest_deployment(
                runner,
                repo_name,
                affected,
                previous_head or "absent",
                journal_attempt_id=journal_attempt_id,
                expected_operation=expected_operation,
                request_id=request_id,
                quiescence_callback=lambda receipt: control.ack(
                    request_id, "quiesced", receipt
                ),
                quiesce_services=(
                    list(reload_plan.quiesce_services)
                    if reload_plan is not None
                    else affected
                ),
                config_digest=config_digest,
                service_environment_bindings=(
                    reload_plan.service_environment_map()
                    if reload_plan is not None
                    else None
                ),
            )
            result = build_handover_result(
                control,
                request_id=request_id,
                operation=expected_operation,
                phase="success",
                previous_head=previous_head,
                target_head=staged.target_head,
                release_id=staged.manifest.release_id,
                ok=True,
                recovered=False,
                retryable=False,
                config_digest=config_digest,
            )
        except Exception as error:
            recovered = isinstance(error, DeploymentError) and error.recovered
            if (
                live_changed
                and expected_operation == "upgrade"
                and not isinstance(error, DeploymentError)
            ):
                assert previous_head is not None
                try:
                    reset_repo_to(repo_path, previous_head)
                    restored_head = get_head(repo_path)
                    if restored_head != previous_head:
                        raise RuntimeError("previous HEAD equality verification failed")
                    state.last_head = restored_head
                    recovered = True
                except Exception as recovery_error:
                    error = RuntimeError(
                        f"RECOVERY_FAILED: {error}; repo recovery failed: {recovery_error}"
                    )
                    recovered = False
            result = build_handover_result(
                control,
                request_id=request_id,
                operation=expected_operation,
                phase="failed",
                previous_head=previous_head,
                target_head=staged.target_head if staged else None,
                release_id=staged.manifest.release_id if staged else None,
                ok=False,
                recovered=recovered,
                retryable=not live_changed,
                error={
                    "code": handover_error_code(error),
                    "message": bounded_redact_text(str(error)),
                },
                config_digest=config_digest,
            )
        control.ack(request_id, "terminal", result.to_dict())
        return result


def _wait_for_owner(control: LifecycleControl, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            control.read_active_owner()
            return
        except LifecycleConflict:
            time.sleep(0.05)
    raise TimeoutError("resident owner did not start before timeout")


def _wait_for_terminal(
    control: LifecycleControl, request_id: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        result = control.read_result(request_id)
        if result.get("terminal"):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    raise TimeoutError(f"request {request_id} did not reach terminal state")


def _start_resident_owner(config_path: Path, request_id: str) -> None:
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "haniel",
            "run",
            "--foreground",
            str(config_path),
            "--initial-request-id",
            request_id,
        ],
        cwd=config_path.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
