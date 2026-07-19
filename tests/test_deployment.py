"""Migration-aware deployment contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from haniel.core.deployment import (
    CommandSpec,
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
    ReleaseManifest,
)


def command(name: str) -> CommandSpec:
    return CommandSpec(name=name, command=f"run-{name}")


def manifest(*, destructive: bool = True) -> ReleaseManifest:
    migration: dict[str, object] = {
        "destructive": destructive,
        "preflight": command("preflight"),
        "apply": command("migrate"),
    }
    if destructive:
        migration |= {
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
        }
    return ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "release-042",
            "migration": migration,
            "post_start_verify": [command("verify-http"), command("verify-mcp")],
            "recovery": {
                "strategy": "roll_forward",
                "command": command("recover"),
                "fallback": command("restore-backup"),
            },
        }
    )


def callbacks(events: list[str]) -> DeploymentCallbacks:
    return DeploymentCallbacks(
        build=lambda: events.append("build"),
        stop=lambda: events.append("stop"),
        start_and_wait=lambda: events.append("start-and-wait"),
        rollback=lambda: events.append("rollback"),
        prepare_roll_forward=lambda: events.append("prepare-roll-forward"),
    )


def coordinator(
    tmp_path: Path,
    events: list[str],
    *,
    fail_command: str | None = None,
    fail_once: str | None = None,
) -> DeploymentCoordinator:
    failures = {fail_once: 1} if fail_once else {}

    def run(spec: CommandSpec, _env: dict[str, str]) -> None:
        events.append(spec.name)
        if spec.name == fail_command:
            raise RuntimeError(f"{spec.name} failed")
        if failures.get(spec.name, 0):
            failures[spec.name] -= 1
            raise RuntimeError(f"{spec.name} failed once")

    return DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path / "state"),
        command_runner=run,
    )


def test_destructive_manifest_requires_backup_verify_and_recovery() -> None:
    with pytest.raises(ValidationError, match="verify_backup"):
        ReleaseManifest.model_validate(
            {
                "schema_version": "haniel.release.v1",
                "release_id": "unsafe",
                "migration": {
                    "destructive": True,
                    "preflight": command("preflight"),
                    "backup": command("backup"),
                    "apply": command("migrate"),
                },
                "post_start_verify": [command("verify")],
                "recovery": {
                    "strategy": "roll_forward",
                    "command": command("recover"),
                },
            }
        )


def test_load_manifest_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps({"schema_version": "haniel.release.v2"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="haniel.release.v1"):
        ReleaseManifest.load(path)


def test_success_waits_for_build_preflight_migration_readiness_and_verify(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events)

    result = deploy.execute(
        repo_name="soulstream",
        previous_head="old",
        target_head="new",
        manifest=manifest(),
        callbacks=callbacks(events),
    )

    assert result.status == "success"
    assert result.recovered is False
    assert events == [
        "build",
        "preflight",
        "stop",
        "backup",
        "verify-backup",
        "migrate",
        "start-and-wait",
        "verify-http",
        "verify-mcp",
    ]
    journal = DeploymentStateStore(tmp_path / "state").read("soulstream")
    assert [entry["state"] for entry in journal["history"]] == [
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
        "success",
    ]


@pytest.mark.parametrize(
    "failure,expected_prefix",
    [
        ("build", ["build"]),
        ("preflight", ["build", "preflight"]),
        (
            "verify-backup",
            ["build", "preflight", "stop", "backup", "verify-backup"],
        ),
    ],
)
def test_failure_before_migration_commit_restores_previous_release(
    tmp_path: Path,
    failure: str,
    expected_prefix: list[str],
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, fail_command=failure)
    deploy_callbacks = callbacks(events)
    if failure == "build":

        def fail_build() -> None:
            events.append("build")
            raise RuntimeError("build failed")

        deploy_callbacks = DeploymentCallbacks(
            build=fail_build,
            stop=deploy_callbacks.stop,
            start_and_wait=deploy_callbacks.start_and_wait,
            rollback=deploy_callbacks.rollback,
            prepare_roll_forward=deploy_callbacks.prepare_roll_forward,
        )

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="soulstream",
            previous_head="old",
            target_head="new",
            manifest=manifest(),
            callbacks=deploy_callbacks,
        )

    assert exc_info.value.recovered is True
    assert events == [*expected_prefix, "rollback"]
    journal = DeploymentStateStore(tmp_path / "state").read("soulstream")
    assert journal["state"] == "failed"
    assert journal["recovered"] is True


def test_migration_failure_enters_declared_recovery_and_rolls_forward(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, fail_once="migrate")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="soulstream",
            previous_head="old",
            target_head="new",
            manifest=manifest(),
            callbacks=callbacks(events),
        )

    assert exc_info.value.recovered is True
    assert events == [
        "build",
        "preflight",
        "stop",
        "backup",
        "verify-backup",
        "migrate",
        "prepare-roll-forward",
        "recover",
        "start-and-wait",
        "verify-http",
        "verify-mcp",
    ]


def test_post_start_failure_runs_roll_forward_and_reverifies(tmp_path: Path) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, fail_once="verify-http")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="soulstream",
            previous_head="old",
            target_head="new",
            manifest=manifest(),
            callbacks=callbacks(events),
        )

    assert exc_info.value.recovered is True
    assert events == [
        "build",
        "preflight",
        "stop",
        "backup",
        "verify-backup",
        "migrate",
        "start-and-wait",
        "verify-http",
        "prepare-roll-forward",
        "recover",
        "start-and-wait",
        "verify-http",
        "verify-mcp",
    ]


def test_persistent_roll_forward_failure_restores_backup_and_previous_release(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, fail_command="verify-http")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="soulstream",
            previous_head="old",
            target_head="new",
            manifest=manifest(),
            callbacks=callbacks(events),
        )

    assert exc_info.value.recovered is True
    assert events == [
        "build",
        "preflight",
        "stop",
        "backup",
        "verify-backup",
        "migrate",
        "start-and-wait",
        "verify-http",
        "prepare-roll-forward",
        "recover",
        "start-and-wait",
        "verify-http",
        "prepare-roll-forward",
        "restore-backup",
        "rollback",
    ]
    journal = DeploymentStateStore(tmp_path / "state").read("soulstream")
    assert journal["state"] == "failed"
    assert journal["recovered"] is True


def test_same_successful_target_is_idempotent(tmp_path: Path) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events)
    args = {
        "repo_name": "soulstream",
        "previous_head": "old",
        "target_head": "new",
        "manifest": manifest(),
        "callbacks": callbacks(events),
    }

    first = deploy.execute(**args)
    second = deploy.execute(**args)

    assert first.status == "success"
    assert second.status == "success"
    assert second.skipped is True
    assert events.count("migrate") == 1
