"""Detached release target preparation before any live checkout mutation."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Iterable, Iterator, Literal

from ..defaults import DEFAULT_GIT_TIMEOUT
from .deployment import ReleaseManifest
from .deployment_command_runner import CommandRunner
from .deployment_command_runner import run_bounded_process_tree
from .deployment_command_runner import subprocess_command_runner
from .deployment_errors import StableDeploymentError
from .safety_redaction import redact_text

logger = logging.getLogger(__name__)


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


def sweep_staging_root(
    *,
    staging_root: Path,
    repo_paths: Iterable[Path],
    keep_request_ids: Collection[str],
) -> tuple[int, int]:
    """Remove staging request folders that no in-flight request owns.

    Returns the number of removed and kept request folders. A folder that
    cannot be removed completely is counted as kept and only warns.
    """
    repos = [Path(repo_path) for repo_path in repo_paths]
    owners = _registered_worktree_owners(repos)
    keep = {_safe(request_id) for request_id in keep_request_ids}
    root = Path(_path_key(staging_root))
    normalized_keep = {os.path.normcase(request_id) for request_id in keep}
    for worktree, repo_path in owners.items():
        path = Path(worktree)
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if (
            relative.parts
            and os.path.normcase(relative.parts[0]) not in normalized_keep
            and not path.exists()
        ):
            _remove_worktree(repo_path, path)
    removed = 0
    kept = 0
    for request_root in sorted(staging_root.iterdir()) if staging_root.is_dir() else []:
        if request_root.is_symlink() or not request_root.is_dir():
            continue
        if request_root.name in keep:
            kept += 1
            continue
        _clear_request_root(request_root, owners)
        if request_root.exists():
            kept += 1
        else:
            removed += 1
    for repo_path in repos:
        _prune_worktrees(repo_path)
    return removed, kept


def _clear_request_root(request_root: Path, owners: dict[str, Path]) -> None:
    for entry in list(request_root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            try:
                entry.unlink()
            except OSError as error:
                logger.warning("Cannot remove staging entry %s: %s", entry, error)
            continue
        owner = owners.get(_path_key(entry))
        if owner is not None and _remove_worktree(owner, entry):
            continue
        _remove_tree(entry)
    try:
        request_root.rmdir()
    except OSError:
        # A failed child removal already logged its warning.
        pass


def _cleanup_stage(repo_path: Path, stage_path: Path) -> None:
    """Release one request's staging checkout and its empty request folder."""
    if _path_key(stage_path) in _registered_worktree_owners([repo_path]):
        _remove_worktree(repo_path, stage_path)
    elif stage_path.exists():
        _remove_tree(stage_path)
    _prune_worktrees(repo_path)
    try:
        stage_path.parent.rmdir()
    except OSError:
        # Other repositories still staged under this request, or removal failed.
        pass


def _registered_worktree_owners(repo_paths: Iterable[Path]) -> dict[str, Path]:
    owners: dict[str, Path] = {}
    for repo_path in repo_paths:
        for path in _registered_worktrees(repo_path):
            owners[_path_key(path)] = repo_path
    return owners


def _registered_worktrees(repo_path: Path) -> list[Path]:
    try:
        output = _git(repo_path, "worktree", "list", "--porcelain")
    except ReleaseStagingError as error:
        logger.warning("Cannot list worktrees for %s: %s", repo_path, error)
        return []
    return [
        Path(line[len("worktree ") :])
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _remove_worktree(repo_path: Path, path: Path) -> bool:
    try:
        _git(repo_path, "worktree", "remove", "--force", "--force", str(path))
        return True
    except ReleaseStagingError as error:
        logger.warning("Cannot remove staging worktree %s: %s", path, error)
        return False


def _prune_worktrees(repo_path: Path) -> None:
    try:
        _git(repo_path, "worktree", "prune")
    except ReleaseStagingError as error:
        logger.warning("Cannot prune worktrees for %s: %s", repo_path, error)


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as error:
        logger.warning("Cannot remove staging directory %s: %s", path, error)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


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
    staging_checkout_timeout: int = DEFAULT_GIT_TIMEOUT,
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
    try:
        _git(
            repo_path,
            "worktree",
            "add",
            "--detach",
            str(stage_path),
            target_head,
            timeout=staging_checkout_timeout,
            use_process_tree=True,
        )
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
        _cleanup_stage(repo_path, stage_path)
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


def _git(
    path: Path,
    *args: str,
    timeout: int = DEFAULT_GIT_TIMEOUT,
    use_process_tree: bool = False,
) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        if use_process_tree:
            result = run_bounded_process_tree(
                ["git", *args], cwd=path, env=env, timeout=timeout, encoding="utf-8"
            )
        else:
            result = subprocess.run(
                ["git", *args],
                cwd=path,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
    except subprocess.CalledProcessError as error:
        detail = redact_text((error.stderr or error.stdout or "").strip())
        raise ReleaseStagingError(
            f"git {' '.join(args)} failed for {path}: {detail}",
            code="PULL_FAILED",
        ) from error
    except subprocess.TimeoutExpired as error:
        cleanup_detail = getattr(error, "cleanup_detail", None)
        cleanup = f"; cleanup: {cleanup_detail}" if cleanup_detail else ""
        raise ReleaseStagingError(
            f"git {' '.join(args)} timed out after {timeout}s for {path}{cleanup}",
            code="PULL_TIMEOUT",
        ) from error
    return result.stdout.strip()


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
