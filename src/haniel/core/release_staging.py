"""Detached release target preparation before any live checkout mutation."""

from __future__ import annotations

import hashlib
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from .deployment import ReleaseManifest
from .deployment_command_runner import CommandRunner
from .deployment_command_runner import subprocess_command_runner
from .deployment_errors import StableDeploymentError
from .safety_redaction import redact_text


class ReleaseStagingError(StableDeploymentError):
    """A target could not be prepared in a detached staging checkout."""

    def __init__(self, message: str, *, code: str = "PULL_FAILED") -> None:
        super().__init__(code, message)


class ReleaseIdentityError(ReleaseStagingError):
    """The target probe disagrees with immutable handover intent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True)
class StagedRelease:
    path: Path
    target_head: str
    manifest_path: str
    manifest_digest: str
    manifest: ReleaseManifest
    actual_operation: Literal["fresh_install", "upgrade"] | None
    probe_result: dict[str, object] | None


def initial_clone_path(repo_path: Path) -> Path:
    """Return the deterministic sibling path held until first probe succeeds."""
    return repo_path.with_name(f".{repo_path.name}.haniel-initial")


@contextmanager
def stage_release(
    *,
    repo_path: Path,
    staging_root: Path,
    repo_name: str,
    branch: str,
    manifest_path: str,
    request_id: str,
    expected_operation: Literal["fresh_install", "upgrade"],
    command_runner: CommandRunner | None = None,
    service_cwd_resolver: Callable[[ReleaseManifest], Path | None] | None = None,
    service_environment_resolver: (
        Callable[[ReleaseManifest], dict[str, str]] | None
    ) = None,
    target_ref: str | None = None,
) -> Iterator[StagedRelease]:
    """Fetch, inspect, and probe a target without changing the live HEAD."""
    live_head = _git(repo_path, "rev-parse", "HEAD")
    _git(repo_path, "fetch", "origin", branch)
    resolved_ref = target_ref or f"origin/{branch}"
    target_head = _git(repo_path, "rev-parse", resolved_ref)
    stage_path = staging_root / _safe(request_id) / _safe(repo_name)
    if stage_path.exists():
        raise ReleaseStagingError(f"staging path already exists: {stage_path}")
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    added = False
    try:
        _git(repo_path, "worktree", "add", "--detach", str(stage_path), target_head)
        added = True
        resolved_manifest = (stage_path / manifest_path).resolve()
        if not resolved_manifest.is_relative_to(stage_path.resolve()):
            raise ReleaseStagingError(
                f"release manifest escapes staging checkout: {manifest_path}"
            )
        manifest_bytes = resolved_manifest.read_bytes()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = ReleaseManifest.model_validate_json(manifest_bytes)
        actual_operation = None
        probe_payload: dict[str, object] | None = None
        provenance_probe = (
            manifest.migration.provenance_probe if manifest.migration else None
        )
        if provenance_probe is not None:
            stage_runner = command_runner or subprocess_command_runner(stage_path)
            environment = {
                "HANIEL_EXPECTED_DATABASE_OPERATION": expected_operation,
                "HANIEL_TARGET_HEAD": target_head,
                "HANIEL_MANIFEST_DIGEST": manifest_digest,
                "HANIEL_STAGING_PROBE": "1",
            }
            if service_cwd_resolver is not None:
                service_cwd = service_cwd_resolver(manifest)
                if service_cwd is not None:
                    environment["HANIEL_SERVICE_CWD"] = str(service_cwd)
            if service_environment_resolver is not None:
                environment.update(service_environment_resolver(manifest))
            stage_runner(provenance_probe.prepare, environment)
            result = stage_runner(provenance_probe.probe, environment)
            probe_payload = result.json_data if result is not None else None
            actual_operation = _validate_probe(
                probe_payload,
                expected_operation=expected_operation,
                target_head=target_head,
                manifest_digest=manifest_digest,
            )
        if _git(repo_path, "rev-parse", "HEAD") != live_head:
            raise ReleaseStagingError("live HEAD changed during detached staging")
        yield StagedRelease(
            path=stage_path,
            target_head=target_head,
            manifest_path=manifest_path,
            manifest_digest=manifest_digest,
            manifest=manifest,
            actual_operation=actual_operation,
            probe_result=probe_payload,
        )
    finally:
        if added:
            _git(repo_path, "worktree", "remove", "--force", str(stage_path))
            _git(repo_path, "worktree", "prune")
        if _git(repo_path, "rev-parse", "HEAD") != live_head:
            raise ReleaseStagingError("live HEAD changed while cleaning staging")


def _validate_probe(
    payload: dict[str, object] | None,
    *,
    expected_operation: Literal["fresh_install", "upgrade"],
    target_head: str,
    manifest_digest: str,
) -> Literal["fresh_install", "upgrade"]:
    if not isinstance(payload, dict):
        raise ReleaseIdentityError("PROVENANCE_PROBE_FAILED", "JSON object required")
    operation = payload.get("operation")
    if operation not in ("fresh_install", "upgrade"):
        raise ReleaseIdentityError("PROVENANCE_PROBE_FAILED", "invalid operation")
    if operation != expected_operation:
        raise ReleaseIdentityError(
            "OPERATION_MISMATCH", f"expected {expected_operation}, got {operation}"
        )
    if payload.get("target_head") != target_head:
        raise ReleaseIdentityError(
            "TARGET_IDENTITY_MISMATCH", "probe target_head does not match target"
        )
    if payload.get("manifest_digest") != manifest_digest:
        raise ReleaseIdentityError(
            "MANIFEST_IDENTITY_MISMATCH",
            "probe manifest_digest does not match manifest",
        )
    return operation


def _git(path: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except subprocess.CalledProcessError as error:
        detail = redact_text((error.stderr or error.stdout or "").strip())
        raise ReleaseStagingError(
            f"git {' '.join(args)} failed for {path}: {detail}",
            code="PULL_FAILED",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseStagingError(
            f"git {' '.join(args)} timed out for {path}",
            code="PULL_TIMEOUT",
        ) from error
    return result.stdout.strip()


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
