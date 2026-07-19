"""Evidence recovery for self updates already applied by another process."""

import subprocess
from pathlib import Path

import pytest

from haniel.core.change_evidence import (
    get_applied_change_evidence,
    get_changes_between,
)
from haniel.core.git import GitError, get_head


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True)
    return repo


def test_returns_commits_and_stat_for_explicit_range(git_repo: Path) -> None:
    previous_head = get_head(git_repo)
    (git_repo / "external.txt").write_text("already pulled\n", encoding="utf-8")
    subprocess.run(["git", "add", "external.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "External self update"],
        cwd=git_repo,
        check=True,
    )
    current_head = get_head(git_repo)

    result = get_changes_between(git_repo, previous_head, current_head)

    assert result["commits"] == [f"{current_head[:7]} External self update"]
    assert result["stat"] is not None
    assert "external.txt" in result["stat"]


def test_invalid_range_raises_for_callers_that_require_actual_evidence(
    git_repo: Path,
) -> None:
    with pytest.raises(GitError, match="Failed to inspect git range"):
        get_changes_between(git_repo, "missing-head", get_head(git_repo))


def test_explicit_target_event_falls_back_when_range_is_unavailable(
    git_repo: Path,
) -> None:
    current_head = get_head(git_repo)

    result = get_applied_change_evidence(git_repo, "missing-head", current_head)

    assert result == {
        "commits": [
            f"{current_head} self-update already applied externally from missing-head"
        ],
        "stat": None,
    }
