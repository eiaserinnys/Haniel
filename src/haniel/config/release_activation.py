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
from ..core.deployment import ReleaseManifest
from ..core.git import GitError

DEFAULT_RELEASE_MANIFEST = "deploy/release-manifest.json"


class ReleaseManifestActivationRequired(RuntimeError):
    """A remote release contract exists but Haniel cannot activate it safely."""


@dataclass(frozen=True)
class ReleaseManifestActivationPlan:
    config_path: Path
    repo_name: str
    release_manifest: str
    config_sha256: str
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
    HanielConfig.model_validate(payload)
    return ReleaseManifestActivationPlan(
        config_path=config_path,
        repo_name=repo_name,
        release_manifest=manifest_path,
        config_sha256=digest,
        changed=current != manifest_path,
    )


def activate_release_manifest(
    config_path: Path,
    repo_name: str,
    manifest_path: str,
    *,
    expected_sha256: str,
) -> ReleaseManifestActivationResult:
    """Apply a one-field config migration with backup and compare-and-swap."""
    plan = plan_release_manifest_activation(config_path, repo_name, manifest_path)
    if plan.config_sha256 != expected_sha256:
        raise ReleaseManifestActivationRequired(
            "Haniel config changed after validation; rerun the activation check"
        )
    if not plan.changed:
        return ReleaseManifestActivationResult(False, plan.config_sha256, None)

    original = config_path.read_bytes()
    payload = _load_mapping(original)
    payload["repos"][repo_name]["release_manifest"] = manifest_path
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode(
        "utf-8"
    )
    HanielConfig.model_validate(yaml.safe_load(rendered))

    backup_path = config_path.with_name(
        f"{config_path.name}.before-release-manifest-{plan.config_sha256[:12]}.bak"
    )
    if backup_path.exists() and backup_path.read_bytes() != original:
        raise ReleaseManifestActivationRequired(
            f"activation backup already exists with different content: {backup_path}"
        )
    if not backup_path.exists():
        _atomic_write(backup_path, original, config_path)
    _atomic_write(config_path, rendered, config_path)
    return ReleaseManifestActivationResult(
        True, hashlib.sha256(rendered).hexdigest(), backup_path
    )


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
    }


if __name__ == "__main__":
    main()
