"""Tests for ServiceRunner._handle_deploy_approval and self-update result mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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


class TestHandleDeployApprovalNonSelf:
    def test_unknown_repo_raises(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        with pytest.raises(ValueError, match="Unknown repo"):
            runner._handle_deploy_approval(
                "id",
                "missing",
                "main",
            )

    def test_calls_trigger_pull_when_pending(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        # trigger_pull would skip if pending_changes is None — make sure it isn't
        runner._repo_states["appA"].pending_changes = {
            "commits": ["abc1234 fix"],
            "stat": "+1 -0",
        }
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        result = runner._handle_deploy_approval("id", "appA", "main")
        runner.trigger_pull.assert_called_once_with("appA", auto=False)
        assert result is None

    def test_no_pending_changes_returns_success_noop(
        self,
        tmp_path: Path,
    ) -> None:
        """No pending_changes → trigger_pull NOT called, return None (success no-op)."""
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].pending_changes = None
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        result = runner._handle_deploy_approval("id", "appA", "main")
        runner.trigger_pull.assert_not_called()
        assert result is None  # caller will report success

    def test_already_pulling_raises(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._pull_locks["appA"].acquire()
        try:
            with pytest.raises(RuntimeError, match="already pulling"):
                runner._handle_deploy_approval("id", "appA", "main")
        finally:
            runner._pull_locks["appA"].release()

    def test_branch_mismatch_warns_and_proceeds(
        self,
        tmp_path: Path,
        caplog,
    ) -> None:
        runner = _build_runner(tmp_path)
        runner._repo_states["appA"].pending_changes = {
            "commits": ["abc1234 fix"],
            "stat": "+1 -0",
        }
        runner.trigger_pull = MagicMock()  # type: ignore[assignment]
        with caplog.at_level("WARNING"):
            runner._handle_deploy_approval("id", "appA", "feature/x")
        assert "differs from configured" in caplog.text
        runner.trigger_pull.assert_called_once()


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
        runner._orch_client.notify_repo_reconciliation.side_effect = (
            lambda *args, **kwargs: order.append("attempt_started")
        )
        runner._orch_client.report_deploy_attempt.side_effect = (
            lambda *args, **kwargs: order.append("settled_report")
        )
        runner.trigger_pull = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *args, **kwargs: order.append("execute")
        )

        runner._run_auto_deploy("appA")

        runner._orch_client.notify_repo_reconciliation.assert_called_once_with(
            before, "attempt_started", wait=True
        )
        runner.trigger_pull.assert_called_once_with("appA", auto=True)
        runner._orch_client.report_deploy_attempt.assert_called_once()
        args = runner._orch_client.report_deploy_attempt.call_args.args
        assert args[0] == settled
        assert args[1] is None
        assert order == ["attempt_started", "execute", "settled_report"]


class TestHandleDeployApprovalSelfRepo:
    def test_writes_pending_and_returns_deferred(
        self,
        tmp_path: Path,
    ) -> None:
        runner = _build_runner(tmp_path, with_self_repo=True)
        runner.approve_self_update = MagicMock(return_value="ok")  # type: ignore[assignment]
        runner._deferred_stop_for_self_update = MagicMock()  # type: ignore[assignment]
        result = runner._handle_deploy_approval(
            "node:haniel:main:abc1234",
            "haniel",
            "main",
        )
        assert result == "deferred"
        # Pending file written so next runner can correlate self-update result
        assert (tmp_path / MARKER_RELPATH).exists()
        # approve_self_update gate (state.self_update_pending=True) bypassed
        runner.approve_self_update.assert_called_once()


class TestEnqueuePendingSelfDeployResult:
    def test_no_marker_skips(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        runner._enqueue_pending_self_deploy_result()
        runner._orch_client.enqueue_deploy_result.assert_not_called()

    def test_no_orch_client_skips(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = None
        write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        # Must not raise
        runner._enqueue_pending_self_deploy_result()
        # Marker still consumed
        assert not (tmp_path / MARKER_RELPATH).exists()

    def test_marker_with_success_result(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        write_pending(
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
        write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
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
        write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
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
        write_pending(tmp_path, "d1", datetime.now(timezone.utc).isoformat())
        runner._last_self_update_result = None
        runner._enqueue_pending_self_deploy_result()
        kwargs = runner._orch_client.enqueue_deploy_result.call_args.kwargs
        assert kwargs.get("status") == "failed"
        assert "missing" in kwargs.get("error", "")

    def test_invalid_started_at_skips_duration(self, tmp_path: Path) -> None:
        runner = _build_runner(tmp_path)
        runner._orch_client = MagicMock()
        write_pending(tmp_path, "d1", "not-a-timestamp")
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
