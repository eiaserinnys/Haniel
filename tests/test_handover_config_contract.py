"""Manifest handover config identity and service env-file safety contracts."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.core.deployment_command_runner import CommandSpec, subprocess_command_runner
from haniel.core.handover_result import HandoverResult
from haniel.core.lifecycle_control import LifecycleConflict, LifecycleControl
from haniel.core.lifecycle_request_server import LifecycleRequestServer
from haniel.core.one_shot_handover import (
    execute_manifest_handover_once,
    execute_owner_handover,
)
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.runner import ServiceRunner
from haniel.core.service_environment import read_service_environment_file


def _write_config(
    path: Path,
    *,
    env_file: Path,
    port: int = 4105,
    service_name: str = "app",
    repo_url: str = "https://example.invalid/app.git",
) -> None:
    path.write_text(
        "\n".join(
            [
                "poll_interval: 60",
                "repos:",
                "  app:",
                f"    url: {repo_url}",
                "    branch: main",
                "    path: ./repo",
                "    release_manifest: deploy/release.json",
                "services:",
                f"  {service_name}:",
                "    run: node app.js",
                "    cwd: ./repo",
                "    repo: app",
                f"    ready: port:{port}",
                f"    release_env_file: {json.dumps(str(env_file))}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _terminal(request_id: str) -> dict[str, object]:
    return HandoverResult(
        schema_version="haniel.handover.result.v1",
        ok=True,
        request_id=request_id,
        release_id="release-1",
        operation="upgrade",
        phase="success",
        previous_head="a" * 40,
        target_head="b" * 40,
        journal_path=None,
        backup_path=None,
        recovered=False,
        retryable=False,
        error=None,
    ).to_dict()


def test_service_config_preserves_release_env_file() -> None:
    service = ServiceConfig(
        run="node app.js",
        repo="app",
        release_env_file="../shared/app.env",
    )

    assert service.release_env_file == "../shared/app.env"


def test_service_config_rejects_second_run_command_env_file() -> None:
    with pytest.raises(ValueError, match="SERVICE_ENV_SOURCE_CONFLICT"):
        ServiceConfig(
            run="node --env-file=old.env server.js",
            release_env_file="new.env",
        )


def test_release_manifest_can_require_service_env_file() -> None:
    manifest = ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "release-1",
            "environment_service": "app",
            "requires_service_env_file": True,
            "post_start_verify": [{"name": "health", "command": "health"}],
            "recovery": {
                "strategy": "rollback",
                "command": {"name": "restore", "command": "restore"},
            },
        }
    )

    assert manifest.requires_service_env_file is True


def test_packaged_cross_repo_contract_matches_public_fields() -> None:
    contract = json.loads(
        files("haniel")
        .joinpath("contracts/manifest-handover-config-environment.v1.json")
        .read_text(encoding="utf-8")
    )

    assert contract["schema_version"] == "haniel.handover.config-environment.v1"
    assert contract["config_content_digest"] == {
        "algorithm": "sha256",
        "request_field": "config_digest",
        "result_field": "config_digest",
        "deployment_journal_field": "config_digest",
        "canonical_paths": True,
        "referenced_env_content_bound": True,
    }
    assert contract["service_environment"] == {
        "service_config_field": "release_env_file",
        "manifest_required_field": "requires_service_env_file",
        "child_path_env": "HANIEL_SERVICE_ENV_FILE",
        "child_digest_env": "HANIEL_SERVICE_ENV_FILE_SHA256",
        "ambient_database_variables_removed": True,
        "runtime_consumes_declared_file": True,
        "request_snapshot_used_through_recovery": True,
    }
    assert set(contract["stable_error_codes"]) == {
        "CONFIG_DIGEST_REQUIRED",
        "CONFIG_DIGEST_MISMATCH",
        "CONFIG_RELOAD_FAILED",
        "CONFIG_RELOAD_UNSAFE",
        "SERVICE_ENV_FILE_REQUIRED",
        "SERVICE_ENV_FILE_INVALID",
        "SERVICE_ENV_FILE_CHANGED",
    }


def test_digest_is_semantic_and_binds_release_env_content(tmp_path: Path) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=postgres://new-secret@db/new\n", encoding="utf-8")
    config = tmp_path / "haniel.yaml"
    _write_config(config, env_file=env_file)

    first = module.handover_config_digest(config)
    original = config.read_text(encoding="utf-8")
    config.write_text(original.replace("\n", "\r\n"), encoding="utf-8")
    assert module.handover_config_digest(config) == first

    sections = original.split("services:\n", maxsplit=1)
    config.write_text("services:\n" + sections[1] + sections[0], encoding="utf-8")
    assert module.handover_config_digest(config) == first

    env_file.write_text(
        "DATABASE_URL=postgres://other-secret@db/other\r\n", encoding="utf-8"
    )
    second = module.handover_config_digest(config)

    assert first != second
    assert len(first) == len(second) == 64
    assert "secret" not in first
    assert "secret" not in second


def test_reload_applies_digest_bound_port_and_env_settings(tmp_path: Path) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    old_env = tmp_path / "old.env"
    new_env = tmp_path / "new.env"
    old_env.write_text("DATABASE_URL=sqlite:///old\n", encoding="utf-8")
    new_env.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=old_env,
        port=4105,
        repo_url="https://example.invalid/old.git",
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    _write_config(
        config_path,
        env_file=new_env,
        port=5105,
        repo_url="https://example.invalid/old.git",
    )

    digest = module.handover_config_digest(config_path)
    plan = runner.prepare_handover_config("app", digest)

    assert plan.config_digest == digest
    assert runner._repo_states["app"].config.url == "https://example.invalid/old.git"
    assert runner._enabled_services["app"].ready == "port:5105"
    assert runner._enabled_services["app"].release_env_file == str(new_env)


def test_release_child_ignores_ambient_database_and_uses_declared_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "service.env"
    old_database = tmp_path / "old.sqlite"
    new_database = tmp_path / "new.sqlite"
    env_file.write_text(f"DATABASE_URL={new_database}\n", encoding="utf-8")
    script = tmp_path / "assert_env.py"
    script.write_text(
        """
import json
import os
import sqlite3
from pathlib import Path

if "DATABASE_URL" in os.environ:
    raise SystemExit(21)
path = Path(os.environ["HANIEL_SERVICE_ENV_FILE"])
database = Path(path.read_text(encoding="utf-8").split("=", 1)[1].strip())
with sqlite3.connect(database) as connection:
    connection.execute("create table release_ledger (id text primary key)")
    connection.execute("insert into release_ledger values ('expected')")
print(json.dumps({"ok": True, "path_matches": path.name == "service.env"}))
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", str(old_database))
    runner = subprocess_command_runner(tmp_path)

    result = runner(
        CommandSpec(
            name="verify-env-source", command=f"{sys.executable} {script.name}"
        ),
        {
            "HANIEL_SERVICE_ENV_FILE": str(env_file.resolve()),
            "HANIEL_SERVICE_ENV_FILE_SHA256": read_service_environment_file(
                env_file
            ).sha256,
        },
    )

    assert result is not None
    assert result.json_data == {"ok": True, "path_matches": True}
    assert not old_database.exists()
    with sqlite3.connect(new_database) as connection:
        assert connection.execute("select id from release_ledger").fetchall() == [
            ("expected",)
        ]


def test_release_child_rejects_env_file_changed_after_identity_binding(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///first\n", encoding="utf-8")
    expected = read_service_environment_file(env_file).sha256
    env_file.write_text("DATABASE_URL=sqlite:///changed\n", encoding="utf-8")
    runner = subprocess_command_runner(tmp_path)

    with pytest.raises(RuntimeError, match="SERVICE_ENV_FILE_CHANGED"):
        runner(
            CommandSpec(name="must-not-run", command=f"{sys.executable} -V"),
            {
                "HANIEL_SERVICE_ENV_FILE": str(env_file),
                "HANIEL_SERVICE_ENV_FILE_SHA256": expected,
            },
        )


@pytest.mark.parametrize(("mode", "raises"), [("success", False), ("failure", True)])
def test_release_child_redacts_bare_secrets_loaded_from_declared_env_file(
    tmp_path: Path,
    mode: str,
    raises: bool,
) -> None:
    secret = "bare-release-secret-value"
    env_file = tmp_path / "service.env"
    env_file.write_text(f"DATABASE_PASSWORD={secret}\n", encoding="utf-8")
    script = tmp_path / "leak.py"
    script.write_text(
        """
import json
import os
import sys
from pathlib import Path

line = Path(os.environ["HANIEL_SERVICE_ENV_FILE"]).read_text(encoding="utf-8")
secret = line.split("=", 1)[1].strip()
if sys.argv[1] == "failure":
    print(secret, file=sys.stderr)
    raise SystemExit(9)
print(json.dumps({"ok": True, "value": secret}))
""".lstrip(),
        encoding="utf-8",
    )
    environment = {
        "HANIEL_SERVICE_ENV_FILE": str(env_file),
        "HANIEL_SERVICE_ENV_FILE_SHA256": read_service_environment_file(
            env_file
        ).sha256,
    }
    runner = subprocess_command_runner(tmp_path)

    if raises:
        with pytest.raises(RuntimeError) as raised:
            runner(
                CommandSpec(
                    name="redact-env-secret",
                    command=f"{sys.executable} {script.name} {mode}",
                ),
                environment,
            )
        serialized = str(raised.value)
    else:
        result = runner(
            CommandSpec(
                name="redact-env-secret",
                command=f"{sys.executable} {script.name} {mode}",
            ),
            environment,
        )
        serialized = json.dumps({"stdout": result.stdout, "json": result.json_data})

    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_public_handover_request_binds_current_config_digest(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=postgres://new@isolated/test\n", encoding="utf-8")
    config = tmp_path / "haniel.yaml"
    _write_config(config, env_file=env_file)
    captured: dict[str, object] = {}
    control = MagicMock()
    control.read_active_owner.return_value = {"instance_id": "owner-1"}

    def submit(request_id: str, payload: dict[str, object]) -> None:
        captured.update(payload)

    control.submit_request.side_effect = submit
    with (
        patch("haniel.core.one_shot_handover.LifecycleControl", return_value=control),
        patch(
            "haniel.core.one_shot_handover._wait_for_terminal",
            return_value={"terminal": _terminal("request-1")},
        ),
    ):
        execute_manifest_handover_once(
            config,
            "app",
            "origin/main",
            "upgrade",
            "request-1",
            False,
            1.0,
        )

    assert captured["config_digest"]
    assert len(str(captured["config_digest"])) == 64


def test_owner_reload_happens_before_ack_or_release_side_effect(tmp_path: Path) -> None:
    config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={"app": ServiceConfig(run="node app.js", repo="app")},
    )
    runner = ServiceRunner(config, tmp_path, config_path=tmp_path / "haniel.yaml")
    events: list[str] = []
    reload_plan = MagicMock(quiesce_services=("app",))
    runner.prepare_handover_config = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("reload") or reload_plan
    )
    control = MagicMock()
    lease = MagicMock(attached=False)
    lease.__enter__.return_value = lease
    lease.__exit__.return_value = None
    control.acquire_deployment.return_value = lease
    control.read_result.return_value = {"acks": []}
    control.ack.side_effect = lambda *_args, **_kwargs: events.append("ack")

    with pytest.raises(Exception):
        execute_owner_handover(
            runner,
            control=control,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            config_digest="a" * 64,
        )

    assert events[:2] == ["reload", "ack"]


def test_reload_plan_quiesces_old_and_new_affected_services(tmp_path: Path) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=postgres://new@isolated/test\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="old-writer")
    old_config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={"old-writer": ServiceConfig(run="old", repo="app")},
    )
    runner = ServiceRunner(old_config, tmp_path, config_path=config_path)
    digest = module.handover_config_digest(config_path)

    plan = runner.prepare_handover_config("app", digest)

    assert plan.old_affected == ("old-writer",)
    assert plan.new_affected == ("old-writer",)
    assert plan.quiesce_services == ("old-writer",)


def test_changed_config_digest_conflicts_without_replacing_request(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=postgres://first@db/one\n", encoding="utf-8")
    config = tmp_path / "haniel.yaml"
    _write_config(config, env_file=env_file)
    control = LifecycleControl(config)
    first_digest = module.handover_config_digest(config)
    first_payload = {
        "kind": "handover",
        "repo": "app",
        "target_ref": "origin/main",
        "expected_operation": "upgrade",
        "config_digest": first_digest,
    }
    submission = control.submit_request("request-1", first_payload)
    before = submission.path.read_bytes()

    env_file.write_text("DATABASE_URL=postgres://second@db/two\n", encoding="utf-8")
    changed_payload = {
        **first_payload,
        "config_digest": module.handover_config_digest(config),
    }
    with pytest.raises(LifecycleConflict, match="REQUEST_IDENTITY_CONFLICT"):
        control.submit_request("request-1", changed_payload)

    assert submission.path.read_bytes() == before
    assert b"first@db" not in before
    assert b"second@db" not in before


def test_manifest_request_without_digest_fails_before_runner_side_effect(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    config.write_text("repos: {}\nservices: {}\n", encoding="utf-8")
    control = LifecycleControl(config)
    control.submit_request(
        "request-no-digest",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
        },
    )
    runner = MagicMock()
    server = LifecycleRequestServer(
        control=control,
        runner=runner,
        instance_id="owner-1",
        poll_interval=0.01,
    )

    server.process_pending_once()

    terminal = control.read_result("request-no-digest")["terminal"]
    assert terminal["error"]["code"] == "CONFIG_DIGEST_REQUIRED"
    runner.prepare_handover_config.assert_not_called()
