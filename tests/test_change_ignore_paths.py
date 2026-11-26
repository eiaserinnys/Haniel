"""Change detection using paths ignored by a remote release manifest."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig
from haniel.core.git import GitError
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.runner import ServiceRunner


def _manifest(**extra: object) -> dict:
    return {
        "schema_version": "haniel.release.v1",
        "release_id": "test-release",
        "post_start_verify": [{"name": "verify", "command": "verify"}],
        "recovery": {
            "strategy": "rollback",
            "command": {"name": "restore", "command": "restore"},
        },
        **extra,
    }


@pytest.fixture
def remote_change(tmp_path: Path):
    repo_path = tmp_path / "app"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    config = HanielConfig.model_validate(
        {
            "repos": {
                "app": {
                    "url": "git@github.com:test/app.git",
                    "branch": "main",
                    "path": "app",
                    "release_manifest": "deploy/release-manifest.json",
                }
            },
            "services": {},
            "orchestrator_client": {
                "url": "ws://localhost/ws/node",
                "token": "test",
                "node_id": "node-a",
            },
        }
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    runner._repo_states["app"].last_head = "old-head"
    runner._orch_client = MagicMock()
    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="old-head"),
        patch("haniel.core.runner.get_remote_head", return_value="remote-head"),
        patch(
            "haniel.core.runner.get_pending_changes",
            return_value={"commits": ["remote-head change"], "stat": "2 files"},
        ),
        patch("haniel.core.runner.sha256_file_at_commit", return_value="digest"),
        patch("haniel.core.repo_reconciliation.get_head", return_value="old-head"),
        patch(
            "haniel.core.repo_reconciliation.get_remote_head",
            return_value="remote-head",
        ),
        patch("haniel.core.runner.read_file_at_commit") as read_manifest,
        patch("haniel.core.runner.get_changed_paths") as changed_paths,
        patch("haniel.core.runner.pull_repo") as pull_repo,
    ):
        yield runner, repo_path, read_manifest, changed_paths, pull_repo


def test_all_ignored_paths_do_not_create_pending(remote_change, caplog) -> None:
    runner, repo_path, read_manifest, changed_paths, pull_repo = remote_change
    read_manifest.return_value = json.dumps(
        _manifest(change_ignore_paths=["soul-app/*", ".github/*"])
    ).encode()
    changed_paths.return_value = ["soul-app/src/app.ts", ".github/workflows/ci.yml"]

    with caplog.at_level(logging.INFO, logger="haniel.core.runner"):
        assert runner._detect_changes() == []
        assert runner._detect_changes() == []

    assert runner._repo_states["app"].pending_changes is None
    runner._orch_client.notify_change.assert_not_called()
    pull_repo.assert_not_called()
    read_manifest.assert_called_with(
        repo_path, "origin/main", "deploy/release-manifest.json"
    )
    changed_paths.assert_called_with(path=repo_path, branch="main", local_ref="HEAD")
    assert sum("Ignoring changes" in record.message for record in caplog.records) == 1


def test_unmatched_path_creates_pending(remote_change) -> None:
    runner, _, read_manifest, changed_paths, _ = remote_change
    read_manifest.return_value = json.dumps(
        _manifest(change_ignore_paths=["soul-app/*"])
    ).encode()
    changed_paths.return_value = ["soul-app/src/app.ts", "server/main.py"]

    assert runner._detect_changes() == ["app"]
    assert runner._repo_states["app"].pending_changes["commits"]
    runner._orch_client.notify_change.assert_called_once()


@pytest.mark.parametrize(
    "read_error", [True, False], ids=["read-error", "field-absent"]
)
def test_unavailable_filter_creates_pending(remote_change, read_error, caplog) -> None:
    runner, _, read_manifest, changed_paths, _ = remote_change
    if read_error:
        read_manifest.side_effect = GitError("cannot read manifest")
    else:
        read_manifest.return_value = json.dumps(_manifest()).encode()
    changed_paths.return_value = ["soul-app/src/app.ts"]

    with caplog.at_level(logging.WARNING, logger="haniel.core.runner"):
        assert runner._detect_changes() == ["app"]
        assert runner._detect_changes() == ["app"]
    assert runner._repo_states["app"].pending_changes["commits"]
    assert runner._orch_client.notify_change.call_count == 2
    assert sum(
        "Cannot filter changes" in record.message for record in caplog.records
    ) == (1 if read_error else 0)


def test_manifest_accepts_change_ignore_paths_and_defaults_to_empty() -> None:
    assert ReleaseManifest.model_validate(_manifest()).change_ignore_paths == []
    manifest = ReleaseManifest.model_validate(
        _manifest(change_ignore_paths=["soul-app/*"])
    )
    assert manifest.change_ignore_paths == ["soul-app/*"]


def test_no_notify_while_deployment_owns_repo(remote_change) -> None:
    runner, _, read_manifest, changed_paths, _ = remote_change
    read_manifest.return_value = json.dumps(
        _manifest(change_ignore_paths=["soul-app/*"])
    ).encode()
    changed_paths.return_value = ["scripts/test-affected.mjs"]
    lock = runner._pull_locks["app"]

    lock.acquire()
    try:
        assert runner._detect_changes() == ["app"]
    finally:
        lock.release()
    runner._orch_client.notify_change.assert_not_called()

    changed_paths.return_value = ["soul-app/src/app.ts"]
    runner._repo_states["app"].last_head = "deployed-head"
    with (
        patch("haniel.core.runner.get_head", return_value="deployed-head"),
        patch("haniel.core.repo_reconciliation.get_head", return_value="deployed-head"),
    ):
        runner._detect_changes()
    runner._orch_client.notify_change.assert_not_called()
