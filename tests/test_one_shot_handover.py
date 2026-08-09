"""Operation-aware one-shot deployment and command-result contracts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from haniel.core.deployment import (
    CommandSpec,
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
    ReleaseManifest,
)
from haniel.core.deployment_command_runner import CommandResult
from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.one_shot_handover import execute_owner_handover
from haniel.core.release_staging import initial_clone_path


def manifest() -> ReleaseManifest:
    def command(name: str) -> dict[str, object]:
        return {"name": name, "command": name}

    return ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "release-1",
            "migration": {
                "destructive": True,
                "operation": "discover",
                "result_contract": "soulstream.database-release.v1",
                "preflight": command("preflight"),
                "backup": command("backup"),
                "verify_backup": command("verify-backup"),
                "apply": command("apply"),
            },
            "post_start_verify": [command("health")],
            "recovery": {"strategy": "rollback", "command": command("restore")},
        }
    )


def receipt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "request_id": "request-1",
        "repo": "app",
        "target_head": "target",
        "owner_instance": "owner-1",
        "quiescence_nonce": "nonce-1",
        "stopped_services": ["app"],
        "already_stopped_services": [],
        "quiesced_services": ["app"],
    }
    value.update(overrides)
    return value


def receipt_without(field: str) -> dict[str, object]:
    value = receipt()
    value.pop(field)
    return value


def callback_set(events: list[str], *, receipt: dict | None) -> DeploymentCallbacks:
    return DeploymentCallbacks(
        build=lambda: events.append("build"),
        stop=lambda: events.append("stop") or receipt,
        start_and_wait=lambda: events.append("start"),
        rollback=lambda: events.append("repo-rollback"),
        prepare_roll_forward=lambda: events.append("prepare-roll-forward"),
        stop_partial=lambda: events.append("stop-partial"),
        writer_services=("app",),
        owner_instance="owner-1",
        quiescence_nonce="nonce-1",
    )


def coordinator(
    tmp_path: Path,
    events: list[str],
    operation: str,
    *,
    fail: str | None = None,
) -> DeploymentCoordinator:
    def run(command: CommandSpec, _env: dict[str, str]) -> CommandResult:
        events.append(command.name)
        if command.name == fail:
            raise RuntimeError(f"{command.name} failed")
        if command.name == "preflight":
            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": operation,
                    "journal_path": str(tmp_path / "db-release.json"),
                },
            )
        return CommandResult(stdout="", json_data=None)

    return DeploymentCoordinator(
        state_store=DeploymentStateStore(tmp_path / "deployments"),
        command_runner=run,
    )


def test_fresh_install_skips_quiescence_backup_and_restore_on_apply_failure(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "fresh_install", fail="apply")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="app",
            previous_head="absent",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(events, receipt=None),
            expected_operation="fresh_install",
            request_id="request-1",
        )

    assert exc_info.value.recovered is False
    assert "stop" not in events
    assert "backup" not in events
    assert "verify-backup" not in events
    assert "restore" not in events
    assert "repo-rollback" not in events


def test_fresh_post_start_failure_stops_partial_target_without_restore(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "fresh_install", fail="health")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="app",
            previous_head="absent",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(events, receipt=None),
            expected_operation="fresh_install",
            request_id="request-1",
        )

    assert exc_info.value.recovered is False
    assert "start" in events
    assert events.count("stop-partial") == 1
    assert "restore" not in events
    assert "repo-rollback" not in events


def test_upgrade_requires_quiescence_before_backup_and_apply(tmp_path: Path) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "upgrade")

    with pytest.raises(DeploymentError, match="QUIESCENCE_REQUIRED"):
        deploy.execute(
            repo_name="app",
            previous_head="old",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(events, receipt=None),
            expected_operation="upgrade",
            request_id="request-1",
        )

    assert events == ["build", "preflight", "stop", "repo-rollback"]
    assert "backup" not in events
    assert "apply" not in events


@pytest.mark.parametrize(
    "bad_receipt",
    [
        receipt_without("request_id"),
        receipt_without("repo"),
        receipt_without("target_head"),
        receipt_without("owner_instance"),
        receipt_without("quiescence_nonce"),
        receipt_without("stopped_services"),
        receipt_without("already_stopped_services"),
        receipt_without("quiesced_services"),
        receipt(request_id="different"),
        receipt(repo="other"),
        receipt(target_head="other"),
        receipt(owner_instance=""),
        receipt(quiescence_nonce="stale-nonce"),
        receipt(stopped_services=["other"]),
    ],
)
def test_upgrade_rejects_inconsistent_quiescence_before_backup(
    tmp_path: Path,
    bad_receipt: dict[str, object],
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "upgrade")

    with pytest.raises(DeploymentError, match="QUIESCENCE_REQUIRED"):
        deploy.execute(
            repo_name="app",
            previous_head="old",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(events, receipt=bad_receipt),
            expected_operation="upgrade",
            request_id="request-1",
        )

    assert "backup" not in events
    assert "apply" not in events


def test_upgrade_failure_restores_database_then_repo_symmetrically(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "upgrade", fail="apply")

    with pytest.raises(DeploymentError) as exc_info:
        deploy.execute(
            repo_name="app",
            previous_head="old",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(
                events,
                receipt=receipt(),
            ),
            expected_operation="upgrade",
            request_id="request-1",
        )

    assert exc_info.value.recovered is True
    assert events == [
        "build",
        "preflight",
        "stop",
        "backup",
        "verify-backup",
        "apply",
        "restore",
        "repo-rollback",
    ]


@pytest.mark.parametrize(
    ("failed_phase", "error_code"),
    [
        ("backup", "BACKUP_CREATE_FAILED"),
        ("verify-backup", "BACKUP_VERIFY_FAILED"),
        ("apply", "APPLY_FAILED"),
        ("health", "POST_VERIFY_FAILED"),
    ],
)
def test_release_phase_failures_keep_stable_error_codes(
    tmp_path: Path,
    failed_phase: str,
    error_code: str,
) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "upgrade", fail=failed_phase)

    with pytest.raises(DeploymentError, match=error_code):
        deploy.execute(
            repo_name="app",
            previous_head="old",
            target_head="target",
            manifest=manifest(),
            callbacks=callback_set(
                events,
                receipt=receipt(),
            ),
            expected_operation="upgrade",
            request_id="request-1",
        )


def test_preflight_result_links_database_journal_and_operation(tmp_path: Path) -> None:
    events: list[str] = []
    deploy = coordinator(tmp_path, events, "upgrade")

    result = deploy.execute(
        repo_name="app",
        previous_head="old",
        target_head="target",
        manifest=manifest(),
        callbacks=callback_set(
            events,
            receipt=receipt(),
        ),
        expected_operation="upgrade",
        request_id="request-1",
    )

    journal = DeploymentStateStore(tmp_path / "deployments").read("app")
    assert result.status == "success"
    assert journal["operation"] == "upgrade"
    assert journal["database_journal_path"] == str(tmp_path / "db-release.json")
    assert journal["quiescence_receipt"]["owner_instance"] == "owner-1"
    assert journal["database_last_result"]["operation"] == "upgrade"


def test_database_result_and_history_are_redacted_before_journal_write(
    tmp_path: Path,
) -> None:
    store = DeploymentStateStore(tmp_path / "deployments")
    store.begin(
        "app",
        "old",
        "target",
        "release-1",
        request_id="request-1",
        expected_operation="upgrade",
    )

    store.record_database_result(
        "app",
        phase="preflight",
        result={
            "ok": False,
            "DATABASE_URL": "postgresql://user:password@db.example/app",
            "nested": {"auth_token": "top-secret"},
        },
    )
    store.transition(
        "app",
        "failed",
        message="PASSWORD=plain-password CREDENTIAL=plain-credential",
    )

    serialized = repr(store.read("app"))
    for secret in (
        "user",
        "password",
        "top-secret",
        "plain-password",
        "plain-credential",
    ):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_retry_after_database_commit_before_haniel_journal_update_reuses_identity(
    tmp_path: Path,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    class FaultOnceStore(DeploymentStateStore):
        crashed = False

        def record_database_result(self, repo_name, *, phase, result):
            if phase == "apply" and not self.crashed:
                self.crashed = True
                raise SimulatedProcessCrash()
            return super().record_database_result(repo_name, phase=phase, result=result)

    events: list[str] = []
    sql_commits = 0
    store = FaultOnceStore(tmp_path / "deployments")

    def run(command: CommandSpec, _env: dict[str, str]) -> CommandResult:
        nonlocal sql_commits
        events.append(command.name)
        if command.name == "preflight":
            return CommandResult(
                stdout="{}",
                json_data={
                    "schema_version": "soulstream.database-release.v1",
                    "ok": True,
                    "operation": "upgrade",
                    "journal_path": str(tmp_path / "database-release.json"),
                },
            )
        if command.name == "apply":
            if sql_commits == 0:
                sql_commits += 1
                phase = "applied"
            else:
                phase = "applied_reconciled"
            return CommandResult(stdout="{}", json_data={"ok": True, "phase": phase})
        return CommandResult(stdout="", json_data=None)

    deploy = DeploymentCoordinator(state_store=store, command_runner=run)
    kwargs = {
        "repo_name": "app",
        "previous_head": "old",
        "target_head": "target",
        "manifest": manifest(),
        "callbacks": callback_set(
            events,
            receipt=receipt(),
        ),
        "expected_operation": "upgrade",
        "request_id": "request-1",
    }

    with pytest.raises(SimulatedProcessCrash):
        deploy.execute(**kwargs)
    attempt_id = store.read("app")["journal_attempt_id"]

    deploy.execute(**kwargs, journal_attempt_id=attempt_id)

    journal = store.read("app")
    assert journal["journal_attempt_id"] == attempt_id
    assert journal["database_last_result"]["phase"] == "applied_reconciled"
    assert sql_commits == 1


def test_fresh_owner_promotes_initial_clone_only_after_probe(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    repo_path = tmp_path / "repo"
    deferred_clone = initial_clone_path(repo_path)
    deferred_clone.mkdir()
    (deferred_clone / "marker").write_text("initial", encoding="utf-8")
    repo_state = SimpleNamespace(
        config=SimpleNamespace(
            path="repo",
            branch="main",
            release_manifest="deploy/release.json",
        ),
        last_head=None,
    )
    runner = SimpleNamespace(
        config_dir=tmp_path,
        _repo_states={"app": repo_state},
        lifecycle_instance_id="owner-1",
        get_affected_services=lambda _repo: ["app"],
    )
    staged = SimpleNamespace(
        target_head="target-head",
        manifest_digest="manifest-digest",
        manifest=SimpleNamespace(release_id="release-1"),
    )
    control = LifecycleControl(config_path)
    control.submit_request(
        "request-1",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "fresh_install",
        },
    )
    events: list[str] = []

    def probe(*_args, **_kwargs):
        assert not repo_path.exists()
        assert deferred_clone.exists()
        events.append("probe")
        return staged

    def activate(path: Path, target: str, *, strategy: str) -> list[str]:
        assert path == repo_path
        assert path.exists()
        assert not deferred_clone.exists()
        assert strategy == "merge"
        events.append(f"activate:{target}")
        return []

    with (
        patch(
            "haniel.core.one_shot_handover.probe_manifest_target",
            side_effect=probe,
        ),
        patch(
            "haniel.core.one_shot_handover.activate_repo_target",
            side_effect=activate,
        ),
        patch(
            "haniel.core.one_shot_handover.get_head",
            side_effect=lambda path: (
                "target-head" if path == repo_path and path.exists() else "old-head"
            ),
        ),
        patch("haniel.core.one_shot_handover.run_manifest_deployment") as deploy,
    ):
        result = execute_owner_handover(
            runner,
            control=control,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="fresh_install",
            request_id="request-1",
        )

    assert result.ok is True
    assert events == ["probe", "activate:target-head"]
    assert (repo_path / "marker").read_text(encoding="utf-8") == "initial"
    deploy.assert_called_once()


def test_one_shot_terminal_prioritizes_recovery_failure_code(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_state = SimpleNamespace(
        config=SimpleNamespace(
            path="repo",
            branch="main",
            release_manifest="deploy/release.json",
            pull_strategy="merge",
        ),
        last_head="old-head",
    )
    runner = SimpleNamespace(
        config_dir=tmp_path,
        _repo_states={"app": repo_state},
        lifecycle_instance_id="owner-1",
        get_affected_services=lambda _repo: ["app"],
    )
    staged = SimpleNamespace(
        target_head="target-head",
        manifest_digest="manifest-digest",
        manifest=SimpleNamespace(release_id="release-1"),
    )
    control = LifecycleControl(config_path)
    control.submit_request(
        "request-1",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
        },
    )
    deployment_error = DeploymentError(
        "deployment failed: POST_VERIFY_FAILED; recovery failed",
        recovered=False,
        recovery_error=RuntimeError("repo reset failed"),
    )

    with (
        patch(
            "haniel.core.one_shot_handover.probe_manifest_target",
            return_value=staged,
        ),
        patch("haniel.core.one_shot_handover.activate_repo_target", return_value=[]),
        patch(
            "haniel.core.one_shot_handover.get_head",
            side_effect=["old-head", "target-head"],
        ),
        patch(
            "haniel.core.one_shot_handover.run_manifest_deployment",
            side_effect=deployment_error,
        ),
    ):
        result = execute_owner_handover(
            runner,
            control=control,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
        )

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "RECOVERY_FAILED"
    terminal = control.read_result("request-1")["terminal"]
    assert terminal["error"]["code"] == "RECOVERY_FAILED"
