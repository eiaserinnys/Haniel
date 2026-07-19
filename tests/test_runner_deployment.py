"""ServiceRunner integration tests for the release manifest handover."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.deployment import DeploymentError
from haniel.core.git import get_head
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import run_manifest_deployment


def release_manifest() -> dict[str, object]:
    def command(name: str) -> dict[str, object]:
        return {"name": name, "command": f"run-{name}", "timeout_seconds": 30}

    return {
        "schema_version": "haniel.release.v1",
        "release_id": "release-042",
        "environment_service": "app",
        "migration": {
            "destructive": True,
            "preflight": command("preflight"),
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
            "apply": command("migrate"),
        },
        "post_start_verify": [command("verify-http"), command("verify-mcp")],
        "recovery": {
            "strategy": "roll_forward",
            "command": command("recover"),
        },
    }


@pytest.fixture
def manifest_runner(tmp_path: Path) -> tuple[ServiceRunner, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "app.txt").write_text("old", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "old"], cwd=repo, check=True)
    previous_head = get_head(repo)

    deploy = repo / "deploy"
    deploy.mkdir()
    (deploy / "release.json").write_text(
        json.dumps(release_manifest()), encoding="utf-8"
    )
    (repo / "app.txt").write_text("new", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=repo, check=True)

    config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="git@github.com:test/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={"app": ServiceConfig(run="app", repo="app", ready="port:9999")},
    )
    return ServiceRunner(config, config_dir=tmp_path), repo, previous_head


def configure_processes(runner: ServiceRunner, events: list[str]) -> dict[str, bool]:
    running = {"app": True}

    def stop(name: str) -> bool:
        events.append(f"stop:{name}")
        running[name] = False
        return True

    def start(name: str) -> bool:
        events.append(f"start:{name}")
        running[name] = True
        return True

    runner.process_manager.is_running = MagicMock(
        side_effect=lambda name: running[name]
    )
    runner.process_manager.stop_service = MagicMock(side_effect=stop)
    runner.process_manager.wait_for_ready = MagicMock(
        side_effect=lambda name: events.append(f"ready:{name}") or True
    )
    runner._start_service = MagicMock(side_effect=start)
    return running


def test_manifest_handover_waits_for_readiness_and_post_verify(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    events: list[str] = []
    configure_processes(runner, events)
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: events.append(f"{hook}:{name}") or True
    )

    def run_command(spec, env):
        assert env["HANIEL_BACKUP_DIR"]
        assert env["HANIEL_SERVICE_CWD"] == str(runner.config_dir.resolve())
        events.append(spec.name)

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert events == [
        "post_pull:app",
        "preflight",
        "stop:app",
        "backup",
        "verify-backup",
        "migrate",
        "start:app",
        "ready:app",
        "verify-http",
        "verify-mcp",
    ]


def test_build_failure_restores_code_without_stopping_old_process(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    hook_results = iter([False, True])
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: (
            events.append(f"{hook}:{name}") or next(hook_results)
        )
    )

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=lambda _spec, _env: None,
    ):
        with pytest.raises(DeploymentError) as exc_info:
            run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert exc_info.value.recovered is True
    assert running["app"] is True
    assert not any(event.startswith("stop:") for event in events)
    assert get_head(repo) == previous_head
    assert (repo / "app.txt").read_text(encoding="utf-8") == "old"


def test_backup_verification_failure_restarts_previous_release(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: events.append(f"{hook}:{name}") or True
    )

    def run_command(spec, _env):
        events.append(spec.name)
        if spec.name == "verify-backup":
            raise RuntimeError("backup verification failed")

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        with pytest.raises(DeploymentError) as exc_info:
            run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert exc_info.value.recovered is True
    assert running["app"] is True
    assert get_head(repo) == previous_head
    assert events == [
        "post_pull:app",
        "preflight",
        "stop:app",
        "backup",
        "verify-backup",
        "post_pull:app",
        "start:app",
        "ready:app",
    ]


def test_readiness_failure_runs_roll_forward_and_recovers_availability(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    ready_results = iter([False, True])
    runner.process_manager.wait_for_ready = MagicMock(
        side_effect=lambda name: events.append(f"ready:{name}") or next(ready_results)
    )
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: events.append(f"{hook}:{name}") or True
    )

    def run_command(spec, _env):
        events.append(spec.name)

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        with pytest.raises(DeploymentError) as exc_info:
            run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert exc_info.value.recovered is True
    assert running["app"] is True
    assert events.count("start:app") == 2
    assert events.count("stop:app") == 2
    assert "recover" in events
    assert events[-2:] == ["verify-http", "verify-mcp"]
