"""Failure injection for startup deployments routed through the release state machine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.deployment import DeploymentError, DeploymentStateStore
from haniel.core.git import get_head
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import run_manifest_deployment


def command(name: str) -> dict[str, object]:
    return {"name": name, "command": f"run-{name}", "timeout_seconds": 30}


def manifest() -> dict[str, object]:
    return {
        "schema_version": "haniel.release.v1",
        "release_id": "startup-release",
        "migration": {
            "destructive": True,
            "preflight": command("preflight"),
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
            "apply": command("migrate"),
        },
        "post_start_verify": [command("verify-http"), command("verify-mcp")],
        "build_retry": {
            "max_attempts": 4,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "total_grace_seconds": 1,
        },
        "post_start_verify_retry": {
            "max_attempts": 4,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "total_grace_seconds": 1,
        },
        "recovery": {
            "strategy": "roll_forward",
            "command": command("recover"),
            "fallback": command("prepare-previous-release"),
        },
    }


@pytest.fixture
def startup_runner(tmp_path: Path) -> tuple[ServiceRunner, Path, str]:
    repo = tmp_path / "soulstream"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "release.txt").write_text("old", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "old"], cwd=repo, check=True)
    previous_head = get_head(repo)

    deploy = repo / "deploy"
    deploy.mkdir()
    (deploy / "release-manifest.json").write_text(
        json.dumps(manifest()), encoding="utf-8"
    )
    (repo / "release.txt").write_text("new", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=repo, check=True)

    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@github.com:test/soulstream.git",
                path="./soulstream",
                release_manifest="deploy/release-manifest.json",
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(
                run="orch", repo="soulstream", ready="port:5200"
            ),
            "soulstream-soul-server-ts": ServiceConfig(
                run="soul",
                repo="soulstream",
                ready="port:3105",
                after=["soulstream-orch-server"],
            ),
        },
    )
    return ServiceRunner(config, config_dir=tmp_path), repo, previous_head


@pytest.mark.parametrize(
    "failed_stage",
    [
        "build",
        "preflight",
        "backup",
        "verify-backup",
        "migrate",
        "start",
        "readiness",
        "process-exit",
        "verify-http",
    ],
)
def test_startup_failure_keeps_availability_without_duplicate_legacy_start(
    startup_runner: tuple[ServiceRunner, Path, str], failed_stage: str
) -> None:
    runner, repo, previous_head = startup_runner
    services = ["soulstream-orch-server", "soulstream-soul-server-ts"]
    running = {name: False for name in services}
    events: list[str] = []
    failures_left = len(services) * 4 if failed_stage == "build" else 1

    def fail_once(stage: str) -> bool:
        nonlocal failures_left
        if failed_stage == stage and failures_left:
            failures_left -= 1
            return True
        return False

    def hook(name: str, hook_name: str) -> bool:
        events.append(f"{hook_name}:{name}")
        return not (hook_name == "post_pull" and fail_once("build"))

    def start(name: str) -> bool:
        events.append(f"start:{name}")
        if fail_once("start"):
            return False
        running[name] = True
        return True

    def stop(name: str) -> bool:
        events.append(f"stop:{name}")
        running[name] = False
        return True

    def ready(name: str) -> bool:
        events.append(f"ready:{name}")
        return not fail_once("readiness")

    exit_check_armed = False

    def is_running(name: str) -> bool:
        nonlocal exit_check_armed
        if failed_stage == "process-exit" and running[name]:
            if exit_check_armed and fail_once("process-exit"):
                running[name] = False
                return False
            exit_check_armed = True
        return running[name]

    def run_command(spec, _environment) -> None:
        events.append(spec.name)
        if fail_once(spec.name):
            raise RuntimeError(f"injected {spec.name} failure")

    runner.execute_hook = MagicMock(side_effect=hook)
    runner._start_service = MagicMock(side_effect=start)
    runner.process_manager.stop_service = MagicMock(side_effect=stop)
    runner.process_manager.wait_for_ready = MagicMock(side_effect=ready)
    runner.process_manager.is_running = MagicMock(side_effect=is_running)
    runner.process_manager.get_pid = MagicMock(
        side_effect=lambda name: 1000 if running[name] else None
    )
    port_services = {
        5200: "soulstream-orch-server",
        3105: "soulstream-soul-server-ts",
    }
    runner.process_manager.platform.is_port_owned_by_process_tree = MagicMock(
        side_effect=lambda port, _pid: running[port_services[port]]
    )

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        if failed_stage == "verify-http":
            run_manifest_deployment(
                runner,
                "soulstream",
                services,
                previous_head,
                desired_running=set(services),
            )
        else:
            with pytest.raises(DeploymentError) as exc_info:
                run_manifest_deployment(
                    runner,
                    "soulstream",
                    services,
                    previous_head,
                    desired_running=set(services),
                )

    assert running == {name: True for name in services}
    assert events.count("start:soulstream-orch-server") <= 2
    assert events.count("start:soulstream-soul-server-ts") <= 2
    journal = DeploymentStateStore(runner.config_dir / ".haniel" / "deployments").read(
        "soulstream"
    )
    assert journal is not None
    if failed_stage == "verify-http":
        assert events.count("verify-http") == 2
        assert "recover" not in events
        assert journal["state"] == "success"
        assert journal["recovered"] is False
        assert get_head(repo) != previous_head
        return

    assert exc_info.value.recovered is True
    assert journal["state"] == "failed"
    assert journal["recovered"] is True
    if failed_stage in {"build", "preflight", "backup", "verify-backup"}:
        assert get_head(repo) == previous_head
    else:
        assert "recover" in events
