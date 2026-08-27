"""Strict RED contracts for removing Haniel-owned inline backups."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from haniel.core.deployment import (
    CommandSpec,
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentResult,
    DeploymentStateStore,
    ReleaseManifest,
)
from haniel.core.deployment_command_runner import CommandResult


SUCCESS_TRACE = [
    "build",
    "preflight",
    "stop",
    "apply",
    "start",
    "health",
]
APPLY_FAILURE_TRACE = [
    "build",
    "preflight",
    "stop",
    "apply",
    "recover-database",
    "repo-rollback",
]
START_FAILURE_TRACE = [
    "build",
    "preflight",
    "stop",
    "apply",
    "start",
    "recover-database",
    "repo-rollback",
]


def command(name: str) -> dict[str, str]:
    return {"name": name, "command": f"run-{name}"}


def manifest_payload(*, include_inline_backup_commands: bool) -> dict[str, object]:
    migration: dict[str, object] = {
        "destructive": True,
        "operation": "discover",
        "result_contract": "soulstream.database-release.v1",
        "preflight": command("preflight"),
        "apply": command("apply"),
    }
    if include_inline_backup_commands:
        migration |= {
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
        }
    return {
        "schema_version": "haniel.release.v1",
        "release_id": "no-inline-backup-contract",
        "migration": migration,
        "post_start_verify": [command("health")],
        "post_start_verify_retry": {
            "max_attempts": 1,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
            "total_grace_seconds": 0,
        },
        "recovery": {
            "strategy": "rollback",
            "command": command("recover-database"),
        },
    }


def upgrade_manifest(*, include_inline_backup_commands: bool) -> ReleaseManifest:
    return ReleaseManifest.model_validate(
        manifest_payload(
            include_inline_backup_commands=include_inline_backup_commands,
        )
    )


def manifest_without_inline_commands_bypassing_current_gate() -> ReleaseManifest:
    """Build the target shape without weakening the production schema."""
    approved = upgrade_manifest(include_inline_backup_commands=True)
    assert approved.migration is not None
    migration = approved.migration.model_copy(
        update={"backup": None, "verify_backup": None}
    )
    return approved.model_copy(update={"migration": migration})


def quiescence_receipt() -> dict[str, object]:
    return {
        "request_id": "request-1",
        "repo": "app",
        "target_head": "target",
        "owner_instance": "owner-1",
        "quiescence_nonce": "nonce-1",
        "stopped_services": ["app"],
        "already_stopped_services": [],
        "quiesced_services": ["app"],
    }


def callbacks(
    events: list[str],
    *,
    fail_start: bool = False,
) -> DeploymentCallbacks:
    def start() -> None:
        events.append("start")
        if fail_start:
            raise RuntimeError("start failed")

    return DeploymentCallbacks(
        build=lambda: events.append("build"),
        stop=lambda: events.append("stop") or quiescence_receipt(),
        start_and_wait=start,
        rollback=lambda: events.append("repo-rollback"),
        prepare_roll_forward=lambda: events.append("prepare-roll-forward"),
        writer_services=("app",),
        owner_instance="owner-1",
        quiescence_nonce="nonce-1",
    )


def coordinator(
    tmp_path: Path,
    events: list[str],
    *,
    fail_command: str | None = None,
) -> DeploymentCoordinator:
    def run(spec: CommandSpec, _environment: dict[str, str]) -> CommandResult:
        events.append(spec.name)
        if spec.name == fail_command:
            raise RuntimeError(f"{spec.name} failed")
        if spec.name == "preflight":
            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "upgrade",
                    "journal_path": str(tmp_path / "database-release.json"),
                },
            )
        return CommandResult(stdout="", json_data=None)

    return DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path / "deployments"),
        command_runner=run,
    )


def execute_upgrade(
    deploy: DeploymentCoordinator,
    *,
    manifest: ReleaseManifest,
    deploy_callbacks: DeploymentCallbacks,
) -> DeploymentResult:
    return deploy.execute(
        repo_name="app",
        previous_head="old",
        target_head="target",
        manifest=manifest,
        callbacks=deploy_callbacks,
        expected_operation="upgrade",
        request_id="request-1",
    )


def assert_owned_trace(actual: list[str], expected: list[str]) -> None:
    missing = [phase for phase in expected if phase not in actual]
    assert not missing, f"missing phase owner(s): {missing}"
    assert actual == expected, f"unexpected phase order: {actual}"


def test_destructive_manifest_accepts_no_inline_backup_commands() -> None:
    approved = upgrade_manifest(include_inline_backup_commands=False)

    assert approved.migration is not None
    assert approved.migration.destructive is True
    assert approved.migration.backup is None
    assert approved.migration.verify_backup is None


def test_upgrade_ignores_inline_backup_commands_and_preserves_success_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events)

    result = execute_upgrade(
        deploy,
        manifest=upgrade_manifest(include_inline_backup_commands=True),
        deploy_callbacks=callbacks(events),
    )

    assert result.status == "success"
    assert result.recovered is False
    assert_owned_trace(events, SUCCESS_TRACE)
    journal = DeploymentStateStore(tmp_path / "deployments").read("app")
    assert journal is not None
    assert [entry["state"] for entry in journal["history"]] == [
        "build",
        "preflight",
        "migrating",
        "starting",
        "verifying",
        "success",
    ]


def test_apply_failure_keeps_database_then_repository_recovery_owners(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, fail_command="apply")

    with pytest.raises(DeploymentError) as raised:
        execute_upgrade(
            deploy,
            manifest=upgrade_manifest(include_inline_backup_commands=True),
            deploy_callbacks=callbacks(events),
        )

    assert raised.value.recovered is True
    assert_owned_trace(events, APPLY_FAILURE_TRACE)


def test_start_failure_keeps_database_then_repository_recovery_owners(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events)

    with pytest.raises(DeploymentError) as raised:
        execute_upgrade(
            deploy,
            manifest=upgrade_manifest(include_inline_backup_commands=True),
            deploy_callbacks=callbacks(events, fail_start=True),
        )

    assert raised.value.recovered is True
    assert_owned_trace(events, START_FAILURE_TRACE)


@pytest.mark.parametrize(
    ("deleted_phase", "failure_path"),
    [
        ("apply", False),
        ("start", False),
        ("health", False),
        ("recover-database", True),
        ("repo-rollback", True),
    ],
)
def test_preservation_oracle_rejects_deleted_phase_owner(
    deleted_phase: str,
    failure_path: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    deploy = coordinator(
        tmp_path,
        events,
        fail_command="apply" if failure_path else None,
    )
    deploy_callbacks = callbacks(events)

    if deleted_phase in {"apply", "recover-database"}:
        original_run_database = deploy._run_database

        def run_database_without_owner(
            repo_name: str,
            phase: str,
            spec: CommandSpec,
            environment: dict[str, str],
        ) -> CommandResult | None:
            if spec.name == deleted_phase:
                return None
            return original_run_database(repo_name, phase, spec, environment)

        monkeypatch.setattr(deploy, "_run_database", run_database_without_owner)
    elif deleted_phase == "start":
        deploy_callbacks = replace(deploy_callbacks, start_and_wait=lambda: None)
    elif deleted_phase == "health":
        monkeypatch.setattr(deploy, "_verify", lambda *_args: None)
    else:
        deploy_callbacks = replace(deploy_callbacks, rollback=lambda: None)

    if failure_path:
        with pytest.raises(DeploymentError) as raised:
            execute_upgrade(
                deploy,
                manifest=manifest_without_inline_commands_bypassing_current_gate(),
                deploy_callbacks=deploy_callbacks,
            )
        assert raised.value.recovered is True
        expected = APPLY_FAILURE_TRACE
    else:
        result = execute_upgrade(
            deploy,
            manifest=manifest_without_inline_commands_bypassing_current_gate(),
            deploy_callbacks=deploy_callbacks,
        )
        assert result.status == "success"
        expected = SUCCESS_TRACE

    with pytest.raises(AssertionError, match="missing phase owner"):
        assert_owned_trace(events, expected)


@pytest.mark.parametrize("restored_phase", ["backup", "verify-backup"])
def test_execution_oracle_rejects_restored_inline_backup(
    restored_phase: str,
) -> None:
    mutated = [*SUCCESS_TRACE[:3], restored_phase, *SUCCESS_TRACE[3:]]

    with pytest.raises(AssertionError, match="unexpected phase order"):
        assert_owned_trace(mutated, SUCCESS_TRACE)
