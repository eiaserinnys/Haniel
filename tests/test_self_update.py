"""Tests for the haniel self-update mechanism.

Tests cover:
- SelfUpdateConfig parsing (config model)
- Exit code constants
- SelfUpdateExit exception
- Runner self-update detection and approval
- Webhook event types for self-update
- WinSW XML generation in wrapper mode
- haniel-runner.conf generation
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel import EXIT_CLEAN, EXIT_SELF_UPDATE, SelfUpdateExit
from haniel.config import (
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    SelfUpdateConfig,
    WebhookConfig,
)
from haniel.integrations.webhook import EventType, EVENT_METADATA


# --- Exit Code Tests ---


class TestExitCodes:
    """Tests for exit code constants and SelfUpdateExit."""

    def test_exit_clean_is_zero(self):
        assert EXIT_CLEAN == 0

    def test_exit_self_update_is_ten(self):
        assert EXIT_SELF_UPDATE == 10

    def test_self_update_exit_is_system_exit(self):
        with pytest.raises(SystemExit) as exc_info:
            raise SelfUpdateExit()
        assert exc_info.value.code == 10

    def test_self_update_exit_caught_by_system_exit(self):
        """SelfUpdateExit should be catchable as SystemExit."""
        caught = False
        try:
            raise SelfUpdateExit()
        except SystemExit as e:
            caught = True
            assert e.code == EXIT_SELF_UPDATE
        assert caught


# --- Config Model Tests ---


class TestSelfUpdateConfig:
    """Tests for SelfUpdateConfig parsing."""

    def test_self_update_config_required_repo(self):
        cfg = SelfUpdateConfig(repo="haniel")
        assert cfg.repo == "haniel"
        assert cfg.auto_update is False

    def test_self_update_config_auto_update(self):
        cfg = SelfUpdateConfig(repo="haniel", auto_update=True)
        assert cfg.auto_update is True

    def test_haniel_config_with_self_update(self):
        """self_update should be parsed from 'self' key via alias."""
        config = HanielConfig.model_validate(
            {
                "repos": {
                    "haniel": {
                        "url": "git@github.com:test/haniel.git",
                        "branch": "main",
                        "path": "./.projects/haniel",
                    }
                },
                "services": {},
                "self": {
                    "repo": "haniel",
                    "auto_update": False,
                },
            }
        )
        assert config.self_update is not None
        assert config.self_update.repo == "haniel"
        assert config.self_update.auto_update is False

    def test_haniel_config_without_self_update(self):
        config = HanielConfig(repos={}, services={})
        assert config.self_update is None


# --- Runner Self-Update Tests ---


class TestRunnerSelfUpdate:
    """Tests for ServiceRunner self-update logic."""

    def _make_config(self, *, auto_update: bool = False, webhooks: bool = False):
        repos = {
            "haniel": RepoConfig(
                url="git@github.com:test/haniel.git",
                branch="main",
                path="./.projects/haniel",
            ),
            "app": RepoConfig(
                url="git@github.com:test/app.git",
                branch="main",
                path="./.projects/app",
            ),
        }
        services = {
            "web": ServiceConfig(run="python server.py", repo="app"),
        }
        webhook_list = (
            [WebhookConfig(url="https://hooks.example.com/test", format="json")]
            if webhooks
            else None
        )
        return HanielConfig.model_validate(
            {
                "repos": {n: r.model_dump() for n, r in repos.items()},
                "services": {n: s.model_dump() for n, s in services.items()},
                "self": {"repo": "haniel", "auto_update": auto_update},
                "webhooks": [w.model_dump() for w in webhook_list]
                if webhook_list
                else None,
            }
        )

    def _make_runner(self, config):
        from haniel.core.runner import ServiceRunner

        with patch.object(ServiceRunner, "__init__", lambda self, *a, **kw: None):
            runner = ServiceRunner.__new__(ServiceRunner)

        # Manually initialize required attributes
        runner.config = config
        runner.config_dir = Path(".")
        runner._self_repo = config.self_update.repo if config.self_update else None
        runner._state = MagicMock()
        runner._state.self_update_pending = False
        runner._state_lock = threading.Lock()
        runner._restart_lock = threading.Lock()
        runner._stop_event = threading.Event()
        runner._self_update_requested = threading.Event()
        runner._pending_restarts = {}
        runner._dependency_graph = MagicMock()
        runner._dependency_graph.get_dependencies.return_value = []
        runner._dependency_graph.get_dependents.return_value = []

        return runner

    def test_self_repo_detection(self):
        """Runner should identify the self-update repo."""
        config = self._make_config()
        runner = self._make_runner(config)
        assert runner._self_repo == "haniel"

    def test_auto_update_signals_event(self):
        """auto_update=true should signal self_update_requested and call stop."""
        config = self._make_config(auto_update=True)
        runner = self._make_runner(config)
        runner.stop = MagicMock()

        runner._initiate_self_update()

        assert runner.self_update_requested is True
        runner.stop.assert_called_once()

    def test_manual_update_sets_pending(self):
        """auto_update=false should set pending state without exiting."""
        config = self._make_config(auto_update=False)
        runner = self._make_runner(config)

        runner._initiate_self_update()

        assert runner._state.self_update_pending is True
        assert runner.self_update_requested is False

    def test_approve_self_update_signals_event(self):
        """Approving a pending update should signal self_update_requested."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner.stop = MagicMock()

        result = runner.approve_self_update()

        assert runner.self_update_requested is True
        runner.stop.assert_called_once()
        assert "approved" in result.lower()

    def test_approve_no_pending_returns_message(self):
        """Approving when no update is pending should return a message."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = False

        result = runner.approve_self_update()
        assert "No self-update pending" in result

    def test_apply_changes_intercepts_self_repo(self):
        """_apply_changes should call _initiate_self_update for self repo."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._initiate_self_update = MagicMock()
        runner.get_affected_services = MagicMock(return_value=[])
        runner._pull_repo = MagicMock()

        runner._apply_changes(["haniel", "app"])

        runner._initiate_self_update.assert_called_once()
        # "app" should still be processed normally
        runner._pull_repo.assert_called_once_with("app")

    def test_apply_changes_self_repo_only(self):
        """_apply_changes with only self repo should return after self-update."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._initiate_self_update = MagicMock()
        runner._pull_repo = MagicMock()

        runner._apply_changes(["haniel"])

        runner._initiate_self_update.assert_called_once()
        runner._pull_repo.assert_not_called()

    def test_get_status_includes_self_update(self):
        """get_status should include self_update section when configured."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._enabled_services = {}
        runner._repo_states = {}
        runner.poll_interval = 60
        runner.health_manager = MagicMock()

        status = runner.get_status()

        assert "self_update" in status
        assert status["self_update"]["repo"] == "haniel"
        assert status["self_update"]["pending"] is False


# --- Webhook Event Tests ---


class TestSelfUpdateWebhookEvents:
    """Tests for self-update webhook event types."""

    def test_self_update_detected_event_exists(self):
        assert EventType.SELF_UPDATE_DETECTED == "self_update_detected"

    def test_self_update_approved_event_exists(self):
        assert EventType.SELF_UPDATE_APPROVED == "self_update_approved"

    def test_self_update_detected_metadata(self):
        metadata = EVENT_METADATA[EventType.SELF_UPDATE_DETECTED]
        assert "title" in metadata
        assert "color" in metadata

    def test_self_update_approved_metadata(self):
        metadata = EVENT_METADATA[EventType.SELF_UPDATE_APPROVED]
        assert "title" in metadata
        assert "color" in metadata


# --- Validator Tests ---


class TestSelfUpdateValidation:
    """Tests for self-update config validation."""

    def test_valid_self_repo_reference(self):
        from haniel.config.validators import check_missing_references

        config = HanielConfig.model_validate(
            {
                "repos": {
                    "haniel": {
                        "url": "git@github.com:test/haniel.git",
                        "branch": "main",
                        "path": "./.projects/haniel",
                    }
                },
                "services": {},
                "self": {"repo": "haniel"},
            }
        )
        errors = check_missing_references(config)
        assert len(errors) == 0

    def test_invalid_self_repo_reference(self):
        from haniel.config.validators import check_missing_references

        config = HanielConfig.model_validate(
            {
                "repos": {},
                "services": {},
                "self": {"repo": "nonexistent"},
            }
        )
        errors = check_missing_references(config)
        assert len(errors) == 1
        assert "self.repo" in errors[0].location
        assert "nonexistent" in errors[0].message


# --- Installer Tests ---


class TestWrapperModeInstaller:
    """Tests for WinSW XML generation in wrapper mode."""

    def test_winsw_xml_wrapper_mode(self):
        """When self-update is configured, XML should use PowerShell wrapper."""
        from haniel.config import ServiceDefinitionConfig
        from haniel.installer.finalize import Finalizer

        config = HanielConfig.model_validate(
            {
                "repos": {
                    "haniel": {
                        "url": "git@github.com:test/haniel.git",
                        "branch": "main",
                        "path": "./.projects/haniel",
                    }
                },
                "services": {},
                "self": {"repo": "haniel"},
            }
        )

        finalizer = Finalizer(
            config=config,
            config_dir=Path("."),
            state=MagicMock(),
            config_filename="haniel.yaml",
        )

        service_cfg = ServiceDefinitionConfig(name="haniel")

        with patch(
            "shutil.which",
            return_value="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        ):
            xml = finalizer._generate_winsw_xml(service_cfg, "C:\\haniel")

        assert "powershell" in xml.lower()
        assert "haniel-runner.ps1" in xml
        assert "-ExecutionPolicy Bypass" in xml

    def test_winsw_xml_direct_mode(self):
        """Without self-update, XML should use Python directly."""
        from haniel.config import ServiceDefinitionConfig
        from haniel.installer.finalize import Finalizer

        config = HanielConfig(repos={}, services={})

        finalizer = Finalizer(
            config=config,
            config_dir=Path("."),
            state=MagicMock(),
            config_filename="haniel.yaml",
        )

        service_cfg = ServiceDefinitionConfig(name="haniel")

        with patch("shutil.which", return_value="C:\\Python312\\python.exe"):
            xml = finalizer._generate_winsw_xml(service_cfg, "C:\\haniel")

        assert "python" in xml.lower()
        assert "-m haniel.cli run" in xml
        assert "haniel-runner.ps1" not in xml

    def test_generate_runner_conf(self):
        """_generate_runner_conf should create a valid conf file."""
        import tempfile

        from haniel.installer.finalize import Finalizer

        config = HanielConfig.model_validate(
            {
                "repos": {
                    "haniel": {
                        "url": "git@github.com:test/haniel.git",
                        "branch": "main",
                        "path": "./.projects/haniel",
                    }
                },
                "services": {},
                "self": {"repo": "haniel"},
                "webhooks": [{"url": "https://hooks.example.com/test"}],
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            finalizer = Finalizer(
                config=config,
                config_dir=Path(tmpdir),
                state=MagicMock(),
                config_filename="haniel.yaml",
            )

            finalizer._generate_runner_conf()

            conf_path = Path(tmpdir) / "haniel-runner.conf"
            assert conf_path.exists()

            content = conf_path.read_text(encoding="utf-8")
            assert "HANIEL_REPO=./.projects/haniel" in content
            assert "CONFIG=haniel.yaml" in content
            assert "WEBHOOK_URL=https://hooks.example.com/test" in content
            assert "MAX_GIT_FAILURES=3" in content
