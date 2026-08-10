"""Tests for service lifecycle management helpers."""

from pathlib import Path
import threading
import time
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
                "ready": "delay:0.01",
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
            service_config={
                "run": "python app.py",
                "ready": "delay:0.01",
                "repo": "main",
            },
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
                service_config={
                    "run": "python app.py",
                    "ready": "delay:0.01",
                    "repo": "main",
                },
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


def test_eight_second_clone_does_not_own_config_write_transaction(tmp_path: Path):
    from haniel.core.service_lifecycle import register_service

    config = HanielConfig(
        poll_interval=60,
        services={"existing": ServiceConfig(run="python old.py", enabled=False)},
        repos={},
    )
    runner = _runner(config, tmp_path)
    clone_entered = threading.Event()
    errors: list[BaseException] = []

    def slow_clone(**_kwargs):
        clone_entered.set()
        time.sleep(8)

    def register() -> None:
        try:
            register_service(
                runner,
                name="new",
                service_config={
                    "run": "python new.py",
                    "ready": "delay:0.01",
                    "repo": "main",
                    "enabled": False,
                },
                repo="main",
                repo_config={
                    "url": "git@example.invalid/repo.git",
                    "path": "./repo",
                },
            )
        except BaseException as error:
            errors.append(error)

    with patch("haniel.core.service_lifecycle.clone_repo", side_effect=slow_clone):
        worker = threading.Thread(target=register)
        worker.start()
        assert clone_entered.wait(1)
        started = time.monotonic()
        runner.reload_config()
        reload_elapsed = time.monotonic() - started
        worker.join(timeout=10)

    assert reload_elapsed < 2
    assert "existing" in runner.config.services
    assert "new" in runner.config.services
    assert errors == []


def test_reload_service_definition_restarts_only_the_target_service(tmp_path: Path):
    from haniel.core.service_lifecycle import reload_service_definition

    old_config = HanielConfig(
        poll_interval=60,
        services={
            "web": ServiceConfig(run="python old.py", ready="delay:0.01"),
            "worker": ServiceConfig(
                run="python worker.py", ready="delay:0.01", after=["web"]
            ),
        },
        repos={},
    )
    runner = _runner(old_config, tmp_path)
    new_config = HanielConfig(
        poll_interval=60,
        services={
            "web": ServiceConfig(run="python new.py", ready="delay:0.01"),
            "worker": ServiceConfig(
                run="python worker.py", ready="delay:0.01", after=["web"]
            ),
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
