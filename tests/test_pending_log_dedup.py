"""Pending notices are logged once per repository and remote head."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.config import HanielConfig
from haniel.core.runner import ServiceRunner


@pytest.fixture
def pending_repos(tmp_path: Path):
    repos = {}
    for name in ("app", "other"):
        (tmp_path / name).mkdir()
        repos[name] = {
            "url": f"git@github.com:test/{name}.git",
            "branch": "main",
            "path": name,
            "auto_apply": False,
        }
    config = HanielConfig.model_validate({"repos": repos, "services": {}})
    runner = ServiceRunner(config, config_dir=tmp_path)
    for state in runner._repo_states.values():
        state.last_head = "local-head"
    heads = {"app": "remote-a", "other": "remote-a"}
    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="local-head"),
        patch(
            "haniel.core.runner.get_remote_head",
            side_effect=lambda path, branch: heads[path.name],
        ),
        patch(
            "haniel.core.runner.get_pending_changes",
            return_value={"commits": ["change"], "stat": "1 file"},
        ),
    ):
        yield runner, heads


def _poll(runner: ServiceRunner) -> None:
    runner._apply_changes(runner._detect_changes())


def _messages(caplog: pytest.LogCaptureFixture, text: str, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if text in record.getMessage() and record.levelno == level
    ]


@pytest.mark.parametrize("global_auto_apply", [False, True], ids=["global", "repo"])
def test_pending_head_logs_once_then_logs_new_head(
    pending_repos, caplog: pytest.LogCaptureFixture, global_auto_apply: bool
) -> None:
    runner, heads = pending_repos
    runner.config.auto_apply = global_auto_apply
    reason = "repo auto_apply=false" if global_auto_apply else "global auto_apply=false"

    with caplog.at_level(logging.DEBUG, logger="haniel.core.runner"):
        _poll(runner)
        _poll(runner)
        heads["app"] = "remote-b"
        _poll(runner)
        _poll(runner)

    remote_info = _messages(caplog, "Remote changes available", logging.INFO)
    assert len(remote_info) == 3
    assert sum("repo: app " in line for line in remote_info) == 2
    assert sum("repo: other " in line for line in remote_info) == 1
    assert len(_messages(caplog, "Remote changes available", logging.DEBUG)) == 5
    reason_info = _messages(caplog, reason, logging.INFO)
    assert len(reason_info) == 2
    assert "['app', 'other']" in reason_info[0]
    assert "['app']" in reason_info[1]
    assert len(_messages(caplog, reason, logging.DEBUG)) == 2


def test_changed_pending_reason_is_logged_again(pending_repos, caplog) -> None:
    runner, _ = pending_repos
    with caplog.at_level(logging.INFO, logger="haniel.core.runner"):
        runner.config.auto_apply = False
        _poll(runner)
        runner.config.auto_apply = True
        _poll(runner)
        runner.config.auto_apply = False
        _poll(runner)
    assert len(_messages(caplog, "global auto_apply=false", logging.INFO)) == 2
    assert len(_messages(caplog, "repo auto_apply=false", logging.INFO)) == 1


def test_cleared_pending_is_logged_again(pending_repos, caplog) -> None:
    runner, heads = pending_repos
    runner.config.auto_apply = False
    with caplog.at_level(logging.INFO, logger="haniel.core.runner"):
        _poll(runner)
        heads["app"] = "local-head"
        _poll(runner)
        heads["app"] = "remote-a"
        _poll(runner)
    assert (
        sum(
            "repo: app " in line
            for line in _messages(caplog, "Remote changes available", logging.INFO)
        )
        == 2
    )
    reasons = _messages(caplog, "global auto_apply=false", logging.INFO)
    assert len(reasons) == 2
    assert "['app']" in reasons[1]
