"""Atomic activation of repository-provided release manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .model import HanielConfig, load_config
from .validators import ConfigSemanticError, require_valid_config

DEFAULT_RELEASE_MANIFEST = "deploy/release-manifest.json"
RELEASE_GIT_INSPECTION_TIMEOUT_SECONDS = 60


class ReleaseManifestActivationRequired(RuntimeError):
    """A remote release contract exists but Haniel cannot activate it safely."""

    code = "RELEASE_ACTIVATION_REQUIRED"


class ReleaseActivationSemanticError(ReleaseManifestActivationRequired):
    """A release activation candidate is schema-valid but semantically invalid."""

    code = "CONFIG_SEMANTIC_INVALID"


class ReleaseActivationIdentityDrift(ReleaseManifestActivationRequired):
    """The exact source or candidate identity changed after planning."""

    code = "CONFIG_IDENTITY_DRIFT"


class ReleaseActivationWriteError(ReleaseManifestActivationRequired):
    """Activation write failed after validation and was rolled back."""

    code = "CONFIG_WRITE_FAILED"


class ReleaseActivationGitError(ReleaseManifestActivationRequired):
    """The fetched release identity could not be inspected deterministically."""

    code = "RELEASE_IDENTITY_UNAVAILABLE"


@dataclass(frozen=True)
class ReleaseManifestActivationPlan:
    config_path: Path
    repo_name: str
    release_manifest: str
    config_sha256: str
    candidate_sha256: str
    validator_revision: str
    validator_error_count: int
    repo_head: str
    manifest_sha256: str
    changed: bool


@dataclass(frozen=True)
class ReleaseManifestActivationResult:
    changed: bool
    config_sha256: str
    backup_path: Path | None


def discover_remote_release_manifest(
    repo_path: Path,
    branch: str,
    manifest_path: str = DEFAULT_RELEASE_MANIFEST,
) -> str | None:
    """Return a validated conventional manifest path from the fetched remote ref."""
    from ..core.release_manifest import ReleaseManifest

    # Unit fixtures may use a sentinel .git directory while mocking fetch/pull.
    # A real fetch cannot succeed for such a directory, so it is not a production
    # fail-open path.
    if not (repo_path / ".git" / "HEAD").exists():
        return None

    ref = f"origin/{branch}"
    verified = _run_git_inspection(repo_path, "rev-parse", "--verify", ref)
    if verified.returncode != 0:
        # No remote target means there is nothing safe to pull. The following
        # real pull will fail as before; unit fixtures may mock that boundary.
        return None
    listed = _run_git_inspection(
        repo_path, "ls-tree", "-r", "--name-only", ref, "--", manifest_path
    )
    if listed.returncode != 0:
        raise ReleaseActivationGitError(
            f"failed to inspect release manifest at {ref}: {listed.stderr.strip()}"
        )
    if manifest_path not in listed.stdout.splitlines():
        return None

    shown = _run_git_inspection(repo_path, "show", f"{ref}:{manifest_path}")
    if shown.returncode != 0:
        raise ReleaseActivationGitError(
            f"failed to read release manifest at {ref}: {shown.stderr.strip()}"
        )
    try:
        ReleaseManifest.model_validate_json(shown.stdout)
    except Exception as error:
        raise ReleaseManifestActivationRequired(
            f"remote release manifest is invalid: {ref}:{manifest_path}: {error}"
        ) from error
    return manifest_path


def _run_git_inspection(
    repo_path: Path, *arguments: str
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=RELEASE_GIT_INSPECTION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseActivationGitError(
            "release identity inspection did not complete"
        ) from error


def plan_release_manifest_activation(
    config_path: Path,
    repo_name: str,
    manifest_path: str,
) -> ReleaseManifestActivationPlan:
    """Validate one semantic config change without writing it."""
    raw = config_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    config = HanielConfig.model_validate(_load_mapping(raw))
    _require_activation_semantics(config)
    if repo_name not in config.repos:
        raise ReleaseManifestActivationRequired(
            f"repository is absent from Haniel config: {repo_name}"
        )
    current = config.repos[repo_name].release_manifest
    if current not in (None, manifest_path):
        raise ReleaseManifestActivationRequired(
            f"repository {repo_name} already uses a different release manifest: {current}"
        )
    try:
        repo_head, manifest_sha256 = _remote_release_identity(
            config_path, config, repo_name, manifest_path
        )
    except Exception as error:
        from ..core.git import GitError

        if isinstance(error, GitError):
            raise ReleaseActivationGitError(
                f"failed to inspect release identity for repository: {repo_name}"
            ) from error
        raise

    payload = _load_mapping(raw)
    repo_payload = payload.get("repos", {}).get(repo_name)
    if not isinstance(repo_payload, dict):
        raise ReleaseManifestActivationRequired(
            f"repository config is not a mapping: {repo_name}"
        )
    repo_payload["release_manifest"] = manifest_path
    candidate, rendered = _validated_candidate(payload)
    evidence = _require_activation_semantics(candidate)
    return ReleaseManifestActivationPlan(
        config_path=config_path,
        repo_name=repo_name,
        release_manifest=manifest_path,
        config_sha256=digest,
        candidate_sha256=hashlib.sha256(rendered).hexdigest(),
        validator_revision=evidence.revision,
        validator_error_count=evidence.error_count,
        repo_head=repo_head,
        manifest_sha256=manifest_sha256,
        changed=current != manifest_path,
    )


def activate_release_manifest(
    config_path: Path,
    repo_name: str | None = None,
    manifest_path: str | None = None,
    *,
    expected_sha256: str | None = None,
    plan: ReleaseManifestActivationPlan | None = None,
) -> ReleaseManifestActivationResult:
    """Apply a one-field config migration with backup and compare-and-swap."""
    if plan is None:
        if repo_name is None or manifest_path is None or expected_sha256 is None:
            raise TypeError(
                "repo_name, manifest_path, and expected_sha256 are required without plan"
            )
        plan = plan_release_manifest_activation(config_path, repo_name, manifest_path)
        if plan.config_sha256 != expected_sha256:
            raise ReleaseActivationIdentityDrift(
                "Haniel config changed after validation; rerun the activation check"
            )
    elif plan.config_path != config_path:
        raise ReleaseActivationIdentityDrift(
            "activation plan targets a different config path"
        )

    preflight = config_path.read_bytes()
    if hashlib.sha256(preflight).hexdigest() != plan.config_sha256:
        raise ReleaseActivationIdentityDrift(
            "Haniel config changed before activation identity revalidation"
        )
    preflight_source = HanielConfig.model_validate(_load_mapping(preflight))
    try:
        repo_head, manifest_sha256 = _remote_release_identity(
            config_path,
            preflight_source,
            plan.repo_name,
            plan.release_manifest,
        )
    except Exception as error:
        raise ReleaseActivationIdentityDrift(
            "remote release identity could not be revalidated"
        ) from error
    if repo_head != plan.repo_head or manifest_sha256 != plan.manifest_sha256:
        raise ReleaseActivationIdentityDrift(
            "remote release identity drifted after activation planning"
        )

    from ..core.service_lifecycle import config_file_transaction

    with config_file_transaction(config_path, operation="release_activation_commit"):
        original = config_path.read_bytes()
        if hashlib.sha256(original).hexdigest() != plan.config_sha256:
            raise ReleaseActivationIdentityDrift(
                "Haniel config changed after validation; stale activation plan"
            )

        source = HanielConfig.model_validate(_load_mapping(original))
        _require_activation_semantics(source)
        source_repo = source.repos.get(plan.repo_name)
        if source_repo is None:
            raise ReleaseActivationIdentityDrift(
                "planned repository disappeared from config"
            )
        current_manifest = source_repo.release_manifest
        if current_manifest not in (None, plan.release_manifest):
            raise ReleaseActivationIdentityDrift(
                "planned repository now uses a different release manifest"
            )
        payload = _load_mapping(original)
        repo_payload = payload.get("repos", {}).get(plan.repo_name)
        if not isinstance(repo_payload, dict):
            raise ReleaseActivationIdentityDrift(
                "planned repository disappeared from config"
            )
        repo_payload["release_manifest"] = plan.release_manifest
        candidate, rendered = _validated_candidate(payload)
        evidence = _require_activation_semantics(candidate)
        if (
            hashlib.sha256(rendered).hexdigest() != plan.candidate_sha256
            or evidence.revision != plan.validator_revision
            or evidence.error_count != plan.validator_error_count
        ):
            raise ReleaseActivationIdentityDrift(
                "activation candidate or semantic validation evidence drifted"
            )
        if config_path.read_bytes() != original:
            raise ReleaseActivationIdentityDrift(
                "Haniel config changed before activation write"
            )
        changed = current_manifest != plan.release_manifest
        if not changed:
            return ReleaseManifestActivationResult(False, plan.config_sha256, None)

        backup_path = config_path.with_name(
            f"{config_path.name}.before-release-manifest-{plan.config_sha256[:12]}.bak"
        )
        if backup_path.exists() and backup_path.read_bytes() != original:
            raise ReleaseManifestActivationRequired(
                f"activation backup already exists with different content: {backup_path}"
            )
        try:
            if not backup_path.exists():
                _atomic_write(backup_path, original, config_path)
            _atomic_write(config_path, rendered, config_path)
            if config_path.read_bytes() != rendered:
                raise OSError("activation target verification failed")
        except Exception as error:
            rollback_error: Exception | None = None
            try:
                if config_path.read_bytes() != original:
                    _atomic_write(config_path, original, config_path)
                if config_path.read_bytes() != original:
                    raise OSError("activation rollback verification failed")
            except Exception as failed_rollback:
                rollback_error = failed_rollback
            message = "release activation write failed; original config restored"
            if rollback_error is not None:
                message = "release activation write and rollback verification failed"
            raise ReleaseActivationWriteError(message) from (rollback_error or error)
        return ReleaseManifestActivationResult(
            True, hashlib.sha256(rendered).hexdigest(), backup_path
        )


def _validated_candidate(payload: dict[str, Any]) -> tuple[HanielConfig, bytes]:
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode(
        "utf-8"
    )
    return HanielConfig.model_validate(yaml.safe_load(rendered)), rendered


def _remote_release_identity(
    config_path: Path,
    config: HanielConfig,
    repo_name: str,
    manifest_path: str,
) -> tuple[str, str]:
    """Capture the exact fetched ref and manifest bytes bound to an activation."""

    from ..core.git import get_remote_head, read_file_at_commit
    from ..core.release_manifest import ReleaseManifest

    repo = config.repos[repo_name]
    repo_path = (config_path.parent / repo.path).resolve(strict=False)
    repo_head = get_remote_head(repo_path, repo.branch)
    manifest = read_file_at_commit(repo_path, repo_head, manifest_path)
    try:
        ReleaseManifest.model_validate_json(manifest)
    except ValueError as error:
        raise ReleaseManifestActivationRequired(
            "remote release manifest is invalid at the exact planned repository head: "
            f"{repo_head}:{manifest_path}"
        ) from error
    return repo_head, hashlib.sha256(manifest).hexdigest()


def _require_activation_semantics(config: HanielConfig):
    try:
        return require_valid_config(config)
    except ConfigSemanticError as error:
        raise ReleaseActivationSemanticError(str(error)) from error


def _load_mapping(raw: bytes) -> dict[str, Any]:
    payload = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ReleaseManifestActivationRequired("Haniel config root must be a mapping")
    return payload


def _atomic_write(path: Path, content: bytes, permission_source: Path) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    mode = stat.S_IMODE(permission_source.stat().st_mode)
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate or atomically activate a repository release manifest"
    )
    parser.add_argument("action", choices=("check", "apply"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", default=DEFAULT_RELEASE_MANIFEST)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--expected-repo-head")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-candidate-sha256")
    args = parser.parse_args()

    config = load_config(args.config)
    repo = config.repos.get(args.repo)
    if repo is None:
        parser.error(f"unknown repository: {args.repo}")
    repo_path = (args.config.parent / repo.path).resolve()
    discovered = discover_remote_release_manifest(repo_path, repo.branch, args.manifest)
    if discovered is None:
        parser.error(f"remote manifest not found: origin/{repo.branch}:{args.manifest}")
    plan = plan_release_manifest_activation(args.config, args.repo, discovered)
    if args.action == "check":
        print(json.dumps(_plan_json(plan), ensure_ascii=False, indent=2))
        raise SystemExit(2 if plan.changed else 0)
    required_evidence = {
        "--expected-sha256": args.expected_sha256,
        "--expected-repo-head": args.expected_repo_head,
        "--expected-manifest-sha256": args.expected_manifest_sha256,
        "--expected-candidate-sha256": args.expected_candidate_sha256,
    }
    missing = [name for name, value in required_evidence.items() if not value]
    if missing:
        parser.error("apply requires fresh check evidence: " + ", ".join(missing))
    expected = (
        args.expected_sha256,
        args.expected_repo_head,
        args.expected_manifest_sha256,
        args.expected_candidate_sha256,
    )
    actual = (
        plan.config_sha256,
        plan.repo_head,
        plan.manifest_sha256,
        plan.candidate_sha256,
    )
    if actual != expected:
        raise ReleaseActivationIdentityDrift(
            "activation check evidence changed; rerun the activation check"
        )
    result = activate_release_manifest(args.config, plan=plan)
    print(
        json.dumps(
            {
                "changed": result.changed,
                "config_sha256": result.config_sha256,
                "backup_path": str(result.backup_path) if result.backup_path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _plan_json(plan: ReleaseManifestActivationPlan) -> dict[str, Any]:
    return {
        "changed": plan.changed,
        "config_path": str(plan.config_path),
        "repo": plan.repo_name,
        "release_manifest": plan.release_manifest,
        "config_sha256": plan.config_sha256,
        "candidate_sha256": plan.candidate_sha256,
        "validator_revision": plan.validator_revision,
        "repo_head": plan.repo_head,
        "manifest_sha256": plan.manifest_sha256,
    }


if __name__ == "__main__":
    main()
