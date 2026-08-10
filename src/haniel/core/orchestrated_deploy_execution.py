"""Execute one immutable retry plan selected by the server preflight."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..integrations.deploy_reporting import ApprovalRevalidationError
from .deployment_errors import StableDeploymentError
from .git import get_head, get_pending_changes, get_remote_head
from .runner_deployment import ProgressCallback, run_manifest_deployment
from .runner_config_snapshot import RepoObservation

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
    state = runner._snapshot_repo_runtime(repo)
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
    state = runner._snapshot_repo_runtime(repo)
    if not state.pending_changes:
        repo_path = runner.config_dir / state.config.path
        current_head = get_head(repo_path)
        if current_head == target and repo not in runner._startup_repo_locks:
            return
        if current_head != target:
            pending_changes = get_pending_changes(repo_path, state.config.branch)
            if not pending_changes.get("commits"):
                raise ApprovalRevalidationError(
                    f"approval target {target} is not present and no pending change is available"
                )
            if not runner._commit_repo_observation(
                RepoObservation(
                    generation=state.generation,
                    repo_name=repo,
                    repo_config=state.config,
                    last_fetch=state.last_fetch,
                    fetch_error=state.fetch_error,
                    last_head=state.last_head,
                    pending_changes=pending_changes,
                    changed=True,
                )
            ):
                raise ApprovalRevalidationError(
                    "CONFIG_GENERATION_CHANGED: config changed before approval execution"
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
    state = runner._snapshot_repo_runtime(repo)
    repo_path = runner.config_dir / state.config.path
    current_head = get_head(repo_path)
    lock = state.pull_lock
    target = probe["target_head"]
    if current_head != target:
        if not state.pending_changes:
            from .git import get_pending_changes

            pending = get_pending_changes(repo_path, branch)
            if not runner._commit_repo_observation(
                RepoObservation(
                    generation=state.generation,
                    repo_name=repo,
                    repo_config=state.config,
                    last_fetch=state.last_fetch,
                    fetch_error=state.fetch_error,
                    last_head=state.last_head,
                    pending_changes=pending,
                    changed=True,
                )
            ):
                raise ApprovalRevalidationError(
                    "CONFIG_GENERATION_CHANGED: config changed before retry"
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
            node_id=probe["node_id"],
            branch=branch,
            target_head=target,
            **progress_kwargs,
        )
        return

    if not lock.acquire(blocking=False):
        raise StableDeploymentError(
            "DEPLOYMENT_LEASE_CONFLICT", f"already deploying {repo}"
        )
    deployment_lease = None
    suppressed: list[str] = []
    try:
        deployment_lease = runner.lifecycle_control.acquire_deployment(
            repo, approval["orchestrator_attempt_id"]
        )
        config_snapshot, state = runner._snapshot_repo_and_config(repo)
        repo_path = runner.config_dir / state.config.path
        if get_head(repo_path) != target:
            raise ApprovalRevalidationError(
                "approved target changed before manifest retry execution"
            )
        if state.config.release_manifest is None:
            affected = list(state.affected_services)
            suppressed = affected
            runner._suppress_pending_restarts(suppressed)
            runner._require_config_generation(config_snapshot)
            runner._restart_after_pull_legacy(repo, affected)
            return
        reload_plan = runner._prepare_current_manifest_config(repo)
        config_snapshot, state = runner._snapshot_repo_and_config(repo)
        repo_path = runner.config_dir / state.config.path
        if get_head(repo_path) != target:
            raise ApprovalRevalidationError(
                "approved target changed while binding manifest config"
            )
        affected = (
            list(reload_plan.new_affected)
            if reload_plan is not None
            else list(state.affected_services)
        )
        suppressed = (
            list(reload_plan.quiesce_services) if reload_plan is not None else affected
        )
        runner._suppress_pending_restarts(suppressed)
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
        config_kwargs = (
            {
                "quiesce_services": suppressed,
                "config_digest": reload_plan.config_digest,
                "service_environment_bindings": (reload_plan.service_environment_map()),
            }
            if reload_plan is not None
            else {}
        )
        runner._require_config_generation(config_snapshot)
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
            config_snapshot=config_snapshot,
            **config_kwargs,
            **progress_kwargs,
        )
    finally:
        runner._release_restart_suppression(suppressed)
        if deployment_lease is not None:
            deployment_lease.__exit__(None, None, None)
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
