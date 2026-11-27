"""배포 푸시 알림(변경점, 성공, 실패)의 제목, 본문, data를 한 꼴로 만든다."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

PENDING_TITLE = "배포 대기"
SUCCESS_TITLE = "배포 성공"
FAILED_TITLE = "배포 실패"

PENDING_ACTION = "변경 감지"
SUCCESS_ACTION = "배포 완료"
FAILED_ACTION = "배포 실패"


def _commit_subject(commit: str) -> str:
    _, sep, subject = commit.partition(" ")
    return subject if sep else commit


def build_push(
    *,
    title_prefix: str,
    action: str,
    deploy_id: str,
    node_id: str,
    repo: str,
    branch: str,
    affected_services: Sequence[str],
    commits: Sequence[str],
    extra_data: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    lines = [f"{node_id}에서 {repo} ({branch}) {action}"]
    if affected_services:
        lines.append(f"서비스: {', '.join(affected_services)}")
    if commits:
        summary = _commit_subject(commits[0])
        if len(commits) > 1:
            summary += f" 외 {len(commits) - 1}건"
        lines.append(f"변경: {summary}")
    data = {
        "deploy_id": deploy_id,
        "node_id": node_id,
        "repo": repo,
        **extra_data,
    }
    return f"{title_prefix}: {repo}", "\n".join(lines), data
