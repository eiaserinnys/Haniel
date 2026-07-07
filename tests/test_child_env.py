"""Tests for child process environment sanitization."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from haniel.config import (
    HanielConfig,
    HooksConfig,
    RepoConfig,
    ServiceConfig,
)
from haniel.core.child_env import sanitized_child_env
from haniel.core.runner import ServiceRunner


class TestSanitizedChildEnv:
    """Tests for sanitized_child_env()."""

    def test_strips_node_ipc_vars(self, monkeypatch):
        monkeypatch.setenv("NODE_CHANNEL_FD", "3")
        monkeypatch.setenv("NODE_CHANNEL_SERIALIZATION_MODE", "json")
        monkeypatch.setenv("NODE_UNIQUE_ID", "1")

        env = sanitized_child_env()

        assert "NODE_CHANNEL_FD" not in env
        assert "NODE_CHANNEL_SERIALIZATION_MODE" not in env
        assert "NODE_UNIQUE_ID" not in env

    def test_preserves_other_vars(self, monkeypatch):
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setenv("NODE_CHANNEL_FD", "3")

        env = sanitized_child_env()

        assert env["NODE_ENV"] == "production"
        assert "PATH" in env

    def test_no_ipc_vars_is_noop(self, monkeypatch):
        monkeypatch.delenv("NODE_CHANNEL_FD", raising=False)

        env = sanitized_child_env()

        assert "NODE_CHANNEL_FD" not in env


class TestHookEnvSanitization:
    """Hooks must not inherit pm2's Node IPC variables."""

    @patch("subprocess.run")
    def test_execute_hook_strips_node_channel_fd(
        self, mock_run: MagicMock, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("NODE_CHANNEL_FD", "3")
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        config = HanielConfig(
            poll_interval=5,
            repos={
                "test-repo": RepoConfig(
                    url="git@github.com:test/test.git",
                    branch="main",
                    path="./test-repo",
                ),
            },
            services={
                "test-service": ServiceConfig(
                    run="sleep 100",
                    repo="test-repo",
                    hooks=HooksConfig(pre_start="node apply-schema.mjs"),
                ),
            },
        )
        runner = ServiceRunner(config, config_dir=tmp_path)

        assert runner.execute_hook("test-service", "pre_start") is True

        env = mock_run.call_args.kwargs["env"]
        assert "NODE_CHANNEL_FD" not in env
