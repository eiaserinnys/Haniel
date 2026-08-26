"""Deployment timeout budgets and repository-owned hook boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from haniel.config import HanielConfig, HooksConfig, RepoConfig, ServiceConfig
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import RunnerDeploymentAdapter


def _manifest() -> ReleaseManifest:
    return ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "timeout-budget-test",
            "build_retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 0,
                "max_backoff_seconds": 0,
                "total_grace_seconds": 0,
            },
            "post_start_verify": [
                {"name": "verify-http", "command": "verify-http", "timeout_seconds": 7},
                {"name": "verify-mcp", "command": "verify-mcp", "timeout_seconds": 11},
            ],
            "recovery": {
                "strategy": "rollback",
                "command": {"name": "restore", "command": "restore"},
            },
        }
    )


def _runner(tmp_path: Path) -> ServiceRunner:
    config = HanielConfig(
        repos={
            "app": RepoConfig(url="git@example/app.git", path="./app"),
            "other": RepoConfig(url="git@example/other.git", path="./other"),
        },
        services={
            "owner": ServiceConfig(
                run="owner",
                repo="app",
                ready_timeout=10,
                hooks=HooksConfig(post_pull="build-owner", timeout=100),
            ),
            "same-repo-dependent": ServiceConfig(
                run="same",
                repo="app",
                after=["owner"],
                ready_timeout=20,
                hooks=HooksConfig(post_pull="build-same", timeout=200),
            ),
            "other-repo-dependent": ServiceConfig(
                run="other",
                repo="other",
                after=["owner"],
                ready_timeout=30,
                hooks=HooksConfig(post_pull="build-other", timeout=500),
            ),
        },
    )
    return ServiceRunner(config, config_dir=tmp_path)


def _adapter(runner: ServiceRunner, tmp_path: Path) -> RunnerDeploymentAdapter:
    return RunnerDeploymentAdapter(
        runner=runner,
        repo_name="app",
        affected=["owner", "same-repo-dependent", "other-repo-dependent"],
        repo_path=tmp_path / "app",
        previous_head="previous",
    )


def test_timeout_fields_default_override_and_validate_ranges(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(
        """
repos: {}
services:
  legacy:
    run: legacy
    hooks:
      post_pull: build
  tuned:
    run: tuned
    ready_timeout: 180
    hooks:
      post_pull: build
      timeout: 1200
""",
        encoding="utf-8",
    )

    from haniel.config import load_config

    config = load_config(config_path)
    assert config.services["legacy"].hooks is not None
    assert config.services["legacy"].hooks.timeout == 900
    assert config.services["legacy"].ready_timeout == 60
    assert config.services["tuned"].hooks is not None
    assert config.services["tuned"].hooks.timeout == 1200
    assert config.services["tuned"].ready_timeout == 180
    with pytest.raises(ValidationError):
        HooksConfig(timeout=0)
    with pytest.raises(ValidationError):
        ServiceConfig(run="bad", ready_timeout=0)


@patch("subprocess.run")
def test_execute_hook_uses_service_hook_timeout(run: MagicMock, tmp_path: Path) -> None:
    run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    runner = _runner(tmp_path)

    assert runner.execute_hook("owner", "post_pull") is True

    assert run.call_args.kwargs["timeout"] == 100


def test_start_service_uses_service_ready_timeout(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.process_manager.start_service = MagicMock()

    assert runner._start_service("owner") is True

    assert runner.process_manager.start_service.call_args.kwargs["ready_timeout"] == 10


def test_manifest_start_and_wait_uses_each_service_ready_timeout(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    adapter = _adapter(runner, tmp_path)
    running = {"owner": False, "same-repo-dependent": False}
    runner.process_manager.is_running = MagicMock(
        side_effect=lambda service: running[service]
    )
    runner._blocked_start_dependencies = MagicMock(return_value=[])
    runner._start_service = MagicMock(
        side_effect=lambda service: running.__setitem__(service, True) or True
    )
    runner.process_manager.wait_for_ready = MagicMock(return_value=True)

    adapter.start_and_wait({"owner", "same-repo-dependent"})

    assert runner.process_manager.wait_for_ready.call_args_list == [
        (("owner",), {"timeout": 10}),
        (("same-repo-dependent",), {"timeout": 20}),
    ]


def test_manifest_build_runs_post_pull_only_for_services_owned_by_repo(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    adapter = _adapter(runner, tmp_path)
    runner.execute_hook = MagicMock(return_value=True)

    adapter.build()

    assert runner.execute_hook.call_args_list == [
        (("owner", "post_pull"), {}),
        (("same-repo-dependent", "post_pull"), {}),
    ]


@patch("haniel.core.runner_deployment.get_head", return_value="previous")
@patch("haniel.core.runner_deployment.reset_repo_to")
def test_manifest_rollback_rebuilds_only_services_owned_by_repo(
    _reset: MagicMock, _head: MagicMock, tmp_path: Path
) -> None:
    runner = _runner(tmp_path)
    adapter = _adapter(runner, tmp_path)
    adapter.desired_running = set()
    runner._commit_repo_observation = MagicMock(return_value=True)
    runner.execute_hook = MagicMock(return_value=True)

    adapter.rollback()

    assert runner.execute_hook.call_args_list == [
        (("owner", "post_pull"), {}),
        (("same-repo-dependent", "post_pull"), {}),
    ]


def test_current_generation_restore_skips_dependent_repo_post_pull(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    adapter = _adapter(runner, tmp_path)
    running = {
        "owner": False,
        "same-repo-dependent": False,
        "other-repo-dependent": False,
    }
    runner.process_manager.is_running = MagicMock(
        side_effect=lambda service: running[service]
    )
    runner._start_service = MagicMock(
        side_effect=lambda service: running.__setitem__(service, True) or True
    )
    runner.process_manager.wait_for_ready = MagicMock(return_value=True)
    runner.execute_hook = MagicMock(return_value=True)

    adapter._restore_current_generation_services()

    assert runner.execute_hook.call_args_list == [
        (("owner", "post_pull"), {}),
        (("same-repo-dependent", "post_pull"), {}),
    ]
    assert runner._start_service.call_count == 3
    assert runner.process_manager.wait_for_ready.call_args_list == [
        (("owner",), {"timeout": 10}),
        (("same-repo-dependent",), {"timeout": 20}),
        (("other-repo-dependent",), {"timeout": 30}),
    ]


def test_legacy_pull_runs_post_pull_only_for_services_owned_by_repo(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    runner.execute_hook = MagicMock(return_value=True)
    runner.process_manager.is_running = MagicMock(return_value=False)
    runner._blocked_start_dependencies = MagicMock(return_value=[])
    runner._start_service = MagicMock(return_value=True)

    runner._restart_after_pull_legacy(
        "app", ["owner", "same-repo-dependent", "other-repo-dependent"]
    )

    assert runner.execute_hook.call_args_list == [
        (("owner", "post_pull"), {}),
        (("same-repo-dependent", "post_pull"), {}),
    ]
    assert runner._start_service.call_count == 3


def test_expected_deployment_budget_uses_owned_hooks_and_all_readiness() -> None:
    from haniel.core.deployment_budget import expected_deployment_budget

    services = _runner(Path("/tmp/unused-haniel-budget")).config.services

    budget = expected_deployment_budget(
        repo_name="app",
        affected=["owner", "same-repo-dependent", "other-repo-dependent"],
        services=services,
        manifest=_manifest(),
    )

    assert budget.build_hooks_sec == 900
    assert budget.readiness_sec == 60
    assert budget.verification_sec == 18
    assert budget.total_sec == 978
