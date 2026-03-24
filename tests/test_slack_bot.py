"""
Tests for SlackBot integration and trigger_pull unification.

Covers:
- SlackBotConfig parsing
- SlackBot.notify_pending (posts DM, replaces old message)
- SlackBot.notify_pulling / notify_done
- runner.trigger_pull() dispatches to SlackBot and ws_handler
- _detect_changes calls notify_pending only on L726 branch, with pull lock guard
- _apply_changes uses trigger_pull(auto=True)
- api.py repo_pull delegates to trigger_pull
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from haniel.config.model import HanielConfig, SlackBotConfig, load_config
from haniel.integrations.slack_bot import SlackBot


# ── SlackBotConfig parsing ────────────────────────────────────────────────────


def test_slack_bot_config_parsed(tmp_path: Path):
    """SlackBotConfig is loaded correctly from YAML."""
    yaml_content = """\
poll_interval: 60
repos: {}
services: {}
slack:
  enabled: true
  bot_token: "xoxb-test-token"
  app_token: "xapp-test-token"
  notify_user: "U12345"
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    cfg = load_config(config_file)

    assert cfg.slack is not None
    assert cfg.slack.enabled is True
    assert cfg.slack.bot_token == "xoxb-test-token"
    assert cfg.slack.app_token == "xapp-test-token"
    assert cfg.slack.notify_user == "U12345"


def test_slack_bot_config_absent(tmp_path: Path):
    """HanielConfig.slack defaults to None when not specified."""
    yaml_content = "poll_interval: 60\nrepos: {}\nservices: {}\n"
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    cfg = load_config(config_file)
    assert cfg.slack is None


def test_slack_bot_config_disabled(tmp_path: Path):
    """SlackBotConfig.enabled=false is parsed correctly."""
    yaml_content = """\
poll_interval: 60
repos: {}
services: {}
slack:
  enabled: false
  bot_token: "xoxb-test"
  app_token: "xapp-test"
  notify_user: "U99"
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    cfg = load_config(config_file)
    assert cfg.slack is not None
    assert cfg.slack.enabled is False


# ── SlackBot unit tests ───────────────────────────────────────────────────────


def _make_slack_config(**kwargs):
    defaults = {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "notify_user": "U12345",
    }
    defaults.update(kwargs)
    return SlackBotConfig(**defaults)


@pytest.fixture
def mock_web_client():
    """Return a MagicMock that mimics WebClient behavior."""
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_TEST_CHANNEL"}}
    client.chat_postMessage.return_value = {"ts": "1234567890.000001"}
    client.chat_delete.return_value = {"ok": True}
    return client


@pytest.fixture
def slack_bot(mock_web_client):
    """SlackBot with mocked Bolt App, SocketModeHandler, and WebClient.

    App(token=...) calls auth.test at construction time, so we patch it out to
    avoid real network calls in unit tests.
    """
    config = _make_slack_config()
    with (
        patch("haniel.integrations.slack_bot.App"),
        patch("haniel.integrations.slack_bot.SocketModeHandler"),
    ):
        bot = SlackBot(config)
    bot._client = mock_web_client
    bot._dm_channel = "D_TEST_CHANNEL"
    return bot


def test_notify_pending_posts_message(slack_bot, mock_web_client):
    """notify_pending posts a Block Kit message and stores the ts."""
    pending = {"commits": ["abc123 fix: bug"], "stat": "1 file changed"}
    slack_bot.notify_pending("my-repo", pending)

    mock_web_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_web_client.chat_postMessage.call_args[1]
    assert call_kwargs["channel"] == "D_TEST_CHANNEL"
    assert slack_bot._pending_ts.get("my-repo") == "1234567890.000001"


def test_notify_pending_deletes_previous_message(slack_bot, mock_web_client):
    """notify_pending deletes the old DM before posting a new one."""
    # Seed an existing ts
    slack_bot._pending_ts["my-repo"] = "OLD_TS"
    mock_web_client.chat_postMessage.return_value = {"ts": "NEW_TS"}

    pending = {"commits": ["def456 feat: new thing"], "stat": ""}
    slack_bot.notify_pending("my-repo", pending)

    mock_web_client.chat_delete.assert_called_once_with(
        channel="D_TEST_CHANNEL", ts="OLD_TS"
    )
    assert slack_bot._pending_ts.get("my-repo") == "NEW_TS"


def test_notify_pulling_removes_pending_ts(slack_bot, mock_web_client):
    """notify_pulling deletes the pending DM and sends a 'pulling...' message."""
    slack_bot._pending_ts["my-repo"] = "PENDING_TS"

    slack_bot.notify_pulling("my-repo", auto=False)

    mock_web_client.chat_delete.assert_called_once_with(
        channel="D_TEST_CHANNEL", ts="PENDING_TS"
    )
    assert "my-repo" not in slack_bot._pending_ts


def test_notify_pulling_auto_sets_pulling_ts(slack_bot, mock_web_client):
    """notify_pulling(auto=True) sends '자동 배포 시작' message and stores pulling ts."""
    mock_web_client.chat_postMessage.return_value = {"ts": "PULLING_TS"}

    slack_bot.notify_pulling("my-repo", auto=True)

    posted_text = mock_web_client.chat_postMessage.call_args[1]["text"]
    assert "자동 배포" in posted_text
    assert slack_bot._pulling_ts.get("my-repo") == "PULLING_TS"


def test_notify_done_success_posts_and_clears(slack_bot, mock_web_client):
    """notify_done(success=True) deletes the pulling DM and posts a success message."""
    slack_bot._pulling_ts["my-repo"] = "PULLING_TS"

    slack_bot.notify_done("my-repo", success=True)

    mock_web_client.chat_delete.assert_called_once_with(
        channel="D_TEST_CHANNEL", ts="PULLING_TS"
    )
    assert "my-repo" not in slack_bot._pulling_ts
    posted = mock_web_client.chat_postMessage.call_args[1]
    assert "완료" in posted["text"]


def test_notify_done_failure_includes_error(slack_bot, mock_web_client):
    """notify_done(success=False) posts error text."""
    slack_bot.notify_done("my-repo", success=False, error="git pull failed")

    posted = mock_web_client.chat_postMessage.call_args[1]
    assert "실패" in posted["text"]


def test_build_pending_blocks_has_approve_button(slack_bot):
    """_build_pending_blocks includes an action block with approve_update action."""
    pending = {"commits": ["abc fix"], "stat": "1 file changed, 2 insertions"}
    blocks = slack_bot._build_pending_blocks("test-repo", pending)

    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(action_blocks) == 1
    elements = action_blocks[0]["elements"]
    assert len(elements) == 1
    assert elements[0]["action_id"] == "approve_update"
    assert elements[0]["value"] == "test-repo"


def test_build_pending_blocks_truncates_commits(slack_bot):
    """_build_pending_blocks shows at most 10 commits."""
    commits = [f"sha{i} message {i}" for i in range(15)]
    pending = {"commits": commits, "stat": ""}
    blocks = slack_bot._build_pending_blocks("repo", pending)

    section_texts = [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section"
    ]
    combined = " ".join(section_texts)
    assert "외 5개" in combined


def test_build_pending_blocks_truncates_long_stat(slack_bot):
    """stat이 3000자를 초과하면 truncate되어야 한다."""
    long_stat = "file.py | 10 +-\n" * 200  # ~3200자
    pending = {"commits": ["abc fix"], "stat": long_stat}
    blocks = slack_bot._build_pending_blocks("repo", pending)

    stat_blocks = [
        b for b in blocks
        if b.get("type") == "section"
        and "변경 통계" in b["text"]["text"]
    ]
    assert len(stat_blocks) == 1
    assert len(stat_blocks[0]["text"]["text"]) <= 3000
    assert "생략" in stat_blocks[0]["text"]["text"]


def test_notify_done_success_with_pending_changes(slack_bot, mock_web_client):
    """notify_done(success=True)에 pending_changes를 넘기면 커밋 목록과 stat이 표시된다."""
    pending = {
        "commits": ["abc123 fix: bug", "def456 feat: new"],
        "stat": "2 files changed",
    }
    slack_bot.notify_done("my-repo", success=True, pending_changes=pending)

    posted = mock_web_client.chat_postMessage.call_args[1]
    blocks_text = str(posted.get("blocks", ""))
    assert "abc123" in blocks_text or "fix: bug" in blocks_text


def test_notify_done_success_without_pending_changes(slack_bot, mock_web_client):
    """notify_done(success=True, pending_changes=None)이면 기존처럼 완료 메시지만 표시."""
    slack_bot.notify_done("my-repo", success=True, pending_changes=None)

    posted = mock_web_client.chat_postMessage.call_args[1]
    assert "완료" in posted["text"]


def test_start_opens_dm_channel(mock_web_client):
    """start() opens a DM channel with notify_user and starts Socket Mode thread."""
    config = _make_slack_config(notify_user="U99999")
    with (
        patch("haniel.integrations.slack_bot.App"),
        patch("haniel.integrations.slack_bot.SocketModeHandler") as MockHandler,
    ):
        mock_handler_instance = MagicMock()
        MockHandler.return_value = mock_handler_instance
        bot = SlackBot(config)
        bot._client = mock_web_client

        bot.start()

    mock_web_client.conversations_open.assert_called_once_with(users="U99999")
    assert bot._dm_channel == "D_TEST_CHANNEL"
    # Socket Mode thread should have been started
    assert bot._socket_thread is not None
    assert bot._socket_thread.daemon is True


# ── Phase 2: approve button interaction ──────────────────────────────────────


def test_approve_action_spawns_thread():
    """approve_update action handler acks and spawns a daemon thread for trigger_pull."""
    config = _make_slack_config()
    approve_called = threading.Event()
    approved_repo = []

    def fake_approve(repo_name):
        approved_repo.append(repo_name)
        approve_called.set()

    with (
        patch("haniel.integrations.slack_bot.App") as MockApp,
        patch("haniel.integrations.slack_bot.SocketModeHandler"),
    ):
        mock_app_instance = MagicMock()
        registered_handlers = {}

        def mock_action(action_id):
            def decorator(fn):
                registered_handlers[action_id] = fn
                return fn
            return decorator

        mock_app_instance.action = mock_action
        MockApp.return_value = mock_app_instance

        bot = SlackBot(config, approve_callback=fake_approve)

    handler = registered_handlers.get("approve_update")
    assert handler is not None, "approve_update handler not registered"

    ack_mock = MagicMock()
    action = {"value": "my-repo"}
    handler(ack=ack_mock, body={}, action=action)

    ack_mock.assert_called_once()
    # Wait for the spawned thread to complete
    assert approve_called.wait(timeout=2), "approve_callback was not called"
    assert approved_repo == ["my-repo"]


def test_approve_action_no_callback_does_not_raise():
    """approve_update without approve_callback logs a warning and does nothing."""
    config = _make_slack_config()

    with (
        patch("haniel.integrations.slack_bot.App") as MockApp,
        patch("haniel.integrations.slack_bot.SocketModeHandler"),
    ):
        mock_app_instance = MagicMock()
        registered_handlers = {}

        def mock_action(action_id):
            def decorator(fn):
                registered_handlers[action_id] = fn
                return fn
            return decorator

        mock_app_instance.action = mock_action
        MockApp.return_value = mock_app_instance

        bot = SlackBot(config, approve_callback=None)

    handler = registered_handlers["approve_update"]
    ack_mock = MagicMock()
    # Should not raise even with no callback
    handler(ack=ack_mock, body={}, action={"value": "repo"})
    ack_mock.assert_called_once()


# ── runner.trigger_pull() ─────────────────────────────────────────────────────


@pytest.fixture
def mock_runner(tmp_path: Path):
    """ServiceRunner with mocked git ops and a minimal config."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  my-repo:
    url: git@github.com:test/repo.git
    branch: main
    path: ./my-repo
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "my-repo").mkdir()

    with (
        patch("haniel.core.runner.get_head", return_value="abc1234"),
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.pull_repo"),
        patch("haniel.core.runner.get_remote_head", return_value="abc1234"),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)

    return runner


def test_trigger_pull_calls_slack_notify(mock_runner):
    """trigger_pull notifies SlackBot of pulling and done states."""
    slack_bot = MagicMock()
    mock_runner._slack_bot = slack_bot
    mock_runner._repo_states["my-repo"].pending_changes = {"commits": ["a"], "stat": ""}

    with patch.object(mock_runner, "_pull_repo", return_value=True):
        mock_runner.trigger_pull("my-repo", auto=False)

    slack_bot.notify_pulling.assert_called_once_with("my-repo", auto=False)
    slack_bot.notify_done.assert_called_once_with(
        "my-repo", success=True, pending_changes={"commits": ["a"], "stat": ""}
    )


def test_trigger_pull_broadcasts_ws_pulling(mock_runner):
    """trigger_pull broadcasts repo_pulling True then False to ws_handler."""
    ws = MagicMock()
    mock_runner._ws_handler = ws
    mock_runner._repo_states["my-repo"].pending_changes = {"commits": ["a"], "stat": ""}

    with patch.object(mock_runner, "_pull_repo", return_value=True):
        mock_runner.trigger_pull("my-repo")

    assert ws.broadcast_repo_pulling.call_count == 2
    calls = ws.broadcast_repo_pulling.call_args_list
    assert calls[0] == call("my-repo", True)
    assert calls[1] == call("my-repo", False)


def test_trigger_pull_releases_lock_on_failure(mock_runner):
    """trigger_pull releases the pull lock even when pull fails."""
    slack_bot = MagicMock()
    mock_runner._slack_bot = slack_bot
    mock_runner._repo_states["my-repo"].pending_changes = {"commits": ["a"], "stat": ""}

    with patch.object(mock_runner, "_pull_repo", side_effect=RuntimeError("oops")):
        with pytest.raises(RuntimeError):
            mock_runner.trigger_pull("my-repo")

    assert not mock_runner._pull_locks["my-repo"].locked()
    slack_bot.notify_done.assert_called_once()
    _, kwargs = slack_bot.notify_done.call_args
    assert kwargs["success"] is False
    assert "pending_changes" in kwargs  # captured_changes 전달 확인


def test_trigger_pull_unknown_repo_raises(mock_runner):
    """trigger_pull raises ValueError for unknown repo names."""
    with pytest.raises(ValueError, match="Unknown repo"):
        mock_runner.trigger_pull("nonexistent-repo")


def test_trigger_pull_ignores_duplicate_while_pulling(mock_runner):
    """trigger_pull is a no-op if pull lock is already held (duplicate approve guard)."""
    mock_runner._pull_locks["my-repo"].acquire()  # simulate ongoing pull
    slack_bot = MagicMock()
    mock_runner._slack_bot = slack_bot

    with patch.object(mock_runner, "_pull_repo") as mock_pull:
        mock_runner.trigger_pull("my-repo")

    mock_pull.assert_not_called()
    slack_bot.notify_pulling.assert_not_called()
    # Clean up: release the lock we manually acquired
    mock_runner._pull_locks["my-repo"].release()


def test_trigger_pull_skips_when_no_pending_changes(mock_runner):
    """trigger_pull skips pull when pending_changes is None (re-entry guard)."""
    mock_runner._repo_states["my-repo"].pending_changes = None
    slack_bot = MagicMock()
    mock_runner._slack_bot = slack_bot

    with patch.object(mock_runner, "_pull_repo") as mock_pull:
        mock_runner.trigger_pull("my-repo")

    mock_pull.assert_not_called()
    slack_bot.notify_pulling.assert_not_called()
    # Lock should have been released
    assert not mock_runner._pull_locks["my-repo"].locked()


# ── _detect_changes notify_pending guard ────────────────────────────────────


def test_detect_changes_notify_pending_on_remote_new(tmp_path: Path):
    """notify_pending is called when remote has new commits (L726 branch)."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  my-repo:
    url: git@github.com:test/repo.git
    branch: main
    path: ./my-repo
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "my-repo").mkdir()

    slack_bot = MagicMock()
    pending = {"commits": ["sha1 msg"], "stat": ""}

    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="CURRENT"),
        patch("haniel.core.runner.get_remote_head", return_value="REMOTE_NEW"),
        patch("haniel.core.runner.get_pending_changes", return_value=pending),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)
        runner._repo_states["my-repo"].last_head = "CURRENT"  # no external pull
        runner._slack_bot = slack_bot

        runner._detect_changes()

    slack_bot.notify_pending.assert_called_once_with("my-repo", pending)


def test_detect_changes_no_notify_when_pulling(tmp_path: Path):
    """notify_pending is NOT called when pull lock is held."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  my-repo:
    url: git@github.com:test/repo.git
    branch: main
    path: ./my-repo
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "my-repo").mkdir()

    slack_bot = MagicMock()
    pending = {"commits": ["sha1 msg"], "stat": ""}

    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="CURRENT"),
        patch("haniel.core.runner.get_remote_head", return_value="REMOTE_NEW"),
        patch("haniel.core.runner.get_pending_changes", return_value=pending),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)
        runner._repo_states["my-repo"].last_head = "CURRENT"
        runner._pull_locks["my-repo"].acquire()  # simulate pull in progress
        runner._slack_bot = slack_bot

        runner._detect_changes()

    slack_bot.notify_pending.assert_not_called()
    # Clean up
    runner._pull_locks["my-repo"].release()


def test_detect_changes_no_notify_on_external_pull(tmp_path: Path):
    """notify_pending is NOT called when local HEAD was advanced externally (L708)."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  my-repo:
    url: git@github.com:test/repo.git
    branch: main
    path: ./my-repo
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "my-repo").mkdir()

    slack_bot = MagicMock()
    pending = {"commits": ["sha1 msg"], "stat": ""}

    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="NEW_HEAD"),  # externally pulled
        patch("haniel.core.runner.get_pending_changes", return_value=pending),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)
        runner._repo_states["my-repo"].last_head = "OLD_HEAD"  # differs → L708 branch
        runner._slack_bot = slack_bot

        runner._detect_changes()

    # L708 branch: already pulled externally — no Slack notification
    slack_bot.notify_pending.assert_not_called()


def test_detect_changes_self_repo_external_pull_notifies_pending(tmp_path: Path):
    """notify_pending IS called when self-repo HEAD advances externally (needs restart)."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  haniel:
    url: https://github.com/eiaserinnys/Haniel.git
    branch: main
    path: ./haniel-src
self:
  repo: haniel
  auto_update: false
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "haniel-src").mkdir()

    slack_bot = MagicMock()
    pending = {"commits": ["abc fix"], "stat": ""}

    with (
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.get_head", return_value="NEW_HEAD"),
        patch("haniel.core.runner.get_pending_changes", return_value=pending),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)
        runner._repo_states["haniel"].last_head = "OLD_HEAD"  # externally pulled
        runner._slack_bot = slack_bot

        runner._detect_changes()

    # Self-repo Case 1: code already pulled but Haniel needs restart → notify
    slack_bot.notify_pending.assert_called_once_with("haniel", pending)


def test_trigger_pull_self_repo_signals_restart(tmp_path: Path):
    """trigger_pull for self-repo signals self-update restart after pull."""
    from haniel.config.model import load_config
    from haniel.core.runner import ServiceRunner

    yaml_content = """\
poll_interval: 60
repos:
  haniel:
    url: https://github.com/eiaserinnys/Haniel.git
    branch: main
    path: ./haniel-src
self:
  repo: haniel
  auto_update: false
services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(yaml_content)
    (tmp_path / "haniel-src").mkdir()

    with (
        patch("haniel.core.runner.get_head", return_value="abc1234"),
        patch("haniel.core.runner.fetch_repo"),
        patch("haniel.core.runner.pull_repo"),
        patch("haniel.core.runner.get_remote_head", return_value="abc1234"),
    ):
        runner = ServiceRunner(load_config(config_file), config_dir=tmp_path)

    slack_bot = MagicMock()
    runner._slack_bot = slack_bot
    runner._repo_states["haniel"].pending_changes = {"commits": ["a"], "stat": ""}

    with (
        patch.object(runner, "_pull_repo", return_value=True),
        patch.object(runner, "stop") as mock_stop,
    ):
        runner.trigger_pull("haniel", auto=False)

    # After self-repo pull, restart should be signalled
    assert runner._self_update_requested.is_set()
    mock_stop.assert_called_once()
