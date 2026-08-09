"""Execute one immutable retry plan selected by the server preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..integrations.deploy_reporting import ApprovalRevalidationError
from .git import get_head, get_pending_changes, get_remote_head
from .runner_deployment import ProgressCallback, run_manifest_deployment

if TYPE_CHECKING:
    from .deploy_retry_planner import DeployRetryPlanner, RetryPlan
    from .runner import ServiceRunner


class OrchestratedDeployRegistry:
    """Own generation-bound probe snapshots and one-shot approval consumption."""

    def __init__(self) -> None:
        self._probes: dict[str, dict[str, Any]] = {}
        self._consumed_attempts: set[str] = set()

    def record_probe(self, probe: dict[str, Any]) -> None:
        self._probes[probe["probe_id"]] = dict(probe)

    def consume_approval(self, approval: dict[str, Any]) -> dict[str, Any] | None:
        orchestrator_attempt_id = approval["orchestrator_attempt_id"]
        if orchestrator_attempt_id in self._consumed_attempts:
            raise ApprovalRevalidationError(
                f"orchestrator attempt {orchestrator_attempt_id} was already consumed"
            )
        self._consumed_attempts.add(orchestrator_attempt_id)
        return self._probes.pop(approval.get("probe_id", ""), None)


def execute_approved_plan(
    runner: "ServiceRunner",
    approval: dict[str, Any],
    probe: dict[str, Any] | None,
    planner: "DeployRetryPlanner",
    *,
    progress_callback: ProgressCallback | None = None,
) -> str | dict[str, Any] | None:
    """Revalidate then enter exactly the mode fixed by the server."""
    deploy_id = approval["deploy_id"]
    _node_id, repo, branch, _target = deploy_id.split(":", 3)
    assert_remote_target(runner, repo, _target)
    if probe is None:
        if approval.get("probe_id") is not None:
            raise ApprovalRevalidationError(
                "approval references a missing preflight probe"
            )
        if approval.get("execution_mode") != "execute":
            raise ApprovalRevalidationError(
                "approval without a probe must use execute mode"
            )
        _execute_normal(
            runner,
            approval,
            repo,
            branch,
            _target,
            progress_callback=progress_callback,
        )
        return None
    plan = validate_approved_plan(planner, probe, approval)
    if plan.mode == "evidence_recovery":
        return build_recovery_evidence(approval, probe, plan)
    _execute_retry(
        runner,
        approval,
        probe,
        plan,
        repo,
        branch,
        progress_callback=progress_callback,
    )
    return None


def assert_remote_target(runner: "ServiceRunner", repo: str, target_head: str) -> None:
    """Fail before side effects if the approved branch no longer names target."""
    state = runner._repo_states[repo]
    repo_path = runner.config_dir / state.config.path
    remote_head = get_remote_head(repo_path, state.config.branch)
    if remote_head != target_head:
        raise ApprovalRevalidationError(
            f"approved target changed before execution: "
            f"approved={target_head} remote={remote_head}"
        )


def _execute_normal(
    runner: "ServiceRunner",
    approval: dict[str, Any],
    repo: str,
    branch: str,
    target: str,
    *,
    progress_callback: ProgressCallback | None,
) -> None:
    state = runner._repo_states[repo]
    if not state.pending_changes:
        repo_path = runner.config_dir / state.config.path
        current_head = get_head(repo_path)
        if current_head == target and repo not in runner._startup_repo_locks:
            return
        if current_head != target:
            state.pending_changes = get_pending_changes(repo_path, state.config.branch)
            if not state.pending_changes.get("commits"):
                raise RuntimeError(
                    f"approval target {target} is not present and no pending change is available"
                )
    progress_kwargs = (
        {"progress_callback": progress_callback}
        if progress_callback is not None
        else {}
    )
    runner.trigger_pull(
        repo,
        auto=False,
        orchestrator_attempt_id=approval["orchestrator_attempt_id"],
        node_id=approval["deploy_id"].split(":", 1)[0],
        branch=branch,
        target_head=target,
        **progress_kwargs,
    )


def validate_approved_plan(
    planner: "DeployRetryPlanner", probe: dict[str, Any], approval: dict[str, Any]
) -> "RetryPlan":
    checks = {
        "probe_id": approval.get("probe_id") == probe.get("probe_id"),
        "connection_generation": approval.get("connection_generation")
        == probe.get("connection_generation"),
        "deploy_id": approval.get("deploy_id") == probe.get("deploy_id"),
    }
    mismatches = [name for name, matches in checks.items() if not matches]
    if mismatches:
        raise ApprovalRevalidationError(
            "approval preflight snapshot mismatch: " + ", ".join(mismatches)
        )
    plan = planner.revalidate(probe, approval)
    if plan.mode == "fail_closed":
        raise ApprovalRevalidationError(plan.error or plan.reason)
    return plan


def _execute_retry(
    runner: "ServiceRunner",
    approval: dict[str, Any],
    probe: dict[str, Any],
    plan: "RetryPlan",
    repo: str,
    branch: str,
    *,
    progress_callback: ProgressCallback | None,
) -> None:
    state = runner._repo_states[repo]
    repo_path = runner.config_dir / state.config.path
    current_head = get_head(repo_path)
    target = probe["target_head"]
    if current_head != target:
        if not state.pending_changes:
            from .git import get_pending_changes

            state.pending_changes = get_pending_changes(repo_path, branch)
        progress_kwargs = (
            {"progress_callback": progress_callback}
            if progress_callback is not None
            else {}
        )
        runner.trigger_pull(
            repo,
            auto=False,
            orchestrator_attempt_id=approval["orchestrator_attempt_id"],
            node_id=probe["node_id"],
            branch=branch,
            target_head=target,
            **progress_kwargs,
        )
        return

    affected = runner.get_affected_services(repo)
    lock = runner._pull_locks[repo]
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"already deploying {repo}")
    runner._suppress_pending_restarts(affected)
    try:
        if state.config.release_manifest is None:
            runner._restart_after_pull_legacy(repo, affected)
            return
        previous_head = plan.evidence.get("original_previous_head")
        if not previous_head:
            raise ApprovalRevalidationError(
                "manifest retry has no durable original previous_head"
            )
        progress_kwargs = (
            {"progress_callback": progress_callback}
            if progress_callback is not None
            else {}
        )
        run_manifest_deployment(
            runner,
            repo,
            affected,
            previous_head,
            orchestrator_attempt_id=approval["orchestrator_attempt_id"],
            node_id=probe["node_id"],
            branch=branch,
            expected_operation="upgrade",
            request_id=approval["orchestrator_attempt_id"],
            **progress_kwargs,
        )
    finally:
        runner._release_restart_suppression(affected)
        lock.release()


def build_recovery_evidence(
    approval: dict[str, Any], probe: dict[str, Any], plan: "RetryPlan"
) -> dict[str, Any]:
    evidence = plan.evidence
    return {
        "type": "manifest_recovery_evidence",
        "deploy_id": approval["deploy_id"],
        "orchestrator_attempt_id": approval["orchestrator_attempt_id"],
        "source_orchestrator_attempt_id": probe["source_orchestrator_attempt_id"],
        "journal_attempt_id": evidence["journal_attempt_id"],
        "connection_generation": approval["connection_generation"],
        "node_id": probe["node_id"],
        "repo": probe["repo"],
        "branch": probe["branch"],
        "target_head": probe["target_head"],
        "current_head": evidence["current_head"],
        "manifest_identity": evidence["manifest_identity"],
        "manifest_digest": evidence["manifest_digest"],
        "journal_status": "success",
        "journal_completed_at": evidence["journal_completed_at"],
        "original_previous_head": evidence["original_previous_head"],
    }
