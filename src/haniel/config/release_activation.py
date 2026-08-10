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
from ..core.deployment import ReleaseManifest
from ..core.git import GitError

DEFAULT_RELEASE_MANIFEST = "deploy/release-manifest.json"


class ReleaseManifestActivationRequired(RuntimeError):
    """A remote release contract exists but Haniel cannot activate it safely."""


class ReleaseActivationSemanticError(ReleaseManifestActivationRequired):
    """A release activation candidate is schema-valid but semantically invalid."""

    code = "CONFIG_SEMANTIC_INVALID"


class ReleaseActivationIdentityDrift(ReleaseManifestActivationRequired):
    """The exact source or candidate identity changed after planning."""

    code = "CONFIG_IDENTITY_DRIFT"


class ReleaseActivationWriteError(ReleaseManifestActivationRequired):
    """Activation write failed after validation and was rolled back."""

    code = "CONFIG_WRITE_FAILED"


@dataclass(frozen=True)
class ReleaseManifestActivationPlan:
    config_path: Path
    repo_name: str
    release_manifest: str
    config_sha256: str
    candidate_sha256: str
    validator_revision: str
    validator_error_count: int
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
    # Unit fixtures may use a sentinel .git directory while mocking fetch/pull.
    # A real fetch cannot succeed for such a directory, so it is not a production
    # fail-open path.
    if not (repo_path / ".git" / "HEAD").exists():
        return None

    ref = f"origin/{branch}"
    verified = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if verified.returncode != 0:
        # No remote target means there is nothing safe to pull. The following
        # real pull will fail as before; unit fixtures may mock that boundary.
        return None
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", manifest_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if listed.returncode != 0:
        raise GitError(
            f"failed to inspect release manifest at {ref}: {listed.stderr.strip()}"
        )
    if manifest_path not in listed.stdout.splitlines():
        return None

    shown = subprocess.run(
        ["git", "show", f"{ref}:{manifest_path}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if shown.returncode != 0:
        raise GitError(
            f"failed to read release manifest at {ref}: {shown.stderr.strip()}"
        )
    try:
        ReleaseManifest.model_validate_json(shown.stdout)
    except Exception as error:
        raise ReleaseManifestActivationRequired(
            f"remote release manifest is invalid: {ref}:{manifest_path}: {error}"
        ) from error
    return manifest_path


def plan_release_manifest_activation(
    config_path: Path,
    repo_name: str,
    manifest_path: str,
) -> ReleaseManifestActivationPlan:
    """Validate one semantic config change without writing it."""
    raw = config_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    config = load_config(config_path)
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

    from ..core.service_lifecycle import config_file_transaction

    with config_file_transaction(config_path):
        original = config_path.read_bytes()
        if hashlib.sha256(original).hexdigest() != plan.config_sha256:
            raise ReleaseActivationIdentityDrift(
                "Haniel config changed after validation; stale activation plan"
            )

        source = HanielConfig.model_validate(_load_mapping(original))
        _require_activation_semantics(source)
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
        if not plan.changed:
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
                    permission_source = (
                        backup_path if backup_path.exists() else config_path
                    )
                    _atomic_write(config_path, original, permission_source)
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
    if not args.expected_sha256:
        parser.error("apply requires --expected-sha256 from a fresh check")
    result = activate_release_manifest(
        args.config,
        args.repo,
        discovered,
        expected_sha256=args.expected_sha256,
    )
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
    }


if __name__ == "__main__":
    main()
