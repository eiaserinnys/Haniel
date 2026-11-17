"""Repository hook and startup recovery contracts."""

import logging
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, HooksConfig, RepoConfig, ServiceConfig
from haniel.core.deployment_errors import StableDeploymentError
from haniel.core.legacy_build_marker import (
    read_completed_commits,
    write_completed_commits,
)
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import RunnerDeploymentAdapter


def make_runner(tmp_path: Path, *, with_service=False, strategy=None):
    (tmp_path / "app").mkdir()
    services = {
        "other": ServiceConfig(run="other"),
    }
    if with_service:
        services["app"] = ServiceConfig(
            run="app", repo="app", hooks=HooksConfig(post_pull="build-service")
        )
    config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="git@example/app",
                path="app",
                pull_strategy=strategy,
                hooks=HooksConfig(post_pull="build-repo", timeout=73),
            )
        },
        services=services,
    )
    return ServiceRunner(config, config_dir=tmp_path)


def test_repo_hook_uses_repo_cwd_and_timeout(tmp_path):
    runner = make_runner(tmp_path)
    with patch("haniel.core.runner.subprocess.run") as run:
        assert runner.execute_repo_hook("app", "post_pull")
    assert run.call_args.kwargs["cwd"] == tmp_path / "app"
    assert run.call_args.kwargs["timeout"] == 73
    assert run.call_args.args[0] == (
        "build-repo" if os.name == "nt" else ["build-repo"]
    )


@pytest.mark.parametrize("success", [True, False])
def test_approved_repo_only_hook_controls_marker(tmp_path, success):
    runner = make_runner(tmp_path)
    write_completed_commits(tmp_path, {"app": "old"})
    with (
        patch.object(
            runner, "execute_repo_hook", return_value=success, create=True
        ) as hook,
        patch("haniel.core.runner.get_head", return_value="new"),
        patch("haniel.core.runner.sync_disk"),
    ):
        if success:
            runner._restart_after_pull_legacy("app", [])
        else:
            with pytest.raises(StableDeploymentError) as exc:
                runner._restart_after_pull_legacy("app", [])
            assert exc.value.code == "APPLY_FAILED"
    hook.assert_called_once_with("app", "post_pull")
    assert read_completed_commits(tmp_path) == {"app": "new" if success else "old"}


def test_approved_repo_hook_runs_before_service_and_failure_preserves_process(tmp_path):
    runner = make_runner(tmp_path, with_service=True)
    write_completed_commits(tmp_path, {"app": "old"})
    calls = []
    with (
        patch.object(
            runner,
            "execute_repo_hook",
            side_effect=lambda *args: calls.append("repo") or False,
        ),
        patch.object(
            runner,
            "execute_hook",
            side_effect=lambda *args: calls.append("service") or True,
        ),
        patch.object(runner.process_manager, "stop_service") as stop,
        patch.object(runner, "_start_service") as start,
        pytest.raises(StableDeploymentError) as exc,
    ):
        runner._restart_after_pull_legacy("app", ["app"])
    assert exc.value.code == "APPLY_FAILED"
    assert calls[0] == "repo"
    stop.assert_not_called()
    start.assert_not_called()
    assert read_completed_commits(tmp_path) == {"app": "old"}


@pytest.mark.parametrize("success", [True, False])
def test_startup_repo_only_hook_and_other_service(tmp_path, success):
    runner = make_runner(tmp_path)
    runner._startup_updated_repos = {"app"}
    with (
        patch.object(
            runner, "execute_repo_hook", return_value=success, create=True
        ) as hook,
        patch.object(runner, "_start_service", return_value=True) as start,
        patch("haniel.core.runner.get_head", return_value="new"),
        patch("haniel.core.runner.sync_disk"),
    ):
        runner.start_services()
    hook.assert_called_once_with("app", "post_pull")
    start.assert_called_once_with("other")
    assert read_completed_commits(tmp_path) == ({"app": "new"} if success else {})


def test_startup_repo_hook_before_service_hook(tmp_path):
    runner = make_runner(tmp_path, with_service=True)
    runner._startup_updated_repos = {"app"}
    calls = []
    with (
        patch.object(
            runner,
            "execute_repo_hook",
            side_effect=lambda *args: calls.append("repo") or True,
            create=True,
        ),
        patch.object(
            runner,
            "execute_hook",
            side_effect=lambda *args: calls.append("service") or True,
        ),
        patch.object(runner, "_start_service", return_value=True),
        patch("haniel.core.runner.get_head", return_value="new"),
        patch("haniel.core.runner.sync_disk"),
    ):
        runner.start_services()
    assert calls == ["repo", "service"]


def test_failed_repo_hook_is_rerun_after_rollback(tmp_path):
    runner = make_runner(tmp_path, with_service=True)
    runner._startup_updated_repos = {"app"}
    write_completed_commits(tmp_path, {"app": "old"})
    calls = []

    def repo_hook(*args):
        calls.append("repo")
        return calls.count("repo") > 1

    with (
        patch.object(runner, "execute_repo_hook", side_effect=repo_hook),
        patch.object(
            runner,
            "execute_hook",
            side_effect=lambda *args: calls.append("service") or True,
        ),
        patch.object(runner, "_start_service", return_value=True),
        patch("haniel.core.runner.get_head", return_value="new"),
        patch("haniel.core.runner._tracked_worktree_dirty", return_value=False),
        patch("haniel.core.runner.reset_repo_to") as reset,
        patch("haniel.core.runner.sync_disk"),
    ):
        runner.start_services()
    reset.assert_called_once_with(tmp_path / "app", "old")
    assert calls == ["repo", "service", "repo", "service"]
    assert read_completed_commits(tmp_path) == {"app": "old"}


@pytest.mark.parametrize("strategy", ["merge", "force"])
def test_failed_startup_hook_respects_dirty_merge(tmp_path, strategy, caplog):
    runner = make_runner(tmp_path, with_service=True, strategy=strategy)
    runner._startup_updated_repos = {"app"}
    write_completed_commits(tmp_path, {"app": "old"})
    with (
        patch.object(runner, "execute_repo_hook", return_value=False, create=True),
        patch.object(runner, "execute_hook", return_value=False),
        patch.object(runner, "_start_service", return_value=True) as start,
        patch("haniel.core.runner.get_head", return_value="new"),
        patch("haniel.core.runner._tracked_worktree_dirty", return_value=True),
        patch("haniel.core.runner.reset_repo_to") as reset,
        patch("haniel.core.runner.sync_disk"),
    ):
        runner.start_services()
    if strategy == "merge":
        reset.assert_not_called()
        assert "not rolling back" in caplog.text
        assert "uncommitted tracked changes" in caplog.text
    else:
        reset.assert_called_once_with(tmp_path / "app", "old")
    start.assert_called_once_with("other")
    assert read_completed_commits(tmp_path) == {"app": "old"}


def test_manifest_warns_without_running_repo_hook(tmp_path, caplog):
    runner = make_runner(tmp_path)
    adapter = RunnerDeploymentAdapter(runner, "app", [], tmp_path / "app", "old")
    with (
        patch.object(runner, "execute_repo_hook", create=True) as hook,
        patch("haniel.core.runner_deployment.sync_disk"),
        caplog.at_level(logging.WARNING),
    ):
        adapter.build()
    hook.assert_not_called()
    assert "post_pull" in caplog.text and "app" in caplog.text


def test_service_registration_repo_hook_uses_configured_timeout(tmp_path):
    from haniel.core import service_lifecycle

    repo_path = tmp_path / "app"
    repo_path.mkdir()
    repo_config = RepoConfig(
        url="git@example/app",
        path="app",
        hooks=HooksConfig(post_pull="build-repo", timeout=42),
    )
    with patch("haniel.core.service_lifecycle.subprocess.run") as run:
        assert service_lifecycle._maybe_run_repo_post_pull(
            "app", repo_config, repo_path, tmp_path
        )
    assert run.call_args.kwargs["timeout"] == 42


def test_service_registration_hook_defaults_to_model_timeout(tmp_path):
    from haniel.core import service_lifecycle

    repo_path = tmp_path / "app"
    repo_path.mkdir()
    with patch("haniel.core.service_lifecycle.subprocess.run") as run:
        service_lifecycle._run_shell_hook(
            "app", "post_pull", "build-repo", repo_path, tmp_path
        )
    assert run.call_args.kwargs["timeout"] == HooksConfig().timeout


def test_service_registration_skips_repo_hook_without_block(tmp_path):
    from haniel.core import service_lifecycle

    repo_path = tmp_path / "app"
    repo_path.mkdir()
    repo_config = RepoConfig(url="git@example/app", path="app")
    with patch("haniel.core.service_lifecycle.subprocess.run") as run:
        assert not service_lifecycle._maybe_run_repo_post_pull(
            "app", repo_config, repo_path, tmp_path
        )
    run.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_runner_conf_overwrites_existing_permissions(tmp_path):
    from haniel.installer.finalize import Finalizer

    config = HanielConfig.model_validate(
        {"self": {"repo": "app"}, "repos": {}, "services": {}}
    )
    path = tmp_path / "haniel-runner.conf"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    Finalizer(config, tmp_path, MagicMock())._generate_runner_conf()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "HANIEL_REPO=" in path.read_text(encoding="utf-8")
