"""create_app() turns ORCH_PUSH_RELAY_URL / ORCH_PUSH_INSTANCE_KEY into the push service."""

import logging

import pytest

from haniel_orch import server
from haniel_orch.push import NullPushService, RelayPushService

INSTANCE_KEY = "inst-key-SECRET-VALUE-1234"
RELAY_URL = "https://relay.example.com"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Build the app through create_app() and return the push service it chose."""
    monkeypatch.setenv("TOKEN", "test-node-token")
    monkeypatch.setenv("AUTH_BEARER_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "orchestrator.db"))
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path / "missing-dashboard"))
    monkeypatch.delenv("ORCH_PUSH_RELAY_URL", raising=False)
    monkeypatch.delenv("ORCH_PUSH_INSTANCE_KEY", raising=False)

    servers: list[server.OrchestratorServer] = []
    real_init = server.OrchestratorServer.__init__

    def recording_init(self, config):
        real_init(self, config)
        servers.append(self)

    monkeypatch.setattr(server.OrchestratorServer, "__init__", recording_init)

    def build(url=None, key=None):
        if url is not None:
            monkeypatch.setenv("ORCH_PUSH_RELAY_URL", url)
        if key is not None:
            monkeypatch.setenv("ORCH_PUSH_INSTANCE_KEY", key)
        server.create_app()
        return servers[-1]._push

    return build


def test_both_set_uses_relay(captured, caplog):
    with caplog.at_level(logging.INFO, logger="haniel_orch.server"):
        push = captured(RELAY_URL, INSTANCE_KEY)

    assert isinstance(push, RelayPushService)
    assert any(
        RELAY_URL in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    )
    assert INSTANCE_KEY not in caplog.text


def test_neither_set_uses_null(captured, caplog):
    with caplog.at_level(logging.INFO, logger="haniel_orch.server"):
        push = captured()

    assert isinstance(push, NullPushService)
    assert "disabled" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_blank_values_count_as_unset(captured, caplog):
    with caplog.at_level(logging.INFO, logger="haniel_orch.server"):
        push = captured("  ", "")

    assert isinstance(push, NullPushService)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_url_only_warns_about_missing_key(captured, caplog):
    with caplog.at_level(logging.INFO, logger="haniel_orch.server"):
        push = captured(url=RELAY_URL)

    assert isinstance(push, NullPushService)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ORCH_PUSH_INSTANCE_KEY" in warnings[0]


def test_key_only_warns_about_missing_url_and_hides_key(captured, caplog):
    with caplog.at_level(logging.DEBUG):
        push = captured(key=INSTANCE_KEY)

    assert isinstance(push, NullPushService)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ORCH_PUSH_RELAY_URL" in warnings[0]
    assert INSTANCE_KEY not in caplog.text


@pytest.mark.parametrize(
    "url,key",
    [(RELAY_URL, INSTANCE_KEY), (RELAY_URL, None), (None, INSTANCE_KEY), (None, None)],
)
def test_instance_key_never_logged(captured, caplog, url, key):
    with caplog.at_level(logging.DEBUG):
        captured(url, key)

    assert INSTANCE_KEY not in caplog.text


async def test_relay_posts_to_url_without_trailing_slash(captured, monkeypatch):
    push = captured(RELAY_URL + "/", INSTANCE_KEY)
    assert isinstance(push, RelayPushService)

    sent = {}

    class FakeResponse:
        is_success = True

        def json(self):
            return {"sent": 1}

    async def fake_post(url, headers, json):
        sent.update(url=url, headers=headers)
        return FakeResponse()

    monkeypatch.setattr(push._client, "post", fake_post)
    await push.notify("t", "b", {})
    await push.close()

    assert sent["url"] == f"{RELAY_URL}/v1/push"
    assert sent["headers"] == {"Authorization": f"Bearer {INSTANCE_KEY}"}
