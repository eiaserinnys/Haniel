"""Repository HEAD snapshots used by every orchestrated deploy trigger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git import get_head, get_remote_head


@dataclass(frozen=True)
class RepoReconciliationSnapshot:
    node_id: str
    repo: str
    branch: str
    local_head: str
    remote_head: str
    deploy_id: str

    @property
    def in_sync(self) -> bool:
        return self.local_head == self.remote_head

    def mismatch_error(self) -> str:
        return (
            "HEAD mismatch after deploy: "
            f"node={self.node_id} repo={self.repo} branch={self.branch} "
            f"local_head={self.local_head} remote_head={self.remote_head}"
        )


def deterministic_deploy_id(
    node_id: str,
    repo: str,
    branch: str,
    remote_head: str,
) -> str:
    """Return the canonical, trigger-independent deploy ID."""
    return f"{node_id}:{repo}:{branch}:{remote_head}"


def capture_repo_snapshot(
    *,
    node_id: str,
    repo: str,
    branch: str,
    path: Path,
    deploy_id: str | None = None,
) -> RepoReconciliationSnapshot:
    """Read settled Git truth and preserve an existing canonical ID when supplied."""
    local_head = get_head(path)
    remote_head = get_remote_head(path, branch)
    return RepoReconciliationSnapshot(
        node_id=node_id,
        repo=repo,
        branch=branch,
        local_head=local_head,
        remote_head=remote_head,
        deploy_id=deploy_id
        or deterministic_deploy_id(node_id, repo, branch, remote_head),
    )
