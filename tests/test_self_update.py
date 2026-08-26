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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from haniel import EXIT_CLEAN, EXIT_SELF_UPDATE, SelfUpdateExit
from haniel.config import (
    HanielConfig,
    RepoConfig,
    SelfUpdateConfig,
    ServiceConfig,
    WebhookConfig,
)
from haniel.core.deployment_errors import StableDeploymentError
from haniel.integrations.webhook import EVENT_METADATA, EventType

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
        assert cfg.prepare_timeout == 3600

    def test_self_update_config_prepare_timeout_must_be_positive(self):
        assert SelfUpdateConfig(repo="haniel", prepare_timeout=45).prepare_timeout == 45
        with pytest.raises(ValueError):
            SelfUpdateConfig(repo="haniel", prepare_timeout=0)

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
        from haniel.core.runner import RepoState, ServiceRunner

        with patch.object(ServiceRunner, "__init__", lambda self, *a, **kw: None):
            runner = ServiceRunner.__new__(ServiceRunner)

        # Manually initialize required attributes
        runner.config = config
        runner.config_dir = Path(".")
        runner._self_repo = config.self_update.repo if config.self_update else None
        runner._state = MagicMock()
        runner._state.self_update_pending = False
        runner._ws_handler = None
        runner._state_lock = threading.Lock()
        runner._config_reload_lock = threading.RLock()
        runner._config_generation = 0
        runner._restart_lock = threading.Lock()
        runner._stop_event = threading.Event()
        runner._stop_complete = threading.Event()
        runner._stop_complete.set()
        runner._stop_owner_thread_id = None
        runner._stop_error = None
        runner._self_update_requested = threading.Event()
        runner._self_update_prestager = MagicMock()
        runner._self_update_prestager.freeze_target.return_value = "a" * 40
        runner._self_update_prestager.prepare.return_value = SimpleNamespace(
            target_commit="a" * 40,
            prepared_release=Path("prepared"),
        )
        runner._last_self_update_result = None
        runner._last_pending_hash = {}
        runner._pending_restarts = {}
        runner._enabled_services = config.services
        runner._repo_states = {
            name: RepoState(name=name, config=repo)
            for name, repo in config.repos.items()
        }
        runner._pull_locks = {name: threading.Lock() for name in config.repos}
        runner.process_manager = MagicMock()
        runner.process_manager.is_running.return_value = False
        runner._dependency_graph = MagicMock()
        runner._dependency_graph.get_dependencies.return_value = []
        runner._dependency_graph.get_dependents.return_value = []
        runner._dependency_graph.topological_sort.return_value = list(
            config.services.keys()
        )
        runner._startup_order = tuple(config.services.keys())
        runner._shutdown_order = tuple(reversed(runner._startup_order))

        return runner

    def test_self_repo_detection(self):
        """Runner should identify the self-update repo."""
        config = self._make_config()
        runner = self._make_runner(config)
        assert runner._self_repo == "haniel"

    def test_auto_update_signals_event(self):
        """auto_update=true should stop on a non-daemon thread outside polling."""
        config = self._make_config(auto_update=True)
        runner = self._make_runner(config)
        runner.stop = MagicMock()

        with patch("haniel.core.runner.threading.Thread") as thread_class:
            runner._initiate_self_update()

        assert runner.self_update_requested is True
        runner.process_manager.is_running.assert_called_with("web")
        assert thread_class.call_args.kwargs == {
            "target": runner.stop,
            "name": "haniel-auto-update-stop",
            "daemon": False,
        }
        thread_class.return_value.start.assert_called_once_with()

    def test_manual_update_sets_pending(self):
        """auto_update=false should set pending state without exiting."""
        config = self._make_config(auto_update=False)
        runner = self._make_runner(config)

        runner._initiate_self_update()

        assert runner._state.self_update_pending is True
        assert runner.self_update_requested is False

    def test_apply_changes_rejects_stale_self_repo_before_shutdown(self):
        config = self._make_config(auto_update=True)
        runner = self._make_runner(config)
        runner.stop = MagicMock()
        original_snapshot = runner._snapshot_config_state
        calls = 0

        def snapshot_with_reload():
            nonlocal calls
            calls += 1
            snapshot = original_snapshot()
            if calls == 1:
                replacement = snapshot.config.model_copy(
                    update={
                        "self_update": SelfUpdateConfig(repo="app", auto_update=True)
                    },
                    deep=True,
                )
                runner._replace_config_snapshot(replacement, snapshot.generation)
            return snapshot

        runner._snapshot_config_state = MagicMock(side_effect=snapshot_with_reload)

        with pytest.raises(StableDeploymentError) as caught:
            runner._apply_changes(["haniel"])

        assert caught.value.code == "CONFIG_GENERATION_CHANGED"
        runner.stop.assert_not_called()
        assert runner.self_update_requested is False

    def test_approve_self_update_signals_event(self):
        """Approving a pending update should signal self_update_requested.

        Note: approve_self_update() no longer calls stop() directly.
        The caller (API/MCP handler) is responsible for scheduling stop()
        after sending the response (see ADR comment in runner.py).
        """
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True

        result = runner.approve_self_update()

        assert runner.self_update_requested is True
        runner.process_manager.is_running.assert_called_with("web")
        assert "approved" in result.lower()

    def test_approve_prepares_before_stopping_services(self):
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        order: list[str] = []
        runner._self_update_prestager.freeze_target.side_effect = (
            lambda *_args, **_kwargs: order.append("fetch-freeze") or "a" * 40
        )
        runner._self_update_prestager.prepare.side_effect = lambda *_args, **_kwargs: (
            order.append("prepare") or SimpleNamespace(target_commit="a" * 40)
        )
        runner._prepare_self_update_shutdown = MagicMock(
            side_effect=lambda: order.append("stop-services")
        )

        runner.approve_self_update()

        assert order == ["fetch-freeze", "prepare", "stop-services"]

    def test_prepare_failure_keeps_pending_and_does_not_stop_services(self):
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner._self_update_prestager.prepare.side_effect = RuntimeError(
            "release_ready failed"
        )
        runner._prepare_self_update_shutdown = MagicMock()

        with pytest.raises(RuntimeError, match="release_ready failed"):
            runner.approve_self_update(target_commit="a" * 40)

        assert runner._state.self_update_pending is True
        assert runner.self_update_requested is False
        runner._prepare_self_update_shutdown.assert_not_called()
        runner._self_update_prestager.discard_result.assert_called_once_with()

    def test_approve_uses_supported_progress_stages(self):
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        progress: list[str] = []

        runner.approve_self_update(
            target_commit="a" * 40,
            progress_callback=progress.append,
        )

        assert progress == ["preparing", "starting"]

    def test_approve_self_update_clears_pending_state_before_restart(self):
        """Approval should clear visible pending state before wrapper restart."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner._repo_states["haniel"].pending_changes = {
            "commits": ["abc123 fix: update haniel"],
            "stat": "1 file changed",
        }
        runner._last_pending_hash["haniel"] = "stale"

        result = runner.approve_self_update()

        assert "approved" in result.lower()
        assert runner._state.self_update_pending is False
        assert runner._repo_states["haniel"].pending_changes is None
        assert "haniel" not in runner._last_pending_hash

    def test_approve_self_update_clears_slack_pending_button(self):
        """Approving a self-update should replace the Slack pending DM."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner._slack_bot = MagicMock()

        result = runner.approve_self_update()

        assert "approved" in result.lower()
        runner._slack_bot.notify_pulling.assert_called_once_with("haniel", auto=False)

    def test_approve_self_update_blocks_when_service_stop_fails(self):
        """Self-update should not proceed if a managed service cannot stop."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner.process_manager.is_running.return_value = True
        runner.process_manager.stop_service.return_value = False

        with pytest.raises(RuntimeError, match="Failed to stop all services"):
            runner.approve_self_update()

        assert runner.self_update_requested is False

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
        runner.trigger_pull = MagicMock()

        runner._apply_changes(["haniel", "app"])

        runner._initiate_self_update.assert_called_once()
        # "app" should still be processed normally
        runner.trigger_pull.assert_called_once_with("app", auto=True)

    def test_apply_changes_self_repo_only(self):
        """_apply_changes with only self repo should return after self-update."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._initiate_self_update = MagicMock()
        runner.trigger_pull = MagicMock()

        runner._apply_changes(["haniel"])

        runner._initiate_self_update.assert_called_once()
        runner.trigger_pull.assert_not_called()

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

    def test_approve_self_update_broadcasts_started(self):
        """approve_self_update with a pending update should broadcast started."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._state.self_update_pending = True
        runner._ws_handler = MagicMock()

        result = runner.approve_self_update()

        assert runner.self_update_requested is True
        assert "approved" in result.lower()
        runner._ws_handler.broadcast_self_update_started.assert_called_once_with(
            "haniel"
        )

    def test_get_status_includes_last_result_when_loaded(self):
        """get_status should expose last_result when a marker was consumed."""
        from haniel.core.self_update_marker import SelfUpdateResult, SelfUpdateStep

        config = self._make_config()
        runner = self._make_runner(config)
        runner._enabled_services = {}
        runner._repo_states = {}
        runner.poll_interval = 60
        runner.health_manager = MagicMock()
        runner._last_self_update_result = SelfUpdateResult(
            version=1,
            started_at="2026-05-05T12:00:00+09:00",
            finished_at="2026-05-05T12:01:00+09:00",
            ok=True,
            steps=[SelfUpdateStep(name="git_fetch", ok=True)],
            error=None,
        )

        status = runner.get_status()

        assert status["self_update"]["last_result"] is not None
        assert status["self_update"]["last_result"]["ok"] is True
        assert status["self_update"]["last_result"]["version"] == 1

    def test_get_status_last_result_none_when_not_loaded(self):
        """When no marker was consumed, last_result should be None."""
        config = self._make_config()
        runner = self._make_runner(config)
        runner._enabled_services = {}
        runner._repo_states = {}
        runner.poll_interval = 60
        runner.health_manager = MagicMock()
        # _last_self_update_result default is None (set in __init__).
        runner._last_self_update_result = None

        status = runner.get_status()

        assert status["self_update"]["last_result"] is None


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
            assert "HANIEL_RELEASE_ROOT=.local/haniel-releases" in content
            assert "HANIEL_RELEASE_RETAIN_EXTRA=3" in content
            assert "HANIEL_RELEASE_MIN_FREE_MB=5120" in content
            assert "CONFIG=haniel.yaml" in content
            assert "WEBHOOK_URL=https://hooks.example.com/test" in content
            assert "MAX_GIT_FAILURES=3" in content
            assert "SELF_UPDATE_EXIT_TIMEOUT=60" in content
            assert "CRASH_RESTART_BASE_SECONDS=5" in content
            assert "CRASH_RESTART_MAX_SECONDS=60" in content
            assert "CRASH_RESET_SECONDS=300" in content

    def test_runner_script_writes_marker_only_after_self_update_exit(self):
        """Wrapper result marker should not be rewritten on ordinary restarts."""
        script_path = Path(__file__).resolve().parents[1] / "haniel-runner.ps1"
        script = script_path.read_text(encoding="utf-8-sig")

        marker_write = script.index(
            "Write-SelfUpdateMarker -Ok ([bool]$script:PreparationResult.ok)"
        )
        guard = script.rfind("if ($writeSelfUpdateMarker)", 0, marker_write)
        assert guard != -1

        self_update_branch = script.index("elseif ($exitCode -eq $EXIT_SELF_UPDATE)")
        restart_branch = script.index("elseif ($exitCode -eq $EXIT_RESTART)")
        flag_set = script.index("$writeSelfUpdateMarker = $true", self_update_branch)
        assert flag_set < restart_branch

    def test_windows_runner_reloads_itself_after_its_file_changes(self):
        """The running PowerShell AST must not outlive an updated wrapper file."""
        script_path = Path(__file__).resolve().parents[1] / "haniel-runner.ps1"
        script = script_path.read_text(encoding="utf-8-sig")

        assert "Re-executing current release wrapper" in script
        assert "& $activeRunner" in script
        assert '"--retain-extra", "$HanielReleaseRetainExtra"' in script
        assert '"--min-free-mb", "$HanielReleaseMinFreeMB"' in script
        assert "$env:HANIEL_ACTIVE_SELF_HEAD = $script:ActiveCommit" in script
        assert '"--active-self-head", $script:ActiveCommit' not in script
        launch = script.index(
            "$hanielProcess = Start-RunnerProcess -Arguments $runArguments"
        )
        prune = script.index("$pruneProcess = Start-ReleasePrune", launch)
        wait = script.index("$hanielProcess.WaitForExit()", prune)
        assert launch < prune < wait
        assert "ConvertTo-WindowsCommandLineArgument" in script
        assert "UseShellExecute = $false" in script
        assert "RedirectStandardOutput = $false" in script
        assert "RedirectStandardError = $false" in script
        prune_function = script[
            script.index("function Start-ReleasePrune") : script.index(
                "function Complete-ReleasePrune"
            )
        ]
        assert "try {" in prune_function
        assert "catch {" in prune_function
        assert "Send-Webhook" in prune_function
        assert "return $null" in prune_function
        complete_function = script[
            script.index("function Complete-ReleasePrune") : script.index(
                "function Stop-ReleasePrune"
            )
        ]
        assert "without a result" in complete_function
        marker_write = script.index(
            "Write-SelfUpdateMarker -Ok ([bool]$script:PreparationResult.ok)"
        )
        reload_wrapper = script.index("& $activeRunner")
        assert marker_write < reload_wrapper

    def test_windows_runner_decodes_utf8_explicitly(self):
        """Windows PowerShell 5.1 Get-Content defaults to ANSI (CP949 on ko-KR).

        The helper writes its result JSON as UTF-8 without BOM, so every file
        read in the wrapper must pass -Encoding UTF8 or non-ASCII error text
        (e.g. localized WinError messages) turns into mojibake that survives
        into self_update_result.json and haniel.log. Regression: 260818.
        """
        script_path = Path(__file__).resolve().parents[1] / "haniel-runner.ps1"
        script = script_path.read_text(encoding="utf-8-sig")

        assert "Get-Content $PreparationResultPath -Raw -Encoding UTF8" in script
        assert "Get-Content $ConfPath -Encoding UTF8" in script
        # Python children must emit UTF-8 stdio to match Console.OutputEncoding.
        assert '$env:PYTHONIOENCODING = "utf-8"' in script
        # No file read may rely on the platform-default encoding.
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "Get-Content" in stripped:
                assert "-Encoding UTF8" in stripped, line
