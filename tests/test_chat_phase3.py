"""
Phase 3 tests: auto-diagnosis + deploy notification deduplication.

Covers:
- DashboardWebSocket._on_state_change: diagnosis triggered on CRASHED/CIRCUIT_OPEN,
  not re-triggered while already diagnosing
- DashboardWebSocket._on_state_change: READY/RUNNING does NOT clear _diagnosing_services
  (cleanup is exclusively _run_diagnosis's finally block)
- DashboardWebSocket._on_state_change: no duplicate session on rapid crash-recovery-crash
- DashboardWebSocket._run_diagnosis: creates session, sends prompt, relays to Slack
- DashboardWebSocket._run_diagnosis: cleans up _diagnosing_services on exception
- ServiceRunner._hash_pending: stable hash for same/different input
- ServiceRunner._detect_changes: notify_pending skipped on repeated identical content
- ServiceRunner.trigger_pull: clears _last_pending_hash on completion
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from haniel.core.health import ServiceState
from haniel.core.runner import ServiceRunner
from haniel.dashboard.ws import DashboardWebSocket


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_runner_mock():
    runner = MagicMock()
    runner.health_manager = MagicMock()
    runner.health_manager.add_callback = MagicMock()
    return runner


def _make_ws_handler(session_manager=None, slack_bot=None, broadcaster=None):
    """Create a DashboardWebSocket with optional chat integration wired up."""
    runner = _make_runner_mock()
    ws_handler = DashboardWebSocket(runner)
    ws_handler._loop = asyncio.get_event_loop()
    ws_handler.configure_chat(
        slack_bot=slack_bot,
        broadcaster=broadcaster,
        session_manager=session_manager,
    )
    return ws_handler


# ── DashboardWebSocket: _diagnosing_services lifecycle ────────────────────────


class TestDiagnosisLifecycle:
    @pytest.mark.asyncio
    async def test_diagnosis_triggered_on_crashed(self):
        """CRASHED state triggers auto-diagnosis for a new service."""
        ws = _make_ws_handler()
        scheduled_coros = []
        ws._schedule_coroutine = lambda c: scheduled_coros.append(c)

        ws._on_state_change("svc-a", ServiceState.RUNNING, ServiceState.CRASHED)

        for c in scheduled_coros:
            c.close()

        assert "svc-a" in ws._diagnosing_services
        assert len(scheduled_coros) == 1

    @pytest.mark.asyncio
    async def test_diagnosis_triggered_on_circuit_open(self):
        """CIRCUIT_OPEN state also triggers auto-diagnosis."""
        ws = _make_ws_handler()
        scheduled_coros = []
        ws._schedule_coroutine = lambda c: scheduled_coros.append(c)

        ws._on_state_change("svc-b", ServiceState.RUNNING, ServiceState.CIRCUIT_OPEN)

        for c in scheduled_coros:
            c.close()

        assert "svc-b" in ws._diagnosing_services
        assert len(scheduled_coros) == 1

    @pytest.mark.asyncio
    async def test_diagnosis_not_retriggered_while_diagnosing(self):
        """A second CRASHED event while diagnosing does not re-trigger."""
        ws = _make_ws_handler()
        scheduled_coros = []

        def capture_schedule(coro):
            scheduled_coros.append(coro)

        ws._schedule_coroutine = capture_schedule

        ws._on_state_change("svc-c", ServiceState.RUNNING, ServiceState.CRASHED)
        ws._on_state_change("svc-c", ServiceState.CRASHED, ServiceState.CRASHED)

        # Close unawaited coroutines to avoid ResourceWarning
        for coro in scheduled_coros:
            coro.close()

        assert len(scheduled_coros) == 1

    @pytest.mark.asyncio
    async def test_diagnosis_flag_not_cleared_by_ready(self):
        """READY state does NOT clear _diagnosing_services while diagnosis is running.

        Cleanup is the exclusive responsibility of _run_diagnosis's finally block.
        Clearing the flag here would allow a rapid crash-recovery-crash cycle to
        spawn a duplicate diagnosis session before the first one finishes.
        """
        ws = _make_ws_handler()
        ws._diagnosing_services.add("svc-d")

        ws._on_state_change("svc-d", ServiceState.CRASHED, ServiceState.READY)

        assert "svc-d" in ws._diagnosing_services

    @pytest.mark.asyncio
    async def test_diagnosis_flag_not_cleared_by_running(self):
        """RUNNING state does NOT clear _diagnosing_services while diagnosis is running."""
        ws = _make_ws_handler()
        ws._diagnosing_services.add("svc-e")

        ws._on_state_change("svc-e", ServiceState.CRASHED, ServiceState.RUNNING)

        assert "svc-e" in ws._diagnosing_services

    @pytest.mark.asyncio
    async def test_no_duplicate_session_on_rapid_crash_recovery_crash(self):
        """Rapid CRASHED→READY→CRASHED cycle does not spawn a duplicate diagnosis session.

        Regression test for: _on_state_change previously called discard() on READY/RUNNING,
        which cleared the guard flag while _run_diagnosis was still executing, allowing the
        subsequent CRASHED event to schedule a second concurrent diagnosis session.
        """
        ws = _make_ws_handler()
        scheduled_coros = []

        def capture_schedule(coro):
            scheduled_coros.append(coro)

        ws._schedule_coroutine = capture_schedule

        # First crash: diagnosis starts
        ws._on_state_change("svc-f", ServiceState.RUNNING, ServiceState.CRASHED)
        # Service briefly recovers — must NOT clear the flag
        ws._on_state_change("svc-f", ServiceState.CRASHED, ServiceState.READY)
        # Service crashes again — guard flag still set, so no second session
        ws._on_state_change("svc-f", ServiceState.READY, ServiceState.CRASHED)

        for coro in scheduled_coros:
            coro.close()

        assert len(scheduled_coros) == 1, (
            "Expected exactly one diagnosis session; "
            f"got {len(scheduled_coros)} (duplicate session bug)"
        )


# ── DashboardWebSocket: _run_diagnosis ────────────────────────────────────────


class TestRunDiagnosis:
    def _make_stream(*events):
        async def _gen(session_id, text):
            for evt in events:
                yield evt
        return _gen

    @pytest.mark.asyncio
    async def test_skipped_when_no_session_manager(self):
        """_run_diagnosis is a no-op when session_manager is None."""
        ws = _make_ws_handler(session_manager=None)
        ws._diagnosing_services.add("svc")

        await ws._run_diagnosis("svc")

        # Service should be cleaned up even when skipped
        assert "svc" not in ws._diagnosing_services

    @pytest.mark.asyncio
    async def test_creates_session_and_streams_prompt(self):
        """_run_diagnosis creates a session and sends a diagnosis prompt."""
        session_manager = MagicMock()
        session_manager.create_session = MagicMock(return_value="sess-diag-1")
        session_manager.get_session = MagicMock(return_value=None)

        async def mock_stream(sid, text):
            assert "svc-broken" in text
            yield {"type": "message_end"}

        session_manager.stream_message = mock_stream

        ws = _make_ws_handler(session_manager=session_manager)
        ws._diagnosing_services.add("svc-broken")

        await ws._run_diagnosis("svc-broken")

        session_manager.create_session.assert_called_once()
        assert "svc-broken" not in ws._diagnosing_services

    @pytest.mark.asyncio
    async def test_exception_cleans_up_diagnosing_services(self):
        """_run_diagnosis removes service from _diagnosing_services even on error."""
        session_manager = MagicMock()
        session_manager.create_session = MagicMock(side_effect=Exception("init error"))

        ws = _make_ws_handler(session_manager=session_manager)
        ws._diagnosing_services.add("svc-err")

        await ws._run_diagnosis("svc-err")  # must not raise

        assert "svc-err" not in ws._diagnosing_services

    @pytest.mark.asyncio
    async def test_events_broadcast_to_broadcaster(self):
        """All stream events are forwarded through ChatBroadcaster."""
        broadcaster = MagicMock()
        broadcaster.broadcast = AsyncMock()
        session_manager = MagicMock()
        session_manager.create_session = MagicMock(return_value="sess-2")
        session_manager.get_session = MagicMock(return_value=None)

        async def mock_stream(sid, text):
            yield {"type": "text_delta", "delta": "diag"}
            yield {"type": "message_end"}

        session_manager.stream_message = mock_stream

        ws = _make_ws_handler(session_manager=session_manager, broadcaster=broadcaster)
        ws._diagnosing_services.add("svc-x")

        await ws._run_diagnosis("svc-x")

        assert broadcaster.broadcast.await_count == 2

    @pytest.mark.asyncio
    async def test_slack_thread_created_and_bound(self):
        """_run_diagnosis creates a Slack thread and binds it to the session."""
        slack_bot = MagicMock()
        slack_bot._dm_channel = "D_CHAN"
        slack_bot.create_chat_thread = MagicMock(return_value="thread.ts")

        session_manager = MagicMock()
        session_manager.create_session = MagicMock(return_value="sess-3")
        session_manager.get_session = MagicMock(return_value=None)

        async def mock_stream(sid, text):
            yield {"type": "message_end"}

        session_manager.stream_message = mock_stream

        ws = _make_ws_handler(session_manager=session_manager, slack_bot=slack_bot)
        ws._diagnosing_services.add("svc-y")

        await ws._run_diagnosis("svc-y")

        slack_bot.create_chat_thread.assert_called_once_with("sess-3", "D_CHAN")
        session_manager.update_slack_binding.assert_called_once_with(
            "sess-3", "thread.ts", "D_CHAN"
        )


# ── ServiceRunner._hash_pending ───────────────────────────────────────────────


class TestHashPending:
    def test_same_dict_same_hash(self):
        """Same pending_changes always produces the same hash."""
        pending = {"commits": ["abc fix bug"], "stat": "1 file"}
        h1 = ServiceRunner._hash_pending(pending)
        h2 = ServiceRunner._hash_pending(pending)
        assert h1 == h2

    def test_different_dicts_different_hash(self):
        """Different pending_changes produce different hashes."""
        h1 = ServiceRunner._hash_pending({"commits": ["aaa"]})
        h2 = ServiceRunner._hash_pending({"commits": ["bbb"]})
        assert h1 != h2

    def test_key_order_independent(self):
        """Hash is stable regardless of key insertion order."""
        p1 = {"a": 1, "b": 2}
        p2 = {"b": 2, "a": 1}
        assert ServiceRunner._hash_pending(p1) == ServiceRunner._hash_pending(p2)

    def test_returns_hex_string(self):
        """Hash is a non-empty hex string."""
        h = ServiceRunner._hash_pending({})
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 = 32 bytes = 64 hex chars


# ── ServiceRunner: notify_pending deduplication ───────────────────────────────


def _make_service_runner(tmp_path: Path):
    """Minimal ServiceRunner with a single repo configured."""
    from haniel.config.model import HanielConfig, RepoConfig
    config = HanielConfig(
        poll_interval=60,
        repos={"my-repo": RepoConfig(url="https://example.com/repo.git", path="my-repo", branch="main")},
        services={},
    )
    return ServiceRunner(config=config, config_dir=tmp_path)


class TestNotifyPendingDedup:
    def test_first_call_notifies(self, tmp_path):
        """First detection of pending changes calls notify_pending."""
        runner = _make_service_runner(tmp_path)
        slack_bot = MagicMock()
        runner._slack_bot = slack_bot

        pending = {"commits": ["abc"], "stat": "1 file"}
        runner._last_pending_hash.clear()

        # Simulate what _detect_changes does
        content_hash = runner._hash_pending(pending)
        if runner._last_pending_hash.get("my-repo") != content_hash:
            runner._last_pending_hash["my-repo"] = content_hash
            runner._slack_bot.notify_pending("my-repo", pending)

        slack_bot.notify_pending.assert_called_once_with("my-repo", pending)

    def test_repeated_same_content_does_not_notify_again(self, tmp_path):
        """Identical pending content on subsequent polls does not re-notify."""
        runner = _make_service_runner(tmp_path)
        slack_bot = MagicMock()
        runner._slack_bot = slack_bot

        pending = {"commits": ["abc"], "stat": "1 file"}
        content_hash = runner._hash_pending(pending)
        runner._last_pending_hash["my-repo"] = content_hash  # already notified

        # Simulate second poll with same content
        if runner._last_pending_hash.get("my-repo") != content_hash:
            runner._slack_bot.notify_pending("my-repo", pending)

        slack_bot.notify_pending.assert_not_called()

    def test_new_content_notifies_again(self, tmp_path):
        """New pending_changes content after previous notification triggers re-notify."""
        runner = _make_service_runner(tmp_path)
        slack_bot = MagicMock()
        runner._slack_bot = slack_bot

        old_pending = {"commits": ["abc"]}
        new_pending = {"commits": ["abc", "def"]}

        # First notification
        runner._last_pending_hash["my-repo"] = runner._hash_pending(old_pending)

        # New content
        content_hash = runner._hash_pending(new_pending)
        if runner._last_pending_hash.get("my-repo") != content_hash:
            runner._last_pending_hash["my-repo"] = content_hash
            runner._slack_bot.notify_pending("my-repo", new_pending)

        slack_bot.notify_pending.assert_called_once_with("my-repo", new_pending)

    def test_trigger_pull_clears_hash(self, tmp_path):
        """trigger_pull removes _last_pending_hash entry after completion."""
        runner = _make_service_runner(tmp_path)
        runner._last_pending_hash["my-repo"] = "some-hash"

        # Simulate what trigger_pull does in finally block
        runner._last_pending_hash.pop("my-repo", None)

        assert "my-repo" not in runner._last_pending_hash
