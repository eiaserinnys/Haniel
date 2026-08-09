"""Detached target staging must never mutate the live checkout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.core.handover_config import handover_config_digest
from haniel.core.path_identity import canonical_path_text
from haniel.core.deployment_command_runner import CommandResult
from haniel.core.git import (
    GitPullError,
    activate_repo_target,
    get_head,
    get_remote_head,
)
from haniel.core.release_staging import ReleaseIdentityError, stage_release
from haniel.core.one_shot_handover import probe_manifest_target
from haniel.core.runner import ServiceRunner


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_remote(tmp_path: Path, manifest: dict[str, object]) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test User")
    (source / "app.txt").write_text("old", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "old")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True)
    live = tmp_path / "live"
    subprocess.run(["git", "clone", str(remote), str(live)], check=True)
    previous = get_head(live)

    (source / "deploy").mkdir()
    (source / "deploy" / "release.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (source / "app.txt").write_text("new", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "new")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    return live, remote, previous


def base_manifest(*, with_probe: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "haniel.release.v1",
        "release_id": "release-1",
        "post_start_verify": [{"name": "health", "command": "health"}],
        "recovery": {
            "strategy": "rollback",
            "command": {"name": "restore", "command": "restore"},
        },
    }
    if with_probe:
        payload["migration"] = {
            "preflight": {"name": "preflight", "command": "preflight"},
            "apply": {"name": "apply", "command": "apply"},
            "provenance_probe": {
                "prepare": {"name": "prepare", "command": "prepare"},
                "probe": {"name": "probe", "command": "probe"},
            },
        }
    return payload


def test_probe_identity_mismatch_preserves_live_head_and_cleans_staging(
    tmp_path: Path,
) -> None:
    live, _remote, previous = make_remote(tmp_path, base_manifest(with_probe=True))

    def runner(command, env):
        if command.name == "probe":
            return CommandResult(
                stdout="{}",
                json_data={
                    "operation": "fresh_install",
                    "target_head": env["HANIEL_TARGET_HEAD"],
                    "manifest_digest": env["HANIEL_MANIFEST_DIGEST"],
                },
            )
        return CommandResult(stdout="", json_data=None)

    with pytest.raises(ReleaseIdentityError, match="OPERATION_MISMATCH"):
        with stage_release(
            repo_path=live,
            staging_root=tmp_path / "staging",
            repo_name="app",
            branch="main",
            manifest_path="deploy/release.json",
            request_id="request-1",
            expected_operation="upgrade",
            command_runner=runner,
        ):
            pass

    assert get_head(live) == previous
    assert not (tmp_path / "staging" / "request-1" / "app").exists()


def test_provenance_probe_receives_resolved_live_service_cwd(tmp_path: Path) -> None:
    live, _remote, previous = make_remote(tmp_path, base_manifest(with_probe=True))
    service_cwd = (tmp_path / "service-runtime").resolve()
    environments: list[dict[str, str]] = []

    def runner(command, env):
        environments.append(dict(env))
        if command.name == "probe":
            return CommandResult(
                stdout="{}",
                json_data={
                    "operation": "upgrade",
                    "target_head": env["HANIEL_TARGET_HEAD"],
                    "manifest_digest": env["HANIEL_MANIFEST_DIGEST"],
                },
            )
        return CommandResult(stdout="", json_data=None)

    with stage_release(
        repo_path=live,
        staging_root=tmp_path / "staging",
        repo_name="app",
        branch="main",
        manifest_path="deploy/release.json",
        request_id="request-service-cwd",
        expected_operation="upgrade",
        command_runner=runner,
        service_cwd_resolver=lambda _manifest: service_cwd,
    ):
        pass

    assert len(environments) == 2
    assert all(env["HANIEL_SERVICE_CWD"] == str(service_cwd) for env in environments)
    assert get_head(live) == previous


def test_legacy_manifest_without_probe_stages_without_command_execution(
    tmp_path: Path,
) -> None:
    live, _remote, previous = make_remote(tmp_path, base_manifest(with_probe=False))
    calls: list[str] = []

    with stage_release(
        repo_path=live,
        staging_root=tmp_path / "staging",
        repo_name="app",
        branch="main",
        manifest_path="deploy/release.json",
        request_id="request-1",
        expected_operation="upgrade",
        command_runner=lambda command, _env: calls.append(command.name),
    ) as staged:
        assert staged.target_head != previous
        assert staged.actual_operation is None
        assert staged.path.exists()

    assert get_head(live) == previous
    assert calls == []
    assert not (tmp_path / "staging" / "request-1" / "app").exists()


def test_required_manifest_without_config_identity_fails_before_live_activation(
    tmp_path: Path,
) -> None:
    manifest = base_manifest(with_probe=False)
    manifest.update(
        {
            "environment_service": "writer",
            "requires_service_env_file": True,
        }
    )
    live, remote, previous = make_remote(tmp_path, manifest)
    env_file = tmp_path / "writer.env"
    env_file.write_text("DATABASE_URL=sqlite:///isolated\n", encoding="utf-8")
    runner = ServiceRunner(
        HanielConfig(
            repos={
                "app": RepoConfig(
                    url=str(remote),
                    path=str(live),
                    release_manifest="deploy/release.json",
                )
            },
            services={
                "writer": ServiceConfig(
                    run="writer",
                    repo="app",
                    release_env_file=str(env_file),
                )
            },
        ),
        config_dir=tmp_path,
    )

    with pytest.raises(ReleaseIdentityError, match="CONFIG_DIGEST_REQUIRED"):
        probe_manifest_target(
            runner,
            "app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="required-without-config-identity",
        )

    assert get_head(live) == previous
    assert not (
        tmp_path / ".haniel" / "staging" / "required-without-config-identity" / "app"
    ).exists()


@pytest.mark.parametrize("caller", ["manual", "startup"])
def test_required_manifest_public_callers_bind_config_before_live_activation(
    tmp_path: Path,
    caller: str,
) -> None:
    manifest = base_manifest(with_probe=False)
    manifest.update(
        {
            "environment_service": "writer",
            "requires_service_env_file": True,
        }
    )
    live, remote, previous = make_remote(tmp_path, manifest)
    env_file = tmp_path / "writer.env"
    env_file.write_text("DATABASE_URL=sqlite:///isolated\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(
        json.dumps(
            {
                "repos": {
                    "app": {
                        "url": str(remote),
                        "path": str(live),
                        "release_manifest": "deploy/release.json",
                    }
                },
                "services": {
                    "writer": {
                        "run": "writer",
                        "repo": "app",
                        "release_env_file": str(env_file),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runner = ServiceRunner(
        load_config(config_path),
        config_dir=tmp_path,
        config_path=config_path,
    )
    if caller == "manual":
        git(live, "fetch", "origin", "main")
        runner._repo_states["app"].pending_changes = {"commits": ["target"]}

    with patch("haniel.core.runner.run_manifest_deployment") as deploy:
        if caller == "manual":
            runner.trigger_pull("app")
        else:
            runner._apply_startup_updates()
            runner.start_services()

    assert get_head(live) != previous
    kwargs = deploy.call_args.kwargs
    assert kwargs["config_digest"] == handover_config_digest(config_path)
    binding = kwargs["service_environment_bindings"]["writer"]
    assert canonical_path_text(Path(binding.path)) == canonical_path_text(env_file)
    assert binding.snapshot.path == env_file.resolve()
    assert kwargs["quiesce_services"] == ["writer"]


def test_remote_move_after_probe_cannot_activate_unverified_commit(
    tmp_path: Path,
) -> None:
    live, remote, previous = make_remote(tmp_path, base_manifest(with_probe=False))

    with stage_release(
        repo_path=live,
        staging_root=tmp_path / "staging",
        repo_name="app",
        branch="main",
        manifest_path="deploy/release.json",
        request_id="request-1",
        expected_operation="upgrade",
    ) as staged:
        approved = staged.target_head

    writer = tmp_path / "writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True)
    git(writer, "config", "user.email", "test@example.com")
    git(writer, "config", "user.name", "Test User")
    (writer / "app.txt").write_text("unverified", encoding="utf-8")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "remote moved after probe")
    git(writer, "push", "origin", "main")
    git(live, "fetch", "origin", "main")
    assert get_remote_head(live, "main") != approved

    discarded = activate_repo_target(live, approved, strategy="merge")

    assert previous != approved
    assert discarded == []
    assert get_head(live) == approved
    assert (live / "app.txt").read_text(encoding="utf-8") == "new"


def test_exact_merge_activation_failure_preserves_existing_local_change(
    tmp_path: Path,
) -> None:
    live, _remote, previous = make_remote(tmp_path, base_manifest(with_probe=False))
    with stage_release(
        repo_path=live,
        staging_root=tmp_path / "staging",
        repo_name="app",
        branch="main",
        manifest_path="deploy/release.json",
        request_id="request-1",
        expected_operation="upgrade",
    ) as staged:
        approved = staged.target_head
    (live / "app.txt").write_text("local-change", encoding="utf-8")

    with pytest.raises(GitPullError):
        activate_repo_target(live, approved, strategy="merge")

    assert get_head(live) == previous
    assert (live / "app.txt").read_text(encoding="utf-8") == "local-change"
