"""Release manifest activation is atomic and happens before a repository pull."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.config.release_activation import (
    DEFAULT_RELEASE_MANIFEST,
    ReleaseActivationWriteError,
    ReleaseManifestActivationRequired,
    activate_release_manifest,
    plan_release_manifest_activation,
)
from haniel.core.git import GitError


def write_config(path: Path) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@github.com:eiaserinnys/soulstream.git",
                path="./services/soulstream",
                auto_apply=False,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(
                run="node orch.js",
                repo="soulstream",
                cwd="./services/soulstream",
                ready="delay:0.01",
            ),
            "soulstream-soul-server-ts": ServiceConfig(
                run="node soul.js",
                repo="soulstream",
                cwd="./services/soulstream",
                after=["soulstream-orch-server"],
                ready="delay:0.01",
            ),
        },
    )
    path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True), sort_keys=False
        ),
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_manifest(repo: Path, release_id: str) -> None:
    manifest = repo / DEFAULT_RELEASE_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "haniel.release.v1",
                "release_id": release_id,
                "post_start_verify": [
                    {"name": "health", "command": "true", "timeout_seconds": 30}
                ],
                "recovery": {
                    "strategy": "rollback",
                    "command": {
                        "name": "rollback",
                        "command": "true",
                        "timeout_seconds": 30,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _init_remote_manifest_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "services" / "soulstream"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _write_manifest(repo, "release-1")
    _git(repo, "add", DEFAULT_RELEASE_MANIFEST)
    _git(repo, "commit", "-m", "release 1")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_plan_is_read_only_and_reports_compare_and_swap_hash(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    before = config_path.read_bytes()

    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )

    assert plan.changed is True
    assert plan.config_sha256 == hashlib.sha256(before).hexdigest()
    assert plan.release_manifest == DEFAULT_RELEASE_MANIFEST
    assert config_path.read_bytes() == before


def test_apply_updates_only_configured_repo_and_preserves_backup(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    before = config_path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()

    result = activate_release_manifest(
        config_path,
        "soulstream",
        DEFAULT_RELEASE_MANIFEST,
        expected_sha256=digest,
    )

    assert result.changed is True
    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == before


def test_apply_rejects_stale_config_hash_without_writing(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    before = config_path.read_bytes()

    with pytest.raises(ReleaseManifestActivationRequired, match="changed"):
        activate_release_manifest(
            config_path,
            "soulstream",
            DEFAULT_RELEASE_MANIFEST,
            expected_sha256="0" * 64,
        )

    assert config_path.read_bytes() == before


def test_apply_restores_exact_original_when_target_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haniel.config.release_activation as activation

    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    before = config_path.read_bytes()
    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )
    actual_atomic_write = activation._atomic_write
    failed_once = False

    def fail_after_target_replace(
        path: Path, content: bytes, permission_source: Path
    ) -> None:
        nonlocal failed_once
        actual_atomic_write(path, content, permission_source)
        if path == config_path and content != before and not failed_once:
            failed_once = True
            raise OSError("post-replace verification boundary")

    monkeypatch.setattr(activation, "_atomic_write", fail_after_target_replace)

    with pytest.raises(ReleaseActivationWriteError) as failed:
        activate_release_manifest(config_path, plan=plan)

    assert failed.value.code == "CONFIG_WRITE_FAILED"
    assert config_path.read_bytes() == before
    assert failed_once is True


def test_rollback_uses_current_config_permissions_not_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haniel.config.release_activation as activation

    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )
    actual_atomic_write = activation._atomic_write
    writes: list[tuple[Path, Path]] = []

    def fail_target(path: Path, content: bytes, permission_source: Path) -> None:
        writes.append((path, permission_source))
        actual_atomic_write(path, content, permission_source)
        if (
            path == config_path
            and len([item for item in writes if item[0] == path]) == 1
        ):
            raise OSError("force rollback")

    monkeypatch.setattr(activation, "_atomic_write", fail_target)
    with pytest.raises(ReleaseActivationWriteError):
        activate_release_manifest(config_path, plan=plan)

    target_writes = [source for path, source in writes if path == config_path]
    assert target_writes == [config_path, config_path]


def test_cli_apply_requires_and_reuses_check_identity_evidence(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    module = "haniel.config.release_activation"
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "check",
            "--config",
            str(config_path),
            "--repo",
            "soulstream",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 2
    evidence = json.loads(checked.stdout)

    applied = subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "apply",
            "--config",
            str(config_path),
            "--repo",
            "soulstream",
            "--expected-sha256",
            evidence["config_sha256"],
            "--expected-repo-head",
            evidence["repo_head"],
            "--expected-manifest-sha256",
            evidence["manifest_sha256"],
            "--expected-candidate-sha256",
            evidence["candidate_sha256"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert applied.returncode == 0, applied.stderr
    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )


def test_plan_parses_the_exact_bytes_used_for_its_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haniel.config.release_activation as activation

    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    monkeypatch.setattr(
        activation,
        "load_config",
        lambda _path: (_ for _ in ()).throw(AssertionError("second config read")),
    )

    plan = activation.plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )

    assert plan.changed is True


def test_plan_wraps_git_failure_in_stable_activation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haniel.config.release_activation as activation

    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    monkeypatch.setattr(
        activation,
        "_remote_release_identity",
        lambda *_args: (_ for _ in ()).throw(GitError("fetch unavailable")),
    )

    with pytest.raises(ReleaseManifestActivationRequired) as failed:
        plan_release_manifest_activation(
            config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
        )

    assert getattr(failed.value, "code", None) == "RELEASE_IDENTITY_UNAVAILABLE"


def test_plan_validates_the_exact_manifest_bytes_bound_to_repo_head(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    repo = _init_remote_manifest_repo(tmp_path)
    (repo / DEFAULT_RELEASE_MANIFEST).write_text("not-json", encoding="utf-8")
    _git(repo, "add", DEFAULT_RELEASE_MANIFEST)
    _git(repo, "commit", "-m", "invalid release manifest")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    with pytest.raises(ReleaseManifestActivationRequired, match="invalid"):
        plan_release_manifest_activation(
            config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
        )


def test_apply_rederives_changed_from_locked_source_instead_of_plan_flag(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    _init_remote_manifest_repo(tmp_path)
    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )

    result = activate_release_manifest(config_path, plan=replace(plan, changed=False))

    assert result.changed is True
    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )


def test_apply_rejects_remote_head_or_manifest_drift_without_writing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    repo = _init_remote_manifest_repo(tmp_path)
    before = config_path.read_bytes()
    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )
    _write_manifest(repo, "release-2")
    _git(repo, "add", DEFAULT_RELEASE_MANIFEST)
    _git(repo, "commit", "-m", "release 2")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    with pytest.raises(ReleaseManifestActivationRequired, match="identity"):
        activate_release_manifest(config_path, plan=plan)

    assert config_path.read_bytes() == before
    assert list(tmp_path.glob("*.bak")) == []
