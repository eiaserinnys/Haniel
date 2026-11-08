"""macOS installer contracts: tool discovery, interpreter reuse and LaunchAgent."""

from __future__ import annotations

import logging
import os
import plistlib
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import (
    EnvironmentConfig,
    HanielConfig,
    InstallConfig,
    RepoConfig,
    ServiceAccountConfig,
    ServiceDefinitionConfig,
)
from haniel.installer import utils
from haniel.installer.finalize import Finalizer
from haniel.installer.launchd import (
    generate_launchd_plist,
    register_launchd_service,
)
from haniel.installer.mechanical import MechanicalInstaller
from haniel.installer.state import InstallState

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="macOS installer contracts"
)

_DARWIN_CANDIDATES = {
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
}


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_darwin_directories(monkeypatch, simulated: set[str]) -> None:
    """Make candidate directory existence deterministic on a Linux host."""
    original = Path.is_dir

    def fake_is_dir(self: Path) -> bool:
        text = str(self)
        if text in _DARWIN_CANDIDATES or text in simulated:
            return text in simulated
        return original(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)


def _patch_darwin(monkeypatch, *, home: Path, path_value: str) -> None:
    monkeypatch.setattr(utils.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", path_value)


def _service(**overrides) -> ServiceDefinitionConfig:
    payload: dict[str, object] = {"name": "runner"}
    payload.update(overrides)
    return ServiceDefinitionConfig(**payload)


def _haniel_config(
    *,
    service: ServiceDefinitionConfig | None = None,
    self_update: bool = False,
    repos: dict[str, RepoConfig] | None = None,
) -> HanielConfig:
    payload: dict[str, object] = {
        "repos": {
            "haniel": {
                "url": "https://example.test/haniel.git",
                "branch": "main",
                "path": "./.projects/haniel",
            }
        }
        if repos is None
        else repos,
        "services": {},
    }
    if self_update:
        payload["self"] = {"repo": "haniel"}
    if service is not None:
        payload["install"] = {"service": service.model_dump()}
    return HanielConfig.model_validate(payload)


def _mechanical(config: HanielConfig, config_dir: Path) -> MechanicalInstaller:
    return MechanicalInstaller(config, config_dir, InstallState())


# Tool search path


def test_tool_search_path_apple_silicon_order(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _patch_darwin(monkeypatch, home=home, path_value="/usr/bin:/bin")
    _fake_darwin_directories(
        monkeypatch,
        {"/opt/homebrew/bin", "/usr/bin", "/bin", str(home / ".local" / "bin")},
    )

    entries = utils.get_tool_search_path().split(os.pathsep)

    assert entries == [
        "/usr/bin",
        "/bin",
        "/opt/homebrew/bin",
        str(home / ".local" / "bin"),
    ]


def test_tool_search_path_intel_candidate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _patch_darwin(monkeypatch, home=home, path_value="/usr/bin:/bin")
    _fake_darwin_directories(
        monkeypatch,
        {"/usr/local/bin", "/usr/bin", "/bin", str(home / ".npm-global" / "bin")},
    )

    entries = utils.get_tool_search_path().split(os.pathsep)

    assert "/usr/local/bin" in entries
    assert "/opt/homebrew/bin" not in entries
    assert entries.index("/usr/local/bin") > entries.index("/bin")
    assert str(home / ".npm-global" / "bin") in entries


def test_tool_search_path_skips_missing_candidates(monkeypatch, tmp_path):
    _patch_darwin(monkeypatch, home=tmp_path / "home", path_value="")
    _fake_darwin_directories(monkeypatch, {"/usr/bin"})

    assert utils.get_tool_search_path() == "/usr/bin"


def test_tool_search_path_keeps_existing_first_and_dedupes(monkeypatch, tmp_path):
    _patch_darwin(
        monkeypatch,
        home=tmp_path / "home",
        path_value="/custom/bin:/opt/homebrew/bin",
    )
    _fake_darwin_directories(monkeypatch, {"/custom/bin", "/opt/homebrew/bin"})

    assert utils.get_tool_search_path() == "/custom/bin:/opt/homebrew/bin"


def test_tool_search_path_non_darwin_is_unchanged(monkeypatch):
    monkeypatch.setattr(utils.sys, "platform", "linux")
    monkeypatch.setenv("PATH", "/custom/bin")

    assert utils.get_tool_search_path() == "/custom/bin"


def test_find_tool_finds_user_local_bin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _patch_darwin(monkeypatch, home=home, path_value="/usr/bin")
    _fake_darwin_directories(
        monkeypatch,
        {"/usr/bin", str(home / ".local" / "bin")},
    )
    tool = _make_executable(home / ".local" / "bin" / "haniel-tool")

    assert utils.find_tool("haniel-tool") == str(tool)


def test_find_tool_makes_uv_discoverable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _patch_darwin(monkeypatch, home=home, path_value="")
    _fake_darwin_directories(monkeypatch, {str(home / ".npm-global" / "bin")})
    uv = _make_executable(home / ".npm-global" / "bin" / "uv")

    assert utils.find_tool("uv") == str(uv)


def test_find_tool_returns_none_for_missing_command(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _patch_darwin(monkeypatch, home=home, path_value="")
    _fake_darwin_directories(monkeypatch, {str(home / ".local" / "bin")})
    (home / ".local" / "bin").mkdir(parents=True)

    assert utils.find_tool("haniel-missing-tool") is None


def test_detect_tool_paths_keeps_symlink_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(utils.sys, "platform", "linux")
    real = _make_executable(tmp_path / "real" / "tool")
    launcher_dir = tmp_path / "launcher"
    launcher_dir.mkdir()
    link = launcher_dir / "tool"
    link.symlink_to(real)
    monkeypatch.setenv("PATH", str(launcher_dir))

    assert utils.detect_tool_paths(["tool"]) == [str(launcher_dir)]


# Interpreter reuse on Darwin


def test_darwin_python_requirement_uses_sys_executable(tmp_path):
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python3")
    config = HanielConfig(
        install=InstallConfig(requirements={"python": ">=3.11"}),
    )
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch("haniel.installer.mechanical.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(stdout="Python 3.12.4\n"),
        ) as run,
    ):
        results = installer.check_requirements()

    args, kwargs = run.call_args
    assert args[0] == [str(interpreter), "--version"]
    assert "env" not in kwargs
    python_result = next(item for item in results if item["name"] == "python")
    assert python_result["installed"] is True
    assert python_result["version"] == "3.12.4"


def test_darwin_python_requirement_rejects_missing_interpreter(tmp_path):
    config = HanielConfig(
        install=InstallConfig(requirements={"python": ">=3.11"}),
    )
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch(
            "haniel.installer.mechanical.sys.executable",
            str(tmp_path / "missing-python"),
        ),
        patch("haniel.installer.mechanical.subprocess.run") as run,
    ):
        results = installer.check_requirements()

    run.assert_not_called()
    python_result = next(item for item in results if item["name"] == "python")
    assert python_result["installed"] is False
    assert "not executable" in python_result["error"]


def test_darwin_python_mismatch_hint_uses_brew(tmp_path):
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python3")
    config = HanielConfig(
        install=InstallConfig(requirements={"python": ">=3.11"}),
    )
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch("haniel.installer.mechanical.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(stdout="Python 3.9.6\n"),
        ),
    ):
        results = installer.check_requirements()

    python_result = next(item for item in results if item["name"] == "python")
    assert python_result["installed"] is False
    assert "brew install python" in python_result["message"]


def test_darwin_venv_creation_uses_sys_executable(tmp_path):
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python3")
    config = HanielConfig(
        install=InstallConfig(
            environments={
                "main-venv": EnvironmentConfig(
                    type="python-venv", path="./runtime/venv"
                )
            }
        )
    )
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch("haniel.installer.mechanical.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as run,
    ):
        installer.create_environments()

    args, _kwargs = run.call_args_list[0]
    assert args[0] == [
        str(interpreter),
        "-m",
        "venv",
        str(tmp_path / "runtime" / "venv"),
    ]
    assert "environments" in installer.state.completed_steps


def test_darwin_venv_marks_failed_without_interpreter(tmp_path):
    config = HanielConfig(
        install=InstallConfig(
            environments={
                "main-venv": EnvironmentConfig(
                    type="python-venv", path="./runtime/venv"
                )
            }
        )
    )
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch(
            "haniel.installer.mechanical.sys.executable",
            str(tmp_path / "missing-python"),
        ),
        patch("haniel.installer.mechanical.subprocess.run") as run,
    ):
        installer.create_environments()

    run.assert_not_called()
    assert [step.step for step in installer.state.failed_steps] == [
        "environments:main-venv"
    ]
    assert "not executable" in installer.state.failed_steps[0].error


def test_darwin_node_hint_uses_brew_only(tmp_path):
    config = HanielConfig(install=InstallConfig(requirements={"node": ">=18"}))
    installer = _mechanical(config, tmp_path)

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(stdout="v16.0.0\n"),
        ),
    ):
        results = installer.check_requirements()

    node_result = next(item for item in results if item["name"] == "node")
    assert "brew install node" in node_result["message"]
    assert "apt" not in node_result["message"]
    assert "winget" not in node_result["message"]


def test_node_check_receives_tool_environment(tmp_path):
    config = HanielConfig(install=InstallConfig(requirements={"node": ">=18"}))
    installer = _mechanical(config, tmp_path)
    tool_env = {"PATH": "/enhanced/bin", "KEEP": "1"}

    with (
        patch("haniel.installer.mechanical.platform.system", return_value="Darwin"),
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value=tool_env,
        ),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(stdout="v20.0.0\n"),
        ) as run,
    ):
        installer.check_requirements()

    _args, kwargs = run.call_args
    assert kwargs["env"] == tool_env


def test_env_with_tool_paths_dedupes_detected_directories(tmp_path):
    installer = _mechanical(HanielConfig(), tmp_path)

    with (
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value={"PATH": "/a:/b", "KEEP": "1"},
        ),
        patch(
            "haniel.installer.mechanical.detect_tool_paths",
            return_value=["/b", "/c"],
        ),
    ):
        env = installer._env_with_tool_paths(["node"])

    assert env == {"PATH": "/a:/b:/c", "KEEP": "1"}


def _repo_config() -> HanielConfig:
    return HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.test/app.git",
                branch="main",
                path="./app",
            )
        },
        install=InstallConfig(),
    )


def test_git_clone_and_pull_receive_tool_environment(tmp_path, monkeypatch):
    installer = _mechanical(_repo_config(), tmp_path)
    tool_env = {"PATH": "/enhanced/bin"}

    with (
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value=tool_env,
        ),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        installer.clone_repos()

    clone_args, clone_kwargs = run.call_args
    assert clone_args[0][:2] == ["git", "clone"]
    assert clone_kwargs["env"] == tool_env

    (tmp_path / "app" / ".git").mkdir(parents=True)
    run.reset_mock()
    with (
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value=tool_env,
        ),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as pull_run,
    ):
        installer.clone_repos()

    pull_args, pull_kwargs = pull_run.call_args
    assert pull_args[0][:2] == ["git", "-C"]
    assert "pull" in pull_args[0]
    assert pull_kwargs["env"] == tool_env


def test_deferred_repo_identity_git_calls_receive_tool_environment(tmp_path):
    config = _repo_config()
    installer = _mechanical(config, tmp_path)
    repo_path = tmp_path / "app"
    repo_path.mkdir()
    tool_env = {"PATH": "/enhanced/bin"}

    with (
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value=tool_env,
        ),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            side_effect=[
                MagicMock(returncode=0, stdout="https://example.test/app.git\n"),
                MagicMock(returncode=0, stdout=""),
            ],
        ) as run,
    ):
        installer._validate_deferred_repo_identity(repo_path, config.repos["app"])

    origin_args, origin_kwargs = run.call_args_list[0]
    assert origin_args[0][:4] == ["git", "-C", str(repo_path), "remote"]
    assert origin_kwargs["env"] == tool_env
    branch_args, branch_kwargs = run.call_args_list[1]
    assert branch_args[0][:4] == ["git", "-C", str(repo_path), "ls-remote"]
    assert branch_kwargs["env"] == tool_env


def test_repo_hook_and_build_receive_tool_environment(tmp_path):
    installer = _mechanical(HanielConfig(), tmp_path)

    with (
        patch(
            "haniel.installer.mechanical.get_tool_environment",
            return_value={"PATH": "/enhanced/bin"},
        ),
        patch(
            "haniel.installer.mechanical.detect_tool_paths",
            return_value=[],
        ),
        patch(
            "haniel.installer.mechanical.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as run,
    ):
        installer._run_repo_hook("app", "post_pull", "echo ok", tmp_path)
        installer._run_build("app", tmp_path, "pnpm run build")

    hook_kwargs = run.call_args_list[0].kwargs
    build_kwargs = run.call_args_list[1].kwargs
    assert hook_kwargs["env"]["PATH"] == "/enhanced/bin"
    assert build_kwargs["env"]["PATH"] == "/enhanced/bin"


# LaunchAgent plist


def _plist_setup(tmp_path: Path, *, filename: str = "haniel.yaml") -> Path:
    config_dir = tmp_path / "root"
    config_dir.mkdir()
    (config_dir / filename).write_text("repos: {}\n", encoding="utf-8")
    return config_dir


def test_generate_plist_without_self_update(tmp_path, monkeypatch):
    config = _haniel_config(service=_service())
    config_dir = _plist_setup(tmp_path, filename="han iel & <prod>.yaml")
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python")

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.launchd.get_tool_search_path",
            return_value="/tools/bin",
        ),
    ):
        payload = plistlib.loads(
            generate_launchd_plist(
                config, config_dir, "han iel & <prod>.yaml", config.install.service
            )
        )

    assert payload["Label"] == "com.haniel.runner"
    assert payload["WorkingDirectory"] == str(config_dir)
    assert payload["ProgramArguments"] == [
        str(interpreter),
        "-m",
        "haniel.cli",
        "run",
        str(config_dir / "han iel & <prod>.yaml"),
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ThrottleInterval"] == 5
    assert payload["EnvironmentVariables"] == {"PATH": "/tools/bin"}
    assert payload["StandardOutPath"] == str(
        config_dir / "logs" / "haniel-runner.stdout.log"
    )
    assert payload["StandardErrorPath"] == str(
        config_dir / "logs" / "haniel-runner.stderr.log"
    )
    assert (config_dir / "logs").is_dir()
    assert "UserName" not in payload
    assert "GroupName" not in payload
    assert "AbandonProcessGroup" not in payload


def test_generate_plist_with_self_update(tmp_path):
    config = _haniel_config(
        service=_service(environment={"FOO": "{root}/data", "PATH": "/custom"}),
        self_update=True,
    )
    config_dir = _plist_setup(tmp_path)
    runner = config_dir / ".projects" / "haniel" / "haniel-runner.sh"
    _make_executable(runner)
    helper = runner.parent / "scripts" / "haniel_atomic_release.py"
    _make_executable(helper)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        payload = plistlib.loads(
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )
        )

    assert payload["ProgramArguments"] == [
        "/bin/bash",
        str(runner),
        str(config_dir / "haniel-runner.conf"),
    ]
    assert payload["EnvironmentVariables"] == {
        "FOO": f"{config_dir}/data",
        "PATH": "/custom",
    }


def test_generate_plist_resolves_relative_working_directory(tmp_path):
    config = _haniel_config(service=_service(working_directory="work"))
    config_dir = _plist_setup(tmp_path)
    (config_dir / "work").mkdir()
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python")

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.launchd.get_tool_search_path",
            return_value="/tools/bin",
        ),
    ):
        payload = plistlib.loads(
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )
        )

    assert payload["WorkingDirectory"] == str(config_dir / "work")


def test_generate_plist_rejects_root_install(tmp_path):
    config = _haniel_config(service=_service())
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=0):
        with pytest.raises(RuntimeError, match="normal user"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_rejects_service_account(tmp_path):
    config = _haniel_config(
        service=_service(service_account=ServiceAccountConfig(username="someone"))
    )
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(RuntimeError, match="service_account"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


@pytest.mark.parametrize("name", ["bad name", "-leading", "a/b"])
def test_generate_plist_rejects_invalid_service_name(tmp_path, name):
    config = _haniel_config(service=_service(name=name))
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(ValueError, match="Invalid service name"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_accepts_dotted_service_name(tmp_path):
    config = _haniel_config(service=_service(name="a.b-c_1"))
    config_dir = _plist_setup(tmp_path)
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python")

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.launchd.get_tool_search_path",
            return_value="/tools/bin",
        ),
    ):
        payload = plistlib.loads(
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )
        )

    assert payload["Label"] == "com.haniel.a.b-c_1"


def test_generate_plist_requires_config_file(tmp_path):
    config = _haniel_config(service=_service())
    config_dir = tmp_path / "root"
    config_dir.mkdir()

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(FileNotFoundError, match="configuration"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_requires_working_directory(tmp_path):
    config = _haniel_config(service=_service(working_directory="{root}/missing"))
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(FileNotFoundError, match="Working directory"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_requires_self_update_repo(tmp_path):
    config = HanielConfig.model_validate({"repos": {}, "self": {"repo": "haniel"}})
    config.install = InstallConfig(service=_service())
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(RuntimeError, match="not configured"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_requires_runner_and_helper(tmp_path):
    config = _haniel_config(service=_service(), self_update=True)
    config_dir = _plist_setup(tmp_path)

    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(FileNotFoundError, match="wrapper"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )

    runner = config_dir / ".projects" / "haniel" / "haniel-runner.sh"
    _make_executable(runner)
    with patch("haniel.installer.launchd.os.geteuid", return_value=501):
        with pytest.raises(FileNotFoundError, match="helper"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


def test_generate_plist_requires_executable_interpreter(tmp_path):
    config = _haniel_config(service=_service())
    config_dir = _plist_setup(tmp_path)

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch(
            "haniel.installer.launchd.sys.executable", str(tmp_path / "missing-python")
        ),
    ):
        with pytest.raises(FileNotFoundError, match="interpreter"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )

    not_executable = tmp_path / "python-noexec"
    not_executable.write_text("", encoding="utf-8")
    not_executable.chmod(0o644)
    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(not_executable)),
    ):
        with pytest.raises(RuntimeError, match="not executable"):
            generate_launchd_plist(
                config, config_dir, "haniel.yaml", config.install.service
            )


# LaunchAgent registration


def test_register_launchd_service_installs_plist_without_loading(
    tmp_path, monkeypatch, caplog
):
    config = _haniel_config(service=_service())
    config_dir = _plist_setup(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python")

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.os.getuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.launchd.get_tool_search_path",
            return_value="/tools/bin",
        ),
        patch("subprocess.run", side_effect=AssertionError("launchctl must not run")),
        caplog.at_level(logging.INFO, logger="haniel.installer.launchd"),
    ):
        register_launchd_service(
            config, config_dir, "haniel.yaml", config.install.service
        )

    plist_path = home / "Library" / "LaunchAgents" / "com.haniel.runner.plist"
    assert plist_path.is_file()
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o644
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "com.haniel.runner"
    assert "LaunchAgent file installed; not loaded in this session" in caplog.text
    assert "launchctl bootstrap gui/501" in caplog.text
    assert str(plist_path) in caplog.text
    assert "launchctl print gui/501/com.haniel.runner" in caplog.text
    assert "launchctl kickstart -k gui/501/com.haniel.runner" in caplog.text
    assert "launchctl bootout gui/501/com.haniel.runner" in caplog.text


def test_register_launchd_service_propagates_write_failure(tmp_path, monkeypatch):
    config = _haniel_config(service=_service())
    config_dir = _plist_setup(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interpreter = _make_executable(tmp_path / "venv" / "bin" / "python")

    with (
        patch("haniel.installer.launchd.os.geteuid", return_value=501),
        patch("haniel.installer.launchd.sys.executable", str(interpreter)),
        patch(
            "haniel.installer.launchd.get_tool_search_path",
            return_value="/tools/bin",
        ),
        patch.object(Path, "write_bytes", side_effect=PermissionError("denied")),
    ):
        with pytest.raises(PermissionError):
            register_launchd_service(
                config, config_dir, "haniel.yaml", config.install.service
            )

    assert not (home / "Library" / "LaunchAgents" / "com.haniel.runner.plist").exists()


# Finalizer wiring


def test_register_service_dispatches_darwin_launchagent(tmp_path):
    config = _haniel_config(service=_service(), self_update=True)
    finalizer = Finalizer(config, tmp_path, InstallState())

    with (
        patch("haniel.installer.finalize.platform.system", return_value="Darwin"),
        patch("haniel.installer.finalize.register_launchd_service") as register,
        patch.object(finalizer, "_generate_runner_conf") as runner_conf,
    ):
        finalizer.register_service()

    runner_conf.assert_called_once_with()
    register.assert_called_once_with(
        config, tmp_path, "haniel.yaml", config.install.service
    )


def test_register_service_darwin_without_self_update_skips_runner_conf(tmp_path):
    config = _haniel_config(service=_service())
    finalizer = Finalizer(config, tmp_path, InstallState())

    with (
        patch("haniel.installer.finalize.platform.system", return_value="Darwin"),
        patch("haniel.installer.finalize.register_launchd_service") as register,
        patch.object(finalizer, "_generate_runner_conf") as runner_conf,
    ):
        finalizer.register_service()

    runner_conf.assert_not_called()
    register.assert_called_once_with(
        config, tmp_path, "haniel.yaml", config.install.service
    )


def test_darwin_runner_conf_records_unresolved_interpreter(tmp_path):
    config = _haniel_config(service=_service(), self_update=True)
    finalizer = Finalizer(config, tmp_path, InstallState())
    real = _make_executable(tmp_path / "real" / "python3")
    link_dir = tmp_path / "venv" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "python"
    link.symlink_to(real)

    with (
        patch("haniel.installer.finalize.platform.system", return_value="Darwin"),
        patch("haniel.installer.finalize.sys.executable", str(link)),
    ):
        finalizer._generate_runner_conf()

    lines = (tmp_path / "haniel-runner.conf").read_text(encoding="utf-8").splitlines()
    assert f"PYTHON_BIN={link}" in lines
    assert f"PYTHON_BIN={real}" not in lines


def test_linux_runner_conf_omits_python_bin_and_keeps_base_lines(tmp_path):
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
    finalizer = Finalizer(config, tmp_path, InstallState())

    with patch("haniel.installer.finalize.platform.system", return_value="Linux"):
        finalizer._generate_runner_conf()

    content = (tmp_path / "haniel-runner.conf").read_text(encoding="utf-8")
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
    assert "PYTHON_BIN" not in content
