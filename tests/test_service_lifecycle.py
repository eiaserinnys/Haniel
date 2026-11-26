"""Tests for service lifecycle management helpers."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.git import GitCloneError
from haniel.core.runner import ServiceRunner


def _write_config(path: Path, config: HanielConfig) -> None:
    data = config.model_dump(by_alias=True, exclude_none=True, mode="python")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


def _read_raw(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _runner(config: HanielConfig, tmp_path: Path) -> ServiceRunner:
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, config)
    return ServiceRunner(config, config_dir=tmp_path, config_path=config_path)


def test_register_service_clones_repo_runs_initial_hook_and_starts(tmp_path: Path):
    from haniel.core.service_lifecycle import register_service

    runner = _runner(HanielConfig(poll_interval=60, services={}, repos={}), tmp_path)
    runner.execute_hook = MagicMock(return_value=True)
    runner._start_service = MagicMock(return_value=True)

    with patch("haniel.core.service_lifecycle.clone_repo") as clone_repo:
        result = register_service(
            runner,
            name="web",
            service_config={
                "run": "python app.py",
                "repo": "main",
                "hooks": {"post_pull": "echo build"},
            },
            repo="main",
            repo_config={
                "url": "git@github.com:test/web.git",
                "branch": "main",
                "path": "",
            },
        )

    assert result["ok"] is True
    assert result["service"] == "web"
    assert result["repo"] == "main"
    assert result["repo_path"] == "./main"
    assert result["clone"] == "created"
    assert result["post_pull"] == "executed"
    assert result["started"] is True
    clone_repo.assert_called_once()
    assert clone_repo.call_args.kwargs["path"] == tmp_path / "main"
    runner.execute_hook.assert_called_once_with("web", "post_pull")
    runner._start_service.assert_called_once_with("web")

    raw = _read_raw(runner.config_path)
    assert raw["repos"]["main"]["path"] == "./main"
    assert "web" in raw["services"]


def test_register_service_reuses_existing_clone_without_cloning(tmp_path: Path):
    from haniel.core.service_lifecycle import register_service

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    config = HanielConfig(
        poll_interval=60,
        services={},
        repos={
            "main": RepoConfig(
                url="git@github.com:test/repo.git",
                path="./repo",
            )
        },
    )
    runner = _runner(config, tmp_path)
    runner.execute_hook = MagicMock(return_value=True)
    runner._start_service = MagicMock(return_value=True)

    with patch("haniel.core.service_lifecycle.clone_repo") as clone_repo:
        result = register_service(
            runner,
            name="web",
            service_config={"run": "python app.py", "repo": "main"},
        )

    assert result["clone"] == "existing"
    clone_repo.assert_not_called()
    runner._start_service.assert_called_once_with("web")


def test_register_repo_updates_config_and_reloads_runner(tmp_path: Path):
    from haniel.core.service_lifecycle import register_repo

    runner = _runner(HanielConfig(poll_interval=60, services={}, repos={}), tmp_path)
    runner.reload_config = MagicMock()

    result = register_repo(
        runner,
        name="main",
        repo_config={
            "url": "git@github.com:test/repo.git",
            "branch": "main",
            "path": "./repo",
        },
    )

    assert result == {"ok": True, "repo": "main"}
    runner.reload_config.assert_called_once()
    raw = _read_raw(runner.config_path)
    assert raw["repos"]["main"]["url"] == "git@github.com:test/repo.git"


def test_register_repo_rejects_existing_repo(tmp_path: Path):
    from haniel.core.service_lifecycle import register_repo

    config = HanielConfig(
        poll_interval=60,
        services={},
        repos={
            "main": RepoConfig(
                url="git@github.com:test/repo.git",
                path="./repo",
            )
        },
    )
    runner = _runner(config, tmp_path)

    with pytest.raises(ValueError, match="Repo already exists"):
        register_repo(
            runner,
            name="main",
            repo_config={
                "url": "git@github.com:test/repo-2.git",
                "path": "./repo-2",
            },
        )


def test_register_service_rolls_back_config_and_partial_clone_on_clone_failure(
    tmp_path: Path,
):
    from haniel.core.service_lifecycle import register_service

    runner = _runner(HanielConfig(poll_interval=60, services={}, repos={}), tmp_path)
    partial_path = tmp_path / "broken"

    def fail_clone(**kwargs):
        partial_path.mkdir()
        raise GitCloneError("boom", url=kwargs["url"])

    with patch("haniel.core.service_lifecycle.clone_repo", side_effect=fail_clone):
        with pytest.raises(GitCloneError):
            register_service(
                runner,
                name="web",
                service_config={"run": "python app.py", "repo": "main"},
                repo="main",
                repo_config={
                    "url": "git@github.com:test/web.git",
                    "branch": "main",
                    "path": "./broken",
                },
            )

    raw = _read_raw(runner.config_path)
    assert raw["services"] == {}
    assert raw["repos"] == {}
    assert not partial_path.exists()


def test_reload_service_definition_restarts_only_the_target_service(tmp_path: Path):
    from haniel.core.service_lifecycle import reload_service_definition

    old_config = HanielConfig(
        poll_interval=60,
        services={
            "web": ServiceConfig(run="python old.py"),
            "worker": ServiceConfig(run="python worker.py", after=["web"]),
        },
        repos={},
    )
    runner = _runner(old_config, tmp_path)
    new_config = HanielConfig(
        poll_interval=60,
        services={
            "web": ServiceConfig(run="python new.py"),
            "worker": ServiceConfig(run="python worker.py", after=["web"]),
        },
        repos={},
    )
    _write_config(runner.config_path, new_config)

    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.process_manager.stop_service = MagicMock(return_value=True)
    runner._start_service = MagicMock(return_value=True)

    result = reload_service_definition(runner, "web")

    assert result["ok"] is True
    assert result["service"] == "web"
    assert result["restarted"] is True
    assert runner.config.services["web"].run == "python new.py"
    assert set(runner._dependency_graph.get_dependents("web")) == {"worker"}
    runner.process_manager.stop_service.assert_called_once_with("web")
    runner._start_service.assert_called_once_with("web")


def test_reload_service_definition_stops_disabled_service_without_start(
    tmp_path: Path,
):
    from haniel.core.service_lifecycle import reload_service_definition

    old_config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python old.py")},
        repos={},
    )
    runner = _runner(old_config, tmp_path)
    new_config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python new.py", enabled=False)},
        repos={},
    )
    _write_config(runner.config_path, new_config)

    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.process_manager.stop_service = MagicMock(return_value=True)
    runner._start_service = MagicMock(return_value=True)

    result = reload_service_definition(runner, "web")

    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["restarted"] is False
    assert result["stopped"] is True
    runner.process_manager.stop_service.assert_called_once_with("web")
    runner._start_service.assert_not_called()


def test_disable_service_updates_config_and_stops_running_process(tmp_path: Path):
    from haniel.core.service_lifecycle import disable_service

    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py")},
        repos={},
    )
    runner = _runner(config, tmp_path)
    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.process_manager.stop_service = MagicMock(return_value=True)

    result = disable_service(runner, "web")

    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["stopped"] is True
    runner.process_manager.stop_service.assert_called_once_with("web")
    raw = _read_raw(runner.config_path)
    assert raw["services"]["web"]["enabled"] is False
    assert "web" not in runner._enabled_services


def test_enable_service_undoes_disable_resets_circuit_and_starts(tmp_path: Path):
    from haniel.core.service_lifecycle import disable_service, enable_service

    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py")},
        repos={},
    )
    runner = _runner(config, tmp_path)
    runner.process_manager.is_running = MagicMock(return_value=False)
    runner.health_manager.reset_circuit = MagicMock()
    runner._start_service = MagicMock(return_value=True)

    disable_service(runner, "web")
    assert "web" not in runner._enabled_services

    result = enable_service(runner, "web")

    assert result == {
        "ok": True,
        "service": "web",
        "enabled": True,
        "was_enabled": False,
        "circuit_reset": True,
        "started": True,
    }
    assert _read_raw(runner.config_path)["services"]["web"]["enabled"] is True
    assert "web" in runner._enabled_services
    runner.health_manager.reset_circuit.assert_called_once_with("web")
    runner._start_service.assert_called_once_with("web")


def test_enable_service_already_enabled_leaves_config_and_running_service(
    tmp_path: Path,
):
    from haniel.core.service_lifecycle import enable_service

    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py")},
        repos={},
    )
    runner = _runner(config, tmp_path)
    before = runner.config_path.read_bytes()
    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.health_manager.reset_circuit = MagicMock()
    runner._start_service = MagicMock()

    result = enable_service(runner, "web")

    assert result["was_enabled"] is True
    assert result["circuit_reset"] is False
    assert result["started"] is False
    runner.health_manager.reset_circuit.assert_not_called()
    assert runner.config_path.read_bytes() == before
    runner._start_service.assert_not_called()


def test_enable_service_unknown_name_is_an_error(tmp_path: Path):
    from haniel.core.service_lifecycle import enable_service

    runner = _runner(HanielConfig(poll_interval=60, services={}, repos={}), tmp_path)
    runner.health_manager.reset_circuit = MagicMock()

    with pytest.raises(KeyError, match="Service not found: nope"):
        enable_service(runner, "nope")
    runner.health_manager.reset_circuit.assert_not_called()


def test_reset_service_circuit_leaves_a_running_service_alone(tmp_path: Path):
    from haniel.core.service_lifecycle import reset_service_circuit

    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py")},
        repos={},
    )
    runner = _runner(config, tmp_path)
    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.health_manager.reset_circuit = MagicMock()

    assert reset_service_circuit(runner, "web")["circuit_reset"] is False
    runner.health_manager.reset_circuit.assert_not_called()


def test_reset_service_circuit_only_resets_known_services(tmp_path: Path):
    from haniel.core.service_lifecycle import reset_service_circuit

    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py", enabled=False)},
        repos={},
    )
    runner = _runner(config, tmp_path)
    runner.health_manager.reset_circuit = MagicMock()
    runner._start_service = MagicMock()

    runner.process_manager.is_running = MagicMock(return_value=False)

    assert reset_service_circuit(runner, "web")["circuit_reset"] is True
    runner.health_manager.reset_circuit.assert_called_once_with("web")
    runner._start_service.assert_not_called()
    assert _read_raw(runner.config_path)["services"]["web"]["enabled"] is False

    with pytest.raises(KeyError, match="Service not found: nope"):
        reset_service_circuit(runner, "nope")


def test_delete_service_config_stops_and_purges_unshared_repo_path(tmp_path: Path):
    from haniel.core.service_lifecycle import delete_service_config

    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    (repo_path / "README.md").write_text("temporary clone", encoding="utf-8")
    config = HanielConfig(
        poll_interval=60,
        services={"web": ServiceConfig(run="python app.py", repo="main")},
        repos={
            "main": RepoConfig(
                url="git@github.com:test/repo.git",
                path="./repo",
            )
        },
    )
    runner = _runner(config, tmp_path)
    runner.process_manager.is_running = MagicMock(return_value=True)
    runner.process_manager.stop_service = MagicMock(return_value=True)

    result = delete_service_config(runner, "web", purge=True)

    assert result["ok"] is True
    assert result["purged"] is True
    assert not repo_path.exists()
    runner.process_manager.stop_service.assert_called_once_with("web")
    raw = _read_raw(runner.config_path)
    assert "web" not in raw["services"]
    assert "main" in raw["repos"]


def _write_raw_config(path: Path, text: str) -> ServiceRunner:
    path.write_text(text, encoding="utf-8")
    from haniel.config import load_config

    return ServiceRunner(load_config(path), config_dir=path.parent, config_path=path)


class TestSaveRejectsInputErrors:
    """Saving an entry refuses unknown keys and bad values in that entry only."""

    def test_create_service_rejects_unknown_key_and_writes_nothing(
        self, tmp_path: Path
    ):
        from haniel.core.service_lifecycle import create_service_config

        runner = _runner(HanielConfig(poll_interval=60), tmp_path)
        before = runner.config_path.read_text(encoding="utf-8")

        with pytest.raises(ValueError, match=r"unknown key services\.api\.redy"):
            create_service_config(
                runner, "api", {"run": "python app.py", "redy": "port:80"}
            )

        assert runner.config_path.read_text(encoding="utf-8") == before
        assert not runner.config_path.with_suffix(".yaml.bak").exists()

    def test_update_service_rejects_bad_ready_and_keeps_old_entry(self, tmp_path: Path):
        from haniel.core.service_lifecycle import update_service_config

        runner = _runner(
            HanielConfig(
                poll_interval=60, services={"api": ServiceConfig(run="python app.py")}
            ),
            tmp_path,
        )

        with pytest.raises(ValueError, match=r"services\.api\.ready"):
            update_service_config(
                runner, "api", {"run": "python app.py", "ready": "prt:80"}
            )

        assert (
            _read_raw(runner.config_path)["services"]["api"]["run"] == "python app.py"
        )
        assert "ready" not in _read_raw(runner.config_path)["services"]["api"]

    def test_reports_every_problem_in_the_entry(self, tmp_path: Path):
        from haniel.core.service_lifecycle import create_service_config

        runner = _runner(HanielConfig(poll_interval=60), tmp_path)

        with pytest.raises(ValueError) as error:
            create_service_config(
                runner,
                "api",
                {"run": "x", "redy": "port:80", "restart_delay": -5},
            )

        assert "unknown key services.api.redy" in str(error.value)
        assert "services.api.restart_delay" in str(error.value)

    def test_repo_save_rejects_unknown_key_and_empty_url(self, tmp_path: Path):
        from haniel.core.service_lifecycle import create_repo_config, register_repo

        runner = _runner(HanielConfig(poll_interval=60), tmp_path)

        with pytest.raises(ValueError, match=r"unknown key repos\.r\.brnch"):
            create_repo_config(runner, "r", {"url": "u", "path": "./r", "brnch": "x"})
        with pytest.raises(ValueError, match=r"repos\.r\.url must not be empty"):
            register_repo(runner, name="r", repo_config={"url": "", "path": "./r"})

    def test_register_service_rejects_unknown_key_before_cloning(self, tmp_path: Path):
        from haniel.core.service_lifecycle import register_service

        runner = _runner(HanielConfig(poll_interval=60), tmp_path)

        with patch("haniel.core.service_lifecycle.clone_repo") as clone_repo:
            with pytest.raises(ValueError, match=r"unknown key repos\.main\.brnch"):
                register_service(
                    runner,
                    name="web",
                    service_config={"run": "python app.py"},
                    repo="main",
                    repo_config={"url": "u", "path": "", "brnch": "main"},
                )
            with pytest.raises(ValueError, match=r"unknown key services\.web\.redy"):
                register_service(
                    runner,
                    name="web",
                    service_config={"run": "python app.py", "redy": "port:80"},
                )

        clone_repo.assert_not_called()

    def test_empty_path_is_still_filled_before_the_repo_is_checked(
        self, tmp_path: Path
    ):
        from haniel.core.service_lifecycle import register_service

        runner = _runner(HanielConfig(poll_interval=60), tmp_path)
        runner.execute_hook = MagicMock(return_value=True)
        runner._start_service = MagicMock(return_value=True)

        with patch("haniel.core.service_lifecycle.clone_repo"):
            result = register_service(
                runner,
                name="web",
                service_config={"run": "python app.py"},
                repo="main",
                repo_config={"url": "u", "path": ""},
            )

        assert result["repo_path"] == "./main"

    def test_old_problem_in_another_entry_does_not_block_the_save(self, tmp_path: Path):
        from haniel.core.service_lifecycle import update_service_config

        runner = _write_raw_config(
            tmp_path / "haniel.yaml",
            "services:\n"
            "  old:\n    run: x\n    redy: 'port:80'\n    ready: 'prt:80'\n"
            "  api:\n    run: y\n",
        )

        update_service_config(runner, "api", {"run": "python app.py"})

        assert (
            _read_raw(runner.config_path)["services"]["api"]["run"] == "python app.py"
        )


def test_reload_config_warns_about_input_errors_and_still_reloads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    runner = _write_raw_config(
        tmp_path / "haniel.yaml",
        "services:\n  api:\n    run: x\n    redy: 'port:80'\n    ready: 'prt:80'\n",
    )

    with caplog.at_level("WARNING", logger="haniel.config.input_checks"):
        runner.reload_config()

    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown key services.api.redy" in m for m in messages)
    assert any("services.api.ready" in m for m in messages)
    assert "api" in runner._enabled_services
