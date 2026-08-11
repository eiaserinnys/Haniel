"""ServiceRunner integration tests for the release manifest handover."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.deploy_retry_planner import DeployRetryPlanner
from haniel.core.deployment import (
    CommandResult,
    CommandSpec,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
)
from haniel.core.deployment_errors import (
    stable_deployment_error_code,
)
from haniel.core.git import get_head, reset_repo_to as actual_reset_repo_to
from haniel.core.lifecycle_control import LifecycleConflict, LifecycleControl
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import (
    RunnerDeploymentAdapter,
    run_manifest_deployment,
)


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
            "fallback": command("prepare-previous-release"),
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


def test_manifest_generation_drift_during_load_blocks_stale_service_actions(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    original_load = ReleaseManifest.load
    runner.execute_hook = MagicMock(return_value=True)

    def load_and_reload(path: Path) -> ReleaseManifest:
        manifest = original_load(path)
        snapshot = runner._snapshot_config_state()
        replacement = snapshot.config.model_copy(deep=True)
        replacement.services["app"] = replacement.services["app"].model_copy(
            update={"enabled": False}
        )
        runner._replace_config_snapshot(replacement, snapshot.generation)
        return manifest

    with (
        patch(
            "haniel.core.runner_deployment.ReleaseManifest.load",
            side_effect=load_and_reload,
        ),
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=lambda _command, _environment: None,
        ),
        pytest.raises(DeploymentError) as caught,
    ):
        run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert stable_deployment_error_code(caught.value) == "CONFIG_GENERATION_CHANGED"
    runner.execute_hook.assert_not_called()


def test_reload_during_apply_blocks_stale_start_and_rollback_service_actions(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["recovery"] = {
        "strategy": "rollback",
        "command": {"name": "restore", "command": "run-restore"},
    }
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    events: list[str] = []
    running = configure_processes(runner, events)
    runner.execute_hook = MagicMock(return_value=True)

    def start_current(service: str) -> bool:
        current = runner._snapshot_config_state().enabled_services[service]
        events.append(f"start-current:{current.run}")
        running[service] = True
        return True

    runner._start_service = MagicMock(side_effect=start_current)

    def run_command(
        command: CommandSpec, _environment: dict[str, str]
    ) -> CommandResult:
        events.append(f"command:{command.name}")
        if command.name == "migrate":
            snapshot = runner._snapshot_config_state()
            replacement = snapshot.config.model_copy(deep=True)
            replacement.services["app"] = replacement.services["app"].model_copy(
                update={"run": "current-generation-app"}
            )
            runner._replace_config_snapshot(replacement, snapshot.generation)
            events.append("config:reloaded")
        return CommandResult(stdout="", json_data=None)

    with (
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=run_command,
        ),
        pytest.raises(DeploymentError) as caught,
    ):
        run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert stable_deployment_error_code(caught.value) == "CONFIG_GENERATION_CHANGED"
    assert get_head(repo) == previous_head
    assert "config:reloaded" in events
    runner._start_service.assert_called_once_with("app")
    assert "start-current:current-generation-app" in events


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
    runner.process_manager.get_pid = MagicMock(
        side_effect=lambda name: 1000 if running[name] else None
    )
    runner.process_manager.stop_service = MagicMock(side_effect=stop)
    runner.process_manager.wait_for_ready = MagicMock(
        side_effect=lambda name: events.append(f"ready:{name}") or True
    )
    runner._start_service = MagicMock(side_effect=start)
    runner.process_manager.platform.is_port_owned_by_process_tree = MagicMock(
        side_effect=lambda _port, _pid: running["app"]
    )
    return running


@contextmanager
def resident_owner(
    runner: ServiceRunner, instance_id: str = "owner-1"
) -> Iterator[None]:
    config_path = runner.config_dir / "haniel.yaml"
    lifecycle = LifecycleControl(config_path)
    runner.lifecycle_control = lifecycle
    runner.lifecycle_instance_id = instance_id
    with lifecycle.acquire_owner(instance_id):
        yield


def write_manifest_without_environment_service(repo: Path) -> None:
    payload = release_manifest()
    payload.pop("environment_service")
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")


def runner_with_services(
    runner: ServiceRunner, services: dict[str, ServiceConfig]
) -> ServiceRunner:
    config = runner.config.model_copy(update={"services": services})
    return ServiceRunner(config, config_dir=runner.config_dir)


def capture_manifest_command_environment(
    runner: ServiceRunner,
    previous_head: str,
    affected: list[str],
) -> dict[str, str]:
    captured: dict[str, str] = {}

    def run_command(_spec: CommandSpec, environment: dict[str, str]) -> None:
        captured.update(environment)

    def execute_one(coordinator: DeploymentCoordinator, **_kwargs) -> None:
        coordinator.command_runner(
            CommandSpec(name="preflight", command="run-preflight"), {}
        )

    with (
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=run_command,
        ),
        patch.object(
            DeploymentCoordinator,
            "execute",
            autospec=True,
            side_effect=execute_one,
        ),
    ):
        run_manifest_deployment(runner, "app", affected, previous_head)

    return captured


def test_manifest_handover_digest_matches_retry_plan_with_crlf_worktree(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    manifest_path = repo / "deploy" / "release.json"
    committed_bytes = (json.dumps(release_manifest(), indent=2) + "\n").encode()
    manifest_path.write_bytes(committed_bytes)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "commit LF manifest"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    target_head = get_head(repo)

    manifest_path.write_bytes(committed_bytes.replace(b"\n", b"\r\n"))
    working_tree_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    committed_digest = hashlib.sha256(committed_bytes).hexdigest()
    assert working_tree_digest != committed_digest

    planner = DeployRetryPlanner(
        repo_path=repo,
        manifest_path="deploy/release.json",
        journal_store=DeploymentStateStore(
            runner.config_dir / ".haniel" / "deployments"
        ),
    )
    plan = planner.plan(
        {
            "target_head": target_head,
            "repo": "app",
            "node_id": "windows-node",
            "branch": "main",
            "expected_manifest_identity": "deploy/release.json",
            "expected_manifest_digest": committed_digest,
        }
    )

    captured: dict[str, object] = {}

    def capture_execute(_coordinator: DeploymentCoordinator, **kwargs) -> None:
        captured.update(kwargs)

    with patch.object(
        DeploymentCoordinator,
        "execute",
        autospec=True,
        side_effect=capture_execute,
    ):
        run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert plan.evidence["manifest_digest"] == committed_digest
    assert captured["manifest_digest"] == plan.evidence["manifest_digest"]


def test_manifest_command_infers_single_repo_service_cwd(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    original, repo, previous_head = manifest_runner
    write_manifest_without_environment_service(repo)
    runner = runner_with_services(
        original,
        {"node-service": ServiceConfig(run="node", repo="app", cwd="./services/app")},
    )

    environment = capture_manifest_command_environment(
        runner, previous_head, ["node-service"]
    )

    assert environment["HANIEL_SERVICE_CWD"] == str(
        (runner.config_dir / "services" / "app").resolve()
    )


def test_manifest_command_infers_shared_cwd_for_multiple_repo_services(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    original, repo, previous_head = manifest_runner
    write_manifest_without_environment_service(repo)
    runner = runner_with_services(
        original,
        {
            "orch": ServiceConfig(run="orch", repo="app", cwd="./services/app"),
            "soul": ServiceConfig(run="soul", repo="app", cwd="./services/app"),
            "disabled": ServiceConfig(
                run="disabled",
                repo="app",
                cwd="./services/disabled",
                enabled=False,
            ),
            "dependent": ServiceConfig(
                run="dependent",
                repo="other-repo",
                cwd="./services/dependent",
                after=["orch"],
            ),
        },
    )

    environment = capture_manifest_command_environment(
        runner, previous_head, ["orch", "soul", "dependent"]
    )

    assert environment["HANIEL_SERVICE_CWD"] == str(
        (runner.config_dir / "services" / "app").resolve()
    )


def test_manifest_command_omits_service_cwd_without_repo_services(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    original, repo, previous_head = manifest_runner
    write_manifest_without_environment_service(repo)
    runner = runner_with_services(original, {})

    environment = capture_manifest_command_environment(runner, previous_head, [])

    assert "HANIEL_SERVICE_CWD" not in environment


def test_manifest_command_omits_service_cwd_when_repo_services_disagree(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    original, repo, previous_head = manifest_runner
    write_manifest_without_environment_service(repo)
    runner = runner_with_services(
        original,
        {
            "orch": ServiceConfig(run="orch", repo="app", cwd="./services/orch"),
            "soul": ServiceConfig(run="soul", repo="app", cwd="./services/soul"),
        },
    )

    environment = capture_manifest_command_environment(
        runner, previous_head, ["orch", "soul"]
    )

    assert "HANIEL_SERVICE_CWD" not in environment


@pytest.mark.parametrize(
    ("services", "affected", "environment_service", "expected_error"),
    [
        (
            {"other": ServiceConfig(run="other", repo="other-repo")},
            [],
            "other",
            "manifest environment_service is not affected: other",
        ),
        (
            {"disabled": ServiceConfig(run="disabled", repo="app", enabled=False)},
            ["disabled"],
            "disabled",
            "manifest environment_service is not enabled: disabled",
        ),
    ],
)
def test_explicit_environment_service_keeps_strict_validation(
    manifest_runner: tuple[ServiceRunner, Path, str],
    services: dict[str, ServiceConfig],
    affected: list[str],
    environment_service: str,
    expected_error: str,
) -> None:
    original, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["environment_service"] = environment_service
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    runner = runner_with_services(original, services)

    with pytest.raises(ValueError, match=expected_error):
        capture_manifest_command_environment(runner, previous_head, affected)


def test_manifest_handover_waits_for_readiness_and_post_verify(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    events: list[str] = []
    progress: list[str] = []
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
        run_manifest_deployment(
            runner,
            "app",
            ["app"],
            previous_head,
            progress_callback=progress.append,
        )

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
    assert progress == [
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
    ]


def test_manifest_requiring_service_env_rejects_legacy_runtime_without_digest(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["requires_service_env_file"] = True
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="CONFIG_DIGEST_REQUIRED"):
        run_manifest_deployment(runner, "app", ["app"], previous_head)


def test_progress_reporting_failure_does_not_abort_manifest_handover(
    manifest_runner: tuple[ServiceRunner, Path, str], caplog
) -> None:
    runner, _repo, previous_head = manifest_runner
    configure_processes(runner, [])
    runner.execute_hook = MagicMock(return_value=True)

    def fail_progress(stage: str) -> None:
        raise RuntimeError(f"progress transport unavailable at {stage}")

    with (
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=lambda _spec, _env: None,
        ),
        caplog.at_level("WARNING"),
    ):
        run_manifest_deployment(
            runner,
            "app",
            ["app"],
            previous_head,
            progress_callback=fail_progress,
        )

    assert "Failed to report deploy progress build" in caplog.text


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


def test_build_failure_does_not_probe_service_already_down_before_deployment(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    running["app"] = False
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
    assert running["app"] is False
    runner.process_manager.get_pid.assert_not_called()
    runner.process_manager.platform.is_port_owned_by_process_tree.assert_not_called()
    assert not any(event.startswith(("start:", "stop:")) for event in events)
    assert get_head(repo) == previous_head


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


def test_contract_upgrade_recovery_orders_database_repo_build_start_health(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["migration"]["operation"] = "discover"
    payload["migration"]["result_contract"] = "soulstream.database-release.v1"
    payload["recovery"] = {
        "strategy": "rollback",
        "command": {"name": "restore", "command": "run-restore"},
    }
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    events: list[str] = []
    running = configure_processes(runner, events)
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: events.append(f"{hook}:{name}") or True
    )
    runner.process_manager.platform.is_port_owned_by_process_tree = MagicMock(
        side_effect=lambda _port, _pid: (
            events.append("recovery-health") or running["app"]
        )
    )

    def run_command(spec, _env):
        events.append(spec.name)
        if spec.name == "preflight":
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "upgrade",
                    "journal_path": str(repo / "database-release.json"),
                },
            )
        if spec.name == "verify-http":
            raise RuntimeError("post verify failed")
        return None

    def reset(path: Path, revision: str) -> None:
        events.append("repo-reset")
        actual_reset_repo_to(path, revision)

    with (
        resident_owner(runner),
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=run_command,
        ),
        patch("haniel.core.runner_deployment.reset_repo_to", side_effect=reset),
        pytest.raises(DeploymentError),
    ):
        run_manifest_deployment(
            runner,
            "app",
            ["app"],
            previous_head,
            expected_operation="upgrade",
            request_id="request-1",
        )

    ordered = [
        "restore",
        "stop:app",
        "repo-reset",
        "post_pull:app",
        "start:app",
        "ready:app",
        "recovery-health",
    ]
    cursor = 0
    for event in events:
        if cursor < len(ordered) and event == ordered[cursor]:
            cursor += 1
    assert cursor == len(ordered), events
    terminal = runner.lifecycle_control.read_result("request-1")["terminal"]
    assert terminal["error"]["code"] == "POST_VERIFY_FAILED"


def test_contract_upgrade_requires_active_resident_before_any_mutation(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["migration"]["operation"] = "discover"
    payload["migration"]["result_contract"] = "soulstream.database-release.v1"
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    runner.execute_hook = MagicMock(return_value=True)
    runner.process_manager.stop_service = MagicMock(return_value=True)

    with pytest.raises(LifecycleConflict, match="LIFECYCLE_OWNER_REQUIRED"):
        run_manifest_deployment(
            runner,
            "app",
            ["app"],
            previous_head,
            expected_operation="upgrade",
            request_id="request-1",
        )

    runner.execute_hook.assert_not_called()
    runner.process_manager.stop_service.assert_not_called()
    assert not (runner.config_dir / ".haniel" / "deployments" / "app.json").exists()


@pytest.mark.parametrize("changed", ["repo", "target", "operation"])
def test_existing_runtime_request_identity_mismatch_blocks_before_mutation(
    manifest_runner: tuple[ServiceRunner, Path, str],
    changed: str,
) -> None:
    runner, repo, previous_head = manifest_runner
    target_head = get_head(repo)
    payload = {
        "kind": "runtime-handover",
        "repo": "app",
        "target_ref": target_head,
        "expected_operation": "upgrade",
        "executor_instance": "owner-1",
    }
    payload[
        {
            "repo": "repo",
            "target": "target_ref",
            "operation": "expected_operation",
        }[changed]
    ] = {
        "repo": "other",
        "target": "different-target",
        "operation": "fresh_install",
    }[changed]

    with resident_owner(runner):
        runner.lifecycle_control.submit_request("request-1", payload)
        with (
            patch.object(runner, "execute_hook") as hook,
            pytest.raises(LifecycleConflict, match="REQUEST_IDENTITY_CONFLICT"),
        ):
            run_manifest_deployment(
                runner,
                "app",
                ["app"],
                previous_head,
                expected_operation="upgrade",
                request_id="request-1",
            )

    hook.assert_not_called()
    assert not (runner.config_dir / ".haniel" / "deployments" / "app.json").exists()


def test_digest_bound_runtime_request_rejects_legacy_digest_omission(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    target_head = get_head(repo)
    payload = {
        "kind": "runtime-handover",
        "repo": "app",
        "target_ref": target_head,
        "expected_operation": "upgrade",
        "executor_instance": "owner-1",
        "config_digest": "a" * 64,
    }

    with resident_owner(runner):
        runner.lifecycle_control.submit_request("request-1", payload)
        with (
            patch.object(runner, "execute_hook") as hook,
            pytest.raises(LifecycleConflict, match="REQUEST_IDENTITY_CONFLICT"),
        ):
            run_manifest_deployment(
                runner,
                "app",
                ["app"],
                previous_head,
                expected_operation="upgrade",
                request_id="request-1",
                config_digest=None,
            )

    hook.assert_not_called()
    assert not (runner.config_dir / ".haniel" / "deployments" / "app.json").exists()


def test_one_shot_request_requires_matching_staged_target_journal(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    store = DeploymentStateStore(runner.config_dir / ".haniel" / "deployments")

    with resident_owner(runner):
        runner.lifecycle_control.submit_request(
            "request-1",
            {
                "kind": "handover",
                "repo": "app",
                "target_ref": "origin/main",
                "expected_operation": "upgrade",
            },
        )
        store.begin_handover(
            "app",
            previous_head=previous_head,
            target_ref="different-target",
            manifest_identity="deploy/release.json",
            request_id="request-1",
            expected_operation="upgrade",
            branch="main",
        )
        store.bind_handover_target(
            "app",
            request_id="request-1",
            target_head="different-target",
            release_id="release-042",
            manifest_digest="digest",
        )

        with pytest.raises(LifecycleConflict, match="REQUEST_IDENTITY_CONFLICT"):
            run_manifest_deployment(
                runner,
                "app",
                ["app"],
                previous_head,
                expected_operation="upgrade",
                request_id="request-1",
            )

    assert store.read("app")["target_head"] == "different-target"


@pytest.mark.parametrize("failure", ["second-start", "readiness", "post-verify"])
def test_fresh_failure_stops_every_partial_target_without_repo_or_database_restore(
    manifest_runner: tuple[ServiceRunner, Path, str],
    failure: str,
) -> None:
    original, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["migration"]["operation"] = "discover"
    payload["migration"]["result_contract"] = "soulstream.database-release.v1"
    payload.pop("environment_service", None)
    payload["recovery"] = {
        "strategy": "rollback",
        "command": {"name": "restore", "command": "run-restore"},
    }
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    config = original.config.model_copy(
        update={
            "services": {
                "first": ServiceConfig(run="first", repo="app"),
                "second": ServiceConfig(run="second", repo="app", after=["first"]),
            }
        }
    )
    runner = ServiceRunner(config, config_dir=original.config_dir)
    running = {"first": False, "second": False}
    events: list[str] = []
    runner.process_manager.is_running = MagicMock(
        side_effect=lambda name: running[name]
    )
    runner.process_manager.stop_service = MagicMock(
        side_effect=lambda name: (
            events.append(f"stop:{name}") or running.__setitem__(name, False) or True
        )
    )

    def start(name: str) -> bool:
        events.append(f"start:{name}")
        if failure == "second-start" and name == "second":
            return False
        running[name] = True
        return True

    runner._start_service = MagicMock(side_effect=start)
    runner.process_manager.wait_for_ready = MagicMock(
        side_effect=lambda name: not (failure == "readiness" and name == "second")
    )
    runner.execute_hook = MagicMock(return_value=True)

    def run_command(spec, _env):
        events.append(spec.name)
        if spec.name == "preflight":
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "fresh_install",
                    "journal_path": str(repo / "database-release.json"),
                },
            )
        if spec.name == "migrate":
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={"ok": True, "phase": "applied"},
            )
        if failure == "post-verify" and spec.name == "verify-http":
            raise RuntimeError("post verify failed")
        return None

    with (
        resident_owner(runner),
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=run_command,
        ),
        patch("haniel.core.runner_deployment.reset_repo_to") as reset,
        pytest.raises(DeploymentError),
    ):
        run_manifest_deployment(
            runner,
            "app",
            ["first", "second"],
            previous_head,
            expected_operation="fresh_install",
            request_id="request-1",
        )

    assert running == {"first": False, "second": False}
    assert "restore" not in events
    reset.assert_not_called()
    journal = DeploymentStateStore(runner.config_dir / ".haniel" / "deployments").read(
        "app"
    )
    assert journal is not None
    assert journal["state"] == "failed"
    assert journal["database_last_result_phase"] == "apply"
    assert journal["database_last_result"]["phase"] == "applied"
    assert get_head(repo) != previous_head


def test_quiescence_receipt_and_fresh_cleanup_use_real_managed_processes(
    tmp_path: Path,
) -> None:
    sleep_command = f'{sys.executable} -c "import time; time.sleep(30)"'
    config = HanielConfig(
        repos={"app": RepoConfig(url="git@example/app", path="./repo")},
        services={
            "first": ServiceConfig(run=sleep_command, repo="app"),
            "second": ServiceConfig(
                run=sleep_command,
                repo="app",
                after=["first"],
            ),
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    adapter = RunnerDeploymentAdapter(
        runner,
        "app",
        ["first", "second"],
        tmp_path / "repo",
        "absent",
        "target",
        "request-1",
    )
    runner.process_manager.start_service("first", runner._enabled_services["first"])
    try:
        assert runner.process_manager.is_running("first")
        assert not runner.process_manager.is_running("second")

        receipt = adapter.stop()

        assert receipt["stopped_services"] == ["first"]
        assert receipt["already_stopped_services"] == ["second"]
        assert receipt["quiesced_services"] == ["first", "second"]
        assert receipt["quiescence_nonce"] == adapter.quiescence_nonce
        assert not runner.process_manager.is_running("first")
        assert not runner.process_manager.is_running("second")

        runner.process_manager.start_service("first", runner._enabled_services["first"])
        assert runner.process_manager.is_running("first")
        adapter.stop_partial()

        assert not runner.process_manager.is_running("first")
        assert not runner.process_manager.is_running("second")
    finally:
        runner.process_manager.stop_all(timeout=1)


@pytest.mark.parametrize("failure", ["second-start", "readiness", "post-verify"])
@pytest.mark.parametrize(
    "contract_mode", [True, False], ids=["contract", "legacy-optional-fields"]
)
def test_fresh_coordinator_failure_stops_actual_partial_processes(
    manifest_runner: tuple[ServiceRunner, Path, str],
    failure: str,
    contract_mode: bool,
) -> None:
    original, repo, _previous_head = manifest_runner
    payload = release_manifest()
    if contract_mode:
        payload["migration"]["operation"] = "discover"
        payload["migration"]["result_contract"] = "soulstream.database-release.v1"
    payload.pop("environment_service", None)
    payload["recovery"] = {
        "strategy": "rollback",
        "command": {"name": "restore", "command": "run-restore"},
    }
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    sleep = f'{sys.executable} -c "import time; time.sleep(30)"'
    second = (
        "/definitely/not/a/haniel-test-command" if failure == "second-start" else sleep
    )
    second_ready = "log:NEVER" if failure == "readiness" else None
    config = original.config.model_copy(
        update={
            "services": {
                "first": ServiceConfig(run=sleep, repo="app"),
                "second": ServiceConfig(
                    run=second,
                    repo="app",
                    ready=second_ready,
                    after=["first"],
                ),
            }
        }
    )
    runner = ServiceRunner(config, config_dir=original.config_dir)
    runner.process_manager.DEFAULT_READY_TIMEOUT = 0.2
    command_events: list[str] = []

    def run_command(spec, _env):
        command_events.append(spec.name)
        if spec.name == "preflight":
            if not contract_mode:
                return None
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "fresh_install",
                    "journal_path": str(repo / "database-release.json"),
                },
            )
        if spec.name == "migrate":
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={"ok": True, "phase": "applied"},
            )
        if failure == "post-verify" and spec.name == "verify-http":
            raise RuntimeError("post verify failed")
        return None

    try:
        with (
            resident_owner(runner),
            patch(
                "haniel.core.runner_deployment.subprocess_command_runner",
                return_value=run_command,
            ),
            patch("haniel.core.runner_deployment.reset_repo_to") as reset,
            pytest.raises(DeploymentError),
        ):
            run_manifest_deployment(
                runner,
                "app",
                ["first", "second"],
                "absent",
                expected_operation="fresh_install",
                request_id=f"fresh-actual-{failure}",
            )

        assert "first" in runner.process_manager._processes
        assert not runner.process_manager.is_running("first")
        assert not runner.process_manager.is_running("second")
        assert runner.process_manager._processes["first"].process is None
        assert "restore" not in command_events
        reset.assert_not_called()
        assert get_head(repo) != "absent"
    finally:
        runner.process_manager.stop_all(timeout=1)


def test_runtime_terminal_prioritizes_recovery_failure_code(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, repo, previous_head = manifest_runner
    payload = release_manifest()
    payload["migration"]["operation"] = "discover"
    payload["migration"]["result_contract"] = "soulstream.database-release.v1"
    (repo / "deploy" / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    events: list[str] = []
    configure_processes(runner, events)
    runner.execute_hook = MagicMock(return_value=True)

    def run_command(spec, _env):
        events.append(spec.name)
        if spec.name == "preflight":
            from haniel.core.deployment_command_runner import CommandResult

            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "upgrade",
                    "journal_path": str(repo / "database-release.json"),
                },
            )
        if spec.name == "verify-http":
            raise RuntimeError("POST_VERIFY_FAILED: health failed")
        return None

    with (
        resident_owner(runner),
        patch(
            "haniel.core.runner_deployment.subprocess_command_runner",
            return_value=run_command,
        ),
        patch(
            "haniel.core.runner_deployment.reset_repo_to",
            side_effect=RuntimeError("repo reset failed"),
        ),
        pytest.raises(DeploymentError) as raised,
    ):
        run_manifest_deployment(
            runner,
            "app",
            ["app"],
            previous_head,
            expected_operation="upgrade",
            request_id="request-recovery-failed",
        )

    assert raised.value.recovery_error is not None
    assert "recover" in events
    terminal = runner.lifecycle_control.read_result("request-recovery-failed")[
        "terminal"
    ]
    assert terminal["error"]["code"] == "RECOVERY_FAILED"


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


def test_rollback_without_restarted_service_is_reported_as_availability_down(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    running["app"] = False
    start_results = iter([True, True, False])

    def start(name: str) -> bool:
        events.append(f"start:{name}")
        started = next(start_results)
        running[name] = started
        return started

    runner._start_service = MagicMock(side_effect=start)
    runner.execute_hook = MagicMock(
        side_effect=lambda name, hook: events.append(f"{hook}:{name}") or True
    )

    def run_command(spec, _env):
        events.append(spec.name)
        if spec.name == "verify-http":
            raise RuntimeError("verification failed")

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        with pytest.raises(DeploymentError) as exc_info:
            run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert exc_info.value.recovered is False
    assert "availability down" in str(exc_info.value)
    assert events.count("start:app") == 3
    journal = DeploymentStateStore(runner.config_dir / ".haniel" / "deployments").read(
        "app"
    )
    assert journal is not None
    assert journal["state"] == "failed"
    assert journal["recovered"] is False
    assert "availability down" in journal["history"][-1]["message"]


def test_rollback_process_without_ready_port_is_reported_as_availability_down(
    manifest_runner: tuple[ServiceRunner, Path, str],
) -> None:
    runner, _repo, previous_head = manifest_runner
    events: list[str] = []
    running = configure_processes(runner, events)
    running["app"] = False
    runner.process_manager.platform.is_port_owned_by_process_tree = MagicMock(
        return_value=False
    )
    runner.execute_hook = MagicMock(return_value=True)

    def run_command(spec, _env):
        if spec.name == "verify-http":
            raise RuntimeError("verification failed")

    with patch(
        "haniel.core.runner_deployment.subprocess_command_runner",
        return_value=run_command,
    ):
        with pytest.raises(DeploymentError) as exc_info:
            run_manifest_deployment(runner, "app", ["app"], previous_head)

    assert exc_info.value.recovered is False
    assert running["app"] is True
    journal = DeploymentStateStore(runner.config_dir / ".haniel" / "deployments").read(
        "app"
    )
    assert journal is not None
    assert journal["recovered"] is False
    assert (
        "app (port 9999 not owned by process 1000)" in journal["history"][-1]["message"]
    )
