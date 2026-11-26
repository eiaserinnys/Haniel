"""Secret references remain in config while consumers receive their values."""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from haniel.cli import main
from haniel.config import load_config
from haniel.config.io import write_config
from haniel.core.handover_config import HandoverConfigError, handover_config_digest
from haniel.core.runner import ServiceRunner
from haniel.installer.finalize import Finalizer
from haniel.installer.mechanical import MechanicalInstaller
from haniel.installer.state import InstallState


@pytest.fixture
def secret_config(tmp_path: Path):
    (tmp_path / "secrets.env").write_text(
        "TOKEN=example-secret-value\nOTHER=other-secret-value\n", encoding="utf-8"
    )
    path = tmp_path / "haniel.yaml"
    path.write_text(
        """secrets_file: secrets.env
webhooks:
  - url: https://example.invalid/${secret:TOKEN}
slack:
  bot_token: ${secret:TOKEN}
  app_token: ${secret:OTHER}
  notify_user: U123
orchestrator_client:
  url: wss://example.invalid/ws
  token: ${secret:TOKEN}
  node_id: test
dashboard:
  token: ${secret:OTHER}
install:
  service:
    name: haniel
    service_account:
      username: user
      password: ${secret:TOKEN}
  configs:
    static:
      path: static.txt
      content: 'value=${secret:TOKEN} ${VAR} $VAR {root}'
    dynamic:
      path: values.env
      keys:
        - key: TOKEN
          default: ${secret:OTHER}
repos:
  app:
    url: https://example.invalid/repo
    path: repo
""",
        encoding="utf-8",
    )
    return path


def test_roundtrip_and_runtime_consumers(secret_config, monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    config = load_config(secret_config)
    assert config.slack.bot_token == "${secret:TOKEN}"
    assert "example-secret-value" not in str(config.model_dump())
    assert "TOKEN" not in os.environ
    write_config(secret_config, config)
    text = secret_config.read_text(encoding="utf-8")
    assert "${secret:TOKEN}" in text and "example-secret-value" not in text
    runner = ServiceRunner(config, secret_config.parent, config_path=secret_config)
    with (
        patch("haniel.integrations.slack_bot.SlackBot") as slack,
        patch("haniel.integrations.orchestrator_client.OrchestratorClient") as orch,
        patch("haniel.integrations.webhook.WebhookNotifier") as webhook,
    ):
        runner._start_slack_bot()
        runner._start_orch_client()
        runner._notify_self_update("SELF_UPDATE_DETECTED", "test")
        assert slack.call_args.kwargs["config"].bot_token == "example-secret-value"
        assert slack.call_args.kwargs["config"].app_token == "other-secret-value"
        assert orch.call_args.kwargs["config"].token == "example-secret-value"
        assert (
            webhook.call_args.args[0][0].url
            == "https://example.invalid/example-secret-value"
        )
    assert (
        config.resolve_secret(config.dashboard.token, "dashboard.token")
        == "other-secret-value"
    )


def test_install_uses_values_without_exposing_state(secret_config):
    config = load_config(secret_config)
    state = InstallState()
    mechanical = MechanicalInstaller(config, secret_config.parent, state)
    mechanical.create_static_configs()
    mechanical.determine_pending_configs()
    finalizer = Finalizer(config, secret_config.parent, state)
    finalizer.generate_config_files()
    assert (
        (secret_config.parent / "static.txt").read_text()
        == f"value=example-secret-value ${{VAR}} $VAR {secret_config.parent.as_posix()}"
    )
    assert (
        "TOKEN=other-secret-value" in (secret_config.parent / "values.env").read_text()
    )
    assert "other-secret-value" not in str(state.model_dump())
    with patch("shutil.which", return_value="python"):
        xml = finalizer._generate_winsw_xml(
            config.install.service, str(secret_config.parent)
        )
    assert "<password>example-secret-value</password>" in xml


def test_runner_conf_receives_resolved_webhook(secret_config):
    from haniel.config import SelfUpdateConfig

    config = load_config(secret_config)
    config.self_update = SelfUpdateConfig(repo="app")
    Finalizer(config, secret_config.parent, InstallState())._generate_runner_conf()
    content = (secret_config.parent / "haniel-runner.conf").read_text()
    assert "WEBHOOK_URL=https://example.invalid/example-secret-value" in content
    assert "${secret:TOKEN}" not in content


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_static_secret_content_is_private(secret_config):
    from haniel.config import ConfigFileConfig

    config = load_config(secret_config)
    target = secret_config.parent / "static.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    plain = secret_config.parent / "plain.txt"
    plain.write_text("old", encoding="utf-8")
    plain.chmod(0o644)
    config.install.configs["plain"] = ConfigFileConfig(
        path="plain.txt", content="plain"
    )
    MechanicalInstaller(
        config, secret_config.parent, InstallState()
    ).create_static_configs()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(plain.stat().st_mode) == 0o644


def test_install_default_error_uses_validation_path(secret_config):
    config = load_config(secret_config)
    config.install.configs["dynamic"].keys[0].default = "${secret:MISSING}"
    finalizer = Finalizer(config, secret_config.parent, InstallState())
    with pytest.raises(
        ValueError, match=r"install\.configs\.dynamic\.keys\.0\.default"
    ):
        finalizer.generate_config_files()


@pytest.mark.parametrize("secrets_file", [None, "missing.env", "secrets.env"])
def test_unresolved_is_safe(secret_config, secrets_file, caplog):
    raw = (
        secret_config.read_text()
        .replace(
            "secrets_file: secrets.env",
            "" if secrets_file is None else f"secrets_file: {secrets_file}",
        )
        .replace("${secret:TOKEN}", "${secret:MISSING}")
    )
    secret_config.write_text(raw)
    result = CliRunner().invoke(main, ["validate", str(secret_config)])
    assert result.exit_code == 1
    assert "webhooks.0.url" in result.output and "MISSING" in result.output
    assert "example-secret-value" not in result.output + caplog.text


def test_no_plain_secrets(secret_config):
    cli = CliRunner()
    assert (
        cli.invoke(
            main, ["validate", "--no-plain-secrets", str(secret_config)]
        ).exit_code
        == 0
    )
    original = secret_config.read_text()
    secret_config.write_text(
        original.replace("bot_token: ${secret:TOKEN}", "bot_token: plaintext")
    )
    result = cli.invoke(main, ["validate", "--no-plain-secrets", str(secret_config)])
    assert result.exit_code == 1 and "slack.bot_token" in result.output
    secret_config.write_text(original + "\n# example-secret-value\n")
    result = cli.invoke(main, ["validate", "--no-plain-secrets", str(secret_config)])
    assert result.exit_code == 1 and "TOKEN" in result.output
    assert "example-secret-value" not in result.output


def test_handover_compares_values(secret_config):
    raw = secret_config.read_text()
    secret_config.write_text(
        raw.replace("${secret:TOKEN}", "example-secret-value").replace(
            "${secret:OTHER}", "other-secret-value"
        )
    )
    runner = ServiceRunner(
        load_config(secret_config), secret_config.parent, config_path=secret_config
    )
    secret_config.write_text(raw)
    runner.prepare_handover_config("app", handover_config_digest(secret_config))
    (secret_config.parent / "secrets.env").write_text(
        "TOKEN=different-secret\nOTHER=other-secret-value\n"
    )
    with pytest.raises(HandoverConfigError, match="CONFIG_RELOAD_UNSAFE"):
        runner.prepare_handover_config("app", handover_config_digest(secret_config))


@pytest.mark.asyncio
async def test_dashboard_auth_and_mcp_config_edit_preserve_references(secret_config):
    from starlette.testclient import TestClient
    from haniel.integrations.mcp_server import HanielMcpServer

    config = load_config(secret_config)
    runner = ServiceRunner(config, secret_config.parent, config_path=secret_config)
    server = HanielMcpServer(runner)
    with (
        patch("uvicorn.Server") as uvicorn_server,
        patch("haniel.core.claude_session.ClaudeSessionManager"),
    ):
        from unittest.mock import AsyncMock

        uvicorn_server.return_value.serve = AsyncMock()
        await server.start()
        app = uvicorn_server.call_args.args[0].app
    client = TestClient(app)
    assert (
        client.get(
            "/api/config", headers={"Authorization": "Bearer other-secret-value"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/config", headers={"Authorization": "Bearer ${secret:OTHER}"}
        ).status_code
        == 403
    )
    result = await server._create_service_config(
        {"name": "test", "config": {"run": "echo test"}}
    )
    assert '"ok": true' in result
    for path in (secret_config, secret_config.with_suffix(".yaml.bak")):
        content = path.read_text()
        assert "${secret:TOKEN}" in content and "example-secret-value" not in content


def test_secret_errors_logs_and_install_output_are_safe(secret_config, caplog):
    config = load_config(secret_config)
    runner = ServiceRunner(config, secret_config.parent, config_path=secret_config)
    with patch(
        "haniel.integrations.slack_bot.SlackBot",
        side_effect=RuntimeError("example-secret-value"),
    ):
        runner._start_slack_bot()
    result = CliRunner().invoke(main, ["install", "--dry-run", str(secret_config)])
    assert result.exit_code == 0
    assert "example-secret-value" not in caplog.text + result.output
    assert "other-secret-value" not in caplog.text + result.output


@pytest.mark.parametrize(
    ("secrets_file", "expected"),
    [
        (None, "no secrets_file is set"),
        ("missing.env", "cannot read {path}"),
        ("secrets.env", "looked in {path}"),
    ],
)
def test_unresolved_secret_names_the_file_it_looked_in(
    secret_config, secrets_file, expected
):
    raw = (
        secret_config.read_text()
        .replace(
            "secrets_file: secrets.env",
            "" if secrets_file is None else f"secrets_file: {secrets_file}",
        )
        .replace("${secret:TOKEN}", "${secret:MISSING}")
    )
    secret_config.write_text(raw)
    with pytest.raises(ValueError) as error:
        load_config(secret_config)
    path = secret_config.parent.resolve() / (secrets_file or "")
    assert "MISSING" in str(error.value)
    assert expected.format(path=path) in str(error.value)


def test_unreadable_secrets_file_error_names_the_path(secret_config):
    secret_config.write_text(
        "secrets_file: missing.env\nservices:\n  a: {run: x}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError) as error:
        load_config(secret_config)
    assert str(secret_config.parent.resolve() / "missing.env") in str(error.value)
