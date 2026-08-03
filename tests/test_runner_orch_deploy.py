"""Tests for ServiceRunner._handle_deploy_approval and self-update result mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config.model import (
    HanielConfig,
)
from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
from haniel.core.orch_pending_deploy import (
    MARKER_RELPATH,
    write as write_pending,
)
from haniel.core.runner import ServiceRunner
from haniel.core.self_update_marker import SelfUpdateResult
from haniel.core.orchestrated_deploy_execution import execute_approved_plan


@pytest.fixture(autouse=True)
def approved_remote_target(monkeypatch):
    monkeypatch.setattr(
        "haniel.core.orchestrated_deploy_execution.get_remote_head",
        lambda _path, _branch: "abc1234",
    )


def _build_runner(tmp_path: Path, with_self_repo: bool = False) -> ServiceRunner:
    repos = {
        "appA": {
            "url": "git@github.com:test/appA.git",
            "path": "appA",
            "branch": "main",
        },
    }
    if with_self_repo:
        repos["haniel"] = {
            "url": "git@github.com:test/haniel.git",
            "path": "haniel",
            "branch": "main",
        }
    payload = {
        "poll_interval": 10,
        "services": {
            "svc-a": {"run": "echo a", "repo": "appA", "enabled": True},
        },
        "repos": repos,
        "orchestrator_client": {
            "url": "ws://localhost/ws/node",
            "token": "test",
            "node_id": "node-a",
        },
    }
    if with_self_repo:
        # `self_update` is exposed via alias `self` in HanielConfig model.
        # Direct kwargs (HanielConfig(self_update=...)) silently drop the value.
        payload["self"] = {"repo": "haniel", "auto_update": False}
    config = HanielConfig.model_validate(payload)
    return ServiceRunner(config=config, config_dir=tmp_path)


def _approval(
    repo: str = "appA",
    branch: str = "main",
    *,
    mode: str = "execute",
    probe_id: str | None = None,
) -> dict:
    return {
        "deploy_id": f"node-a:{repo}:{branch}:abc1234",
        "orchestrator_attempt_id": "orch-1",
        "connection_generation": "generation-1",
        "execution_mode": mode,
        "probe_id": probe_id,
        "preflight_fingerprint": "fingerprint-1" if probe_id else None,
        "approved_by": "dashboard",
    }


def _write_pending(tmp_path: Path, deploy_id: str, started_at: str) -> None:
    write_pending(
        tmp_path,
        deploy_id,
        started_at,
        orchestrator_attempt_id="orch-1",
        connection_generation="generation-1",
        execution_mode="execute",
        probe_id="probe-1",
        preflight_fingerprint="fingerprint-1",
    )


class TestHandleDeployApprovalNonSelf:
    def test_unknown_repo_raises(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        with pytest.raises(ValueError, match="Unknown repo"):
            runner._handle_deploy_approval(_approval("missing"))

    def test_calls_trigger_pull_when_pending(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        # trigger_pull would skip if pending_changes is None — make sure it isn't
        runner._repo_states["appA"].pending_changes = {
            "commits": ["abc1234 fix"],
            "stat": "+1 -0",
        }
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        result = runner._handle_deploy_approval(_approval())
        runner.trigger_pull.assert_called_once_with(
            "appA",
            auto=False,
            orchestrator_attempt_id="orch-1",
            node_id="node-a",
            branch="main",
            target_head="abc1234",
        )
        assert result is None

    def test_no_pending_changes_still_enters_execution_path(
        self,
        tmp_path: Path,
    ) -> None:
        """Memory state is not proof; settled HEAD remains the success authority."""
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].pending_changes = None
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        with patch(
            "haniel.core.orchestrated_deploy_execution.get_head",
            return_value="abc1234",
        ):
            result = runner._handle_deploy_approval(_approval())
        runner.trigger_pull.assert_not_called()
        assert result is None

    def test_already_pulling_raises(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._pull_locks["appA"].acquire()
        try:
            with pytest.raises(RuntimeError, match="already pulling"):
                runner._repo_states["appA"].pending_changes = {
                    "commits": ["abc1234 fix"],
                    "stat": "+1 -0",
                }
                runner._handle_deploy_approval(_approval())
        finally:
            runner._pull_locks["appA"].release()

    def test_branch_mismatch_fails_before_execution(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].pending_changes = {
            "commits": ["abc1234 fix"],
            "stat": "+1 -0",
        }
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        with pytest.raises(Exception, match="differs from configured branch"):
            runner._handle_deploy_approval(_approval(branch="feature/x"))
        runner.trigger_pull.assert_not_called()

    def test_remote_target_change_fails_before_execution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].pending_changes = {"commits": ["abc1234 fix"]}
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        monkeypatch.setattr(
            "haniel.core.orchestrated_deploy_execution.get_remote_head",
            lambda _path, _branch: "newer",
        )

        with pytest.raises(Exception, match="approved target changed"):
            runner._handle_deploy_approval(_approval())

        runner.trigger_pull.assert_not_called()

    def test_missing_preflight_probe_fails_closed(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]

        with pytest.raises(Exception, match="missing preflight probe"):
            runner._handle_deploy_approval(_approval(probe_id="missing"))

        runner.trigger_pull.assert_not_called()

    def test_duplicate_approval_attempt_is_not_executed_twice(
        self, tmp_path: Path
    ) -> None:
        runner = _build_runner(tmp_path)
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        with patch(
            "haniel.core.orchestrated_deploy_execution.get_head",
            return_value="abc1234",
        ):
            runner._handle_deploy_approval(_approval())
            with pytest.raises(Exception, match="already consumed"):
                runner._handle_deploy_approval(_approval())

        runner.trigger_pull.assert_not_called()


class TestAutoDeployReconciliation:
    def test_no_service_repo_still_uses_common_attempt_path(self, tmp_path: Path):
        runner = _build_runner(tmp_path)
        runner.config.services.clear()
        runner._run_auto_deploy = MagicMock()  # type: ignore[method-assign]

        runner._apply_changes(["appA"])

        runner._run_auto_deploy.assert_called_once_with("appA")

    def test_attempt_started_precedes_execution_and_settled_report(
        self, tmp_path: Path
    ):
        runner = _build_runner(tmp_path)
        before = RepoReconciliationSnapshot(
            "node-a", "appA", "main", "local", "remote", "node-a:appA:main:remote"
        )
        settled = RepoReconciliationSnapshot(
            "node-a", "appA", "main", "remote", "remote", before.deploy_id
        )
        runner._capture_orchestrator_repo_snapshot = MagicMock(  # type: ignore[method-assign]
            side_effect=[before, settled]
        )
        order: list[str] = []
        runner._orch_client = MagicMock()
        permit = {
            "accepted": True,
            "requested_orchestrator_attempt_id": "orch-auto",
            "begun_orchestrator_attempt_id": "orch-auto",
            "deploy_id": before.deploy_id,
            "connection_generation": "generation-1",
            "probe_id": "probe-1",
            "execution_mode": "execute",
            "preflight_fingerprint": "fingerprint-1",
        }
        runner._orch_client.request_auto_attempt.side_effect = lambda snapshot: (
            order.append("attempt_started") or permit
        )
        progress = MagicMock()
        progress.stop.side_effect = lambda: order.append("progress_stopped")
        runner._orch_client.start_deploy_progress.side_effect = lambda _permit: (
            order.append("progress_started") or progress
        )
        runner._orchestrated_deploys.record_probe({"probe_id": "probe-1"})
        runner._orch_client.report_deploy_attempt.side_effect = lambda *args, **kwargs: (
            order.append("settled_report")
        )
        with patch(
            "haniel.core.runner.execute_approved_plan",
            side_effect=lambda *args, **kwargs: (
                order.append("execute")
                if kwargs["progress_callback"] is progress.transition
                else (_ for _ in ()).throw(AssertionError("wrong progress callback"))
            ),
        ):
            runner._run_auto_deploy("appA")

        runner._orch_client.request_auto_attempt.assert_called_once_with(before)
        runner._orch_client.report_deploy_attempt.assert_called_once()
        args = runner._orch_client.report_deploy_attempt.call_args.args
        assert args[0] == settled
        assert args[1] is None
        runner._orch_client.start_deploy_progress.assert_called_once_with(permit)
        assert order == [
            "attempt_started",
            "progress_started",
            "execute",
            "progress_stopped",
            "settled_report",
        ]


class TestImmutableRetryExecution:
    @staticmethod
    def _probe() -> dict:
        return {
            "probe_id": "probe-1",
            "connection_generation": "generation-1",
            "deploy_id": "node-a:appA:main:abc1234",
            "node_id": "node-a",
            "repo": "appA",
            "branch": "main",
            "target_head": "abc1234",
            "source_orchestrator_attempt_id": "source-1",
        }

    def test_generation_mismatch_fails_before_retry_execution(
        self, tmp_path: Path
    ) -> None:
        runner = _build_runner(tmp_path)
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        approval = _approval(probe_id="probe-1")
        approval["connection_generation"] = "different"

        with pytest.raises(Exception, match="connection_generation"):
            execute_approved_plan(runner, approval, self._probe(), MagicMock())

        runner.trigger_pull.assert_not_called()

    def test_equal_head_legacy_restarts_without_pull_or_manifest(
        self, tmp_path: Path
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._restart_after_pull_legacy = MagicMock()  # type: ignore[assignment]
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        planner = MagicMock()
        planner.revalidate.return_value = MagicMock(
            mode="execute", evidence={}, reason="legacy_retry"
        )
        with (
            patch(
                "haniel.core.orchestrated_deploy_execution.get_head",
                return_value="abc1234",
            ),
            patch(
                "haniel.core.orchestrated_deploy_execution.run_manifest_deployment"
            ) as manifest,
        ):
            execute_approved_plan(
                runner, _approval(probe_id="probe-1"), self._probe(), planner
            )

        runner._restart_after_pull_legacy.assert_called_once_with("appA", ["svc-a"])
        runner.trigger_pull.assert_not_called()
        manifest.assert_not_called()

    def test_equal_head_manifest_retry_uses_original_previous_head(
        self, tmp_path: Path
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].config.release_manifest = "release.json"
        planner = MagicMock()
        planner.revalidate.return_value = MagicMock(
            mode="execute",
            evidence={"original_previous_head": "original-previous"},
            reason="manifest_retry",
        )
        with (
            patch(
                "haniel.core.orchestrated_deploy_execution.get_head",
                return_value="abc1234",
            ),
            patch(
                "haniel.core.orchestrated_deploy_execution.run_manifest_deployment"
            ) as manifest,
        ):
            execute_approved_plan(
                runner, _approval(probe_id="probe-1"), self._probe(), planner
            )

        manifest.assert_called_once_with(
            runner,
            "appA",
            ["svc-a"],
            "original-previous",
            orchestrator_attempt_id="orch-1",
            node_id="node-a",
            branch="main",
        )

    def test_recovery_mode_returns_evidence_without_deploy_side_effects(
        self, tmp_path: Path
    ) -> None:
        runner = _build_runner(tmp_path)
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        runner._restart_after_pull_legacy = MagicMock()  # type: ignore[assignment]
        planner = MagicMock()
        planner.revalidate.return_value = MagicMock(
            mode="evidence_recovery",
            reason="durable_local_success",
            evidence={
                "journal_attempt_id": "journal-1",
                "current_head": "abc1234",
                "manifest_identity": "release.json",
                "manifest_digest": "digest",
                "journal_completed_at": "2026-08-01T00:00:00Z",
                "original_previous_head": "previous",
            },
        )

        result = execute_approved_plan(
            runner,
            _approval(mode="evidence_recovery", probe_id="probe-1"),
            self._probe(),
            planner,
        )

        assert result["type"] == "manifest_recovery_evidence"
        runner.trigger_pull.assert_not_called()
        runner._restart_after_pull_legacy.assert_not_called()


class TestHandleDeployApprovalSelfRepo:
    def test_writes_pending_and_returns_deferred(
        self,
        tmp_path: Path,
    ) -> None:
        runner = _build_runner(tmp_path, with_self_repo=True)
        runner.approve_self_update = MagicMock(return_value="ok")  # type: ignore[assignment]
        runner._deferred_stop_for_self_update = MagicMock()  # type: ignore[assignment]
        result = runner._handle_deploy_approval(_approval("haniel"))
        assert result == "deferred"
        # Pending file written so next runner can correlate self-update result
        assert (tmp_path / MARKER_RELPATH).exists()
        # approve_self_update gate (state.self_update_pending=True) bypassed
        runner.approve_self_update.assert_called_once()

    def test_recovery_approval_never_starts_self_update(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path, with_self_repo=True)
        runner.approve_self_update = MagicMock()  # type: ignore[assignment]
        runner._orchestrated_deploys.record_probe({"probe_id": "probe-1"})
        recovered = {"type": "manifest_recovery_evidence", "deploy_id": "d"}
        plan = MagicMock(mode="evidence_recovery")
        with (
            patch.object(runner, "_deploy_retry_planner", return_value=MagicMock()),
            patch("haniel.core.runner.validate_approved_plan", return_value=plan),
            patch("haniel.core.runner.build_recovery_evidence", return_value=recovered),
        ):
            result = runner._handle_deploy_approval(
                _approval("haniel", mode="evidence_recovery", probe_id="probe-1")
            )

        assert result == recovered
        runner.approve_self_update.assert_not_called()
        assert not (tmp_path / MARKER_RELPATH).exists()


class TestEnqueuePendingSelfDeployResult:
    def test_no_marker_skips(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        runner._enqueue_pending_self_deploy_result()
        runner._orch_client.enqueue_deploy_result.assert_not_called()

    def test_no_orch_client_skips(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = None
        _write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        # Must not raise
        runner._enqueue_pending_self_deploy_result()
        # Marker still consumed
        assert not (tmp_path / MARKER_RELPATH).exists()

    def test_marker_with_success_result(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        _write_pending(
            tmp_path,
            "d1",
            datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        )
        runner._last_self_update_result = SelfUpdateResult(
            version=1,
            started_at=datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
            finished_at=datetime(2026, 5, 5, 0, 1, 30, tzinfo=timezone.utc).isoformat(),
            ok=True,
            steps=[],
        )
        runner._enqueue_pending_self_deploy_result()
        runner._orch_client.enqueue_deploy_result.assert_called_once()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        args = runner._orch_client.enqueue_deploy_result.call_args.args
        assert ("d1" in args) or kwargs.get("deploy_id") == "d1"
        assert kwargs.get("status") == "success"
        assert kwargs.get("error") is None
        assert kwargs.get("duration_ms") == 90 * 1000

    def test_marker_with_failed_result(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        _write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        runner._last_self_update_result = SelfUpdateResult(
            version=1,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=False,
            steps=[],
            error="git pull failed",
        )
        runner._enqueue_pending_self_deploy_result()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        assert kwargs.get("status") == "failed"
        assert kwargs.get("error") == "git pull failed"

    def test_marker_with_failed_no_error_uses_default_message(
        self,
        tmp_path: Path,
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        _write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        runner._last_self_update_result = SelfUpdateResult(
            version=1,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            ok=False,
            steps=[],
            error=None,
        )
        runner._enqueue_pending_self_deploy_result()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        assert kwargs.get("status") == "failed"
        assert "self-update reported failure" in kwargs.get("error", "")

    def test_marker_without_self_update_result_sends_failed(
        self,
        tmp_path: Path,
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        _write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        runner._last_self_update_result = None
        runner._enqueue_pending_self_deploy_result()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        assert kwargs.get("status") == "failed"
        assert "missing" in kwargs.get("error", "")

    def test_invalid_started_at_skips_duration(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        _write_pending(tmp_path, "d1", "not-a-timestamp")
        runner._last_self_update_result = SelfUpdateResult(
            version=1,
            started_at="t1",
            finished_at="t2",
            ok=True,
            steps=[],
        )
        runner._enqueue_pending_self_deploy_result()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        assert kwargs.get("status") == "success"
        assert kwargs.get("duration_ms") is None
