"""Migration-aware deployment contract tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from haniel.core.deployment import (
    CommandSpec,
    DeploymentCommandError,
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
    ReleaseManifest,
    subprocess_command_runner,
    stable_deployment_error_code,
)
from haniel.core.deployment_errors import KNOWN_DEPLOYMENT_ERROR_CODES
from haniel.core.deployment_errors import StableDeploymentError


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


def test_recovery_failure_code_wins_over_typed_recovery_child() -> None:
    error = DeploymentError(
        "apply failed and rollback timed out",
        recovered=False,
        recovery_error=DeploymentCommandError(
            "COMMAND_TIMEOUT",
            "restore",
            "restore timed out",
        ),
    )

    assert stable_deployment_error_code(error) == "RECOVERY_FAILED"


def test_coordinator_recovery_journal_preserves_typed_failure_code(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def run(spec: CommandSpec, _env: dict[str, str]) -> None:
        events.append(spec.name)
        if spec.name in {"migrate", "recover", "restore-backup"}:
            raise RuntimeError(f"{spec.name} failed")

    deploy = DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path / "state"),
        command_runner=run,
    )

    with pytest.raises(DeploymentError) as raised:
        deploy.execute(
            repo_name="soulstream",
            previous_head="old",
            target_head="new",
            manifest=manifest(),
            callbacks=callbacks(events),
        )

    assert raised.value.recovery_error is not None
    journal = DeploymentStateStore(tmp_path / "state").read("soulstream")
    assert journal is not None
    assert journal["error_code"] == "RECOVERY_FAILED"


def test_untyped_message_does_not_create_journal_error_code(tmp_path: Path) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin("app", "old", "new", "release")

    store.transition(
        "app",
        "failed",
        message="PULL_FAILED happened after CONFIG_DIGEST_MISMATCH",
    )

    assert store.read("app")["error_code"] == "HANDOVER_FAILED"
    assert "HANDOVER_FAILED" in KNOWN_DEPLOYMENT_ERROR_CODES


def test_untyped_exception_uses_a_distinct_stable_fallback_code() -> None:
    assert stable_deployment_error_code(RuntimeError("plain failure")) == (
        "UNCLASSIFIED_DEPLOYMENT_ERROR"
    )


def test_transition_rejects_unregistered_typed_error_code(tmp_path: Path) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin("app", "old", "new", "release")
    error = RuntimeError("foreign typed failure")
    error.code = "FOREIGN_UNREGISTERED_CODE"  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="unregistered deployment error code"):
        store.transition("app", "failed", message=str(error), error=error)

    current = store.read("app")
    assert current is not None
    assert "error_code" not in current


def test_stable_deployment_error_rejects_the_fallback_as_free_form_input() -> None:
    with pytest.raises(ValueError, match="unregistered deployment error code"):
        StableDeploymentError("FOREIGN_UNREGISTERED_CODE", "failure")


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


@pytest.mark.parametrize(
    "active_state",
    [
        "build",
        "preflight",
        "backing_up",
        "migrating",
        "starting",
        "verifying",
        "recovering",
    ],
)
def test_nonterminal_attempt_is_closed_as_interrupted(
    tmp_path: Path, active_state: str
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin("soulstream", "old", "new", "release-042")
    if active_state != "build":
        store.transition("soulstream", active_state, message="preserved detail")
    before = store.read("soulstream")
    assert before is not None

    changed = store.mark_interrupted(
        "soulstream", reason="runner restarted before deployment completed"
    )
    after = store.read("soulstream")

    assert changed is True
    assert after is not None
    assert after["state"] == "interrupted"
    assert after["interrupted_from"] == active_state
    assert after["interruption_reason"] == (
        "runner restarted before deployment completed"
    )
    assert after["release_id"] == before["release_id"]
    assert after["previous_head"] == before["previous_head"]
    assert after["target_head"] == before["target_head"]
    assert after["history"][:-1] == before["history"]
    assert after["history"][-1]["state"] == "interrupted"

    history = list(after["history"])
    assert store.mark_interrupted("soulstream", reason="duplicate startup") is False
    assert store.read("soulstream")["history"] == history


def test_new_attempt_preserves_interrupted_attempt_with_distinct_identity(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin("soulstream", "old", "first-target", "release-042")
    first = store.read("soulstream")
    assert first is not None
    store.transition("soulstream", "migrating", message="migration began")
    store.mark_interrupted("soulstream", reason="process stopped")
    interrupted = store.read("soulstream")
    assert interrupted is not None

    store.begin("soulstream", "old", "second-target", "release-043")
    current = store.read("soulstream")

    assert current is not None
    assert current["state"] == "build"
    assert current["journal_attempt_id"] != first["journal_attempt_id"]
    assert current["target_head"] == "second-target"
    previous = current["previous_attempts"][-1]
    assert previous["journal_attempt_id"] == first["journal_attempt_id"]
    assert previous["state"] == "interrupted"
    assert previous["interrupted_from"] == "migrating"
    assert previous["interruption_reason"] == "process stopped"


def test_bound_intent_reuses_one_journal_identity_and_rejects_drift(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    journal_id = store.begin(
        "repo",
        "previous",
        "target",
        "approved-pull-pending",
        orchestrator_attempt_id="orch-1",
        node_id="node-a",
        branch="main",
        manifest_identity="release.json",
        manifest_digest="digest",
    )

    rebound = store.begin(
        "repo",
        "previous",
        "target",
        "release-1",
        orchestrator_attempt_id="orch-1",
        node_id="node-a",
        branch="main",
        manifest_identity="release.json",
        manifest_digest="digest",
        journal_attempt_id=journal_id,
    )

    assert rebound == journal_id
    current = store.read("repo")
    assert current["journal_attempt_id"] == journal_id
    assert current["orchestrator_attempt_id"] == "orch-1"
    assert "previous_attempts" not in current
    with pytest.raises(ValueError, match="immutable fields"):
        store.begin(
            "repo",
            "previous",
            "different-target",
            "release-1",
            orchestrator_attempt_id="orch-1",
            node_id="node-a",
            branch="main",
            manifest_identity="release.json",
            manifest_digest="digest",
            journal_attempt_id=journal_id,
        )


def test_bound_config_digest_cannot_be_omitted_when_reusing_journal(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    journal_id = store.begin(
        "repo",
        "previous",
        "target",
        "approved-pull-pending",
        journal_attempt_id=None,
        request_id="request-1",
        expected_operation="upgrade",
        config_digest="a" * 64,
    )

    with pytest.raises(ValueError, match="config_digest"):
        store.begin(
            "repo",
            "previous",
            "target",
            "release-1",
            journal_attempt_id=journal_id,
            request_id="request-1",
            expected_operation="upgrade",
        )


def test_new_begin_aborts_unfinished_live_intent_before_archiving(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin("soulstream", "old", "old", "approved-pull-pending")

    store.begin("soulstream", "old", "new", "release-042")
    current = store.read("soulstream")

    assert current is not None
    previous = current["previous_attempts"][-1]
    assert previous["state"] == "aborted"
    assert previous["aborted_from"] == "build"
    assert previous["history"][-1]["state"] == "aborted"


def test_new_begin_inherits_active_orchestrator_link_when_superseding(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin(
        "soulstream",
        "old",
        "target",
        "approved-pull-pending",
        orchestrator_attempt_id="orch-1",
        node_id="node-a",
        branch="main",
    )

    store.begin("soulstream", "old", "target", "release-042")

    current = store.read("soulstream")
    assert current is not None
    assert current["orchestrator_attempt_id"] == "orch-1"
    assert current["node_id"] == "node-a"
    assert current["branch"] == "main"
    assert current["previous_attempts"][-1]["state"] == "aborted"


def test_new_begin_does_not_inherit_terminal_orchestrator_link(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin(
        "soulstream",
        "old",
        "first-target",
        "release-041",
        orchestrator_attempt_id="orch-old",
        node_id="node-a",
        branch="main",
    )
    store.transition("soulstream", "success")

    store.begin("soulstream", "first-target", "next-target", "release-042")

    current = store.read("soulstream")
    assert current is not None
    assert current["orchestrator_attempt_id"] is None
    assert current["node_id"] is None
    assert current["branch"] is None


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


def test_roll_forward_manifest_requires_previous_release_fallback() -> None:
    with pytest.raises(ValidationError, match="previous-release fallback"):
        ReleaseManifest.model_validate(
            {
                "schema_version": "haniel.release.v1",
                "release_id": "unavailable-on-persistent-failure",
                "migration": {
                    "destructive": False,
                    "preflight": command("preflight"),
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


def test_failed_command_persists_bounded_stderr_and_stdout_in_journal(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    secret = "journal-secret-value"
    stderr = "s" * 9000 + f" TOKEN={secret} Error: DATABASE_URL is required"
    stdout = "o" * 5000 + "preflight context"
    deploy = DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path / "state"),
        command_runner=subprocess_command_runner(tmp_path),
    )

    with (
        patch(
            "haniel.core.deployment_command_runner.shutil.which",
            return_value="/tools/run-preflight",
        ),
        patch("haniel.core.deployment_command_runner._run_process_tree") as run,
    ):
        run.side_effect = subprocess.CalledProcessError(
            1,
            ["run-preflight"],
            output=stdout,
            stderr=stderr,
        )
        with pytest.raises(DeploymentError):
            deploy.execute(
                repo_name="soulstream",
                previous_head="old",
                target_head="new",
                manifest=manifest(),
                callbacks=callbacks(events),
            )

    journal = DeploymentStateStore(tmp_path / "state").read("soulstream")
    recovering = next(
        entry for entry in journal["history"] if entry["state"] == "recovering"
    )
    message = recovering["message"]

    assert "command 'preflight' failed with exit code 1" in message
    assert "stderr (last 8192 chars):" in message
    assert "Error: DATABASE_URL is required" in message
    assert "stdout (last 4096 chars):" in message
    assert "preflight context" in message
    assert secret not in message
    assert "TOKEN=[REDACTED]" in message
    assert len(message) <= 16384
    assert stderr not in message
    assert stdout not in message


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


def test_staging_failure_only_terminates_matching_live_request(tmp_path: Path) -> None:
    store = DeploymentStateStore(tmp_path / "state")
    store.begin_handover(
        "app",
        previous_head="old",
        target_ref="origin/main",
        manifest_identity="deploy/release.json",
        request_id="request-new",
        expected_operation="upgrade",
        branch="main",
    )

    assert (
        store.fail_handover_if_current("app", "request-old", "COMMAND_TIMEOUT", "stale")
        is False
    )
    current = store.read("app")
    assert current is not None
    assert current["state"] == "target_resolving"

    assert (
        store.fail_handover_if_current(
            "app", "request-new", "COMMAND_TIMEOUT", "child timed out"
        )
        is True
    )
    terminal = store.read("app")
    assert terminal is not None
    assert terminal["state"] == "failed"
    assert terminal["error_code"] == "COMMAND_TIMEOUT"
