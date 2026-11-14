"""Startup service lanes keep one repository's build from blocking other services."""

import threading
from pathlib import Path
from unittest.mock import patch

from haniel.config import HanielConfig, HooksConfig, RepoConfig, ServiceConfig
from haniel.core.legacy_build_marker import read_completed_commits
from haniel.core.runner import ServiceRunner


def _repo_paths(tmp_path: Path, *names: str) -> None:
    for name in names:
        repo_path = tmp_path / name
        repo_path.mkdir()
        (repo_path / ".git").mkdir()


def _runner(config: HanielConfig, tmp_path: Path) -> ServiceRunner:
    return ServiceRunner(config, config_dir=tmp_path)


def _record_start(name: str, started: list[str], lock: threading.Lock) -> bool:
    with lock:
        started.append(name)
    return True


def test_unjupdated_repo_and_repo_less_services_start_while_updated_repo_build_is_blocked(
    tmp_path: Path,
) -> None:
    """갱신된 A의 post_pull이 막혀 있어도 갱신 안 된 B와 리포 없는 서비스는 먼저 뜬다."""
    _repo_paths(tmp_path, "repo-a", "repo-b")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-b": RepoConfig(url="git@test/b", path="./repo-b"),
        },
        services={
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                hooks=HooksConfig(post_pull="echo build"),
            ),
            "b": ServiceConfig(run="echo b", repo="repo-b"),
            "standalone": ServiceConfig(run="echo standalone"),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a"}

    hook_entered = threading.Event()
    allow_hook = threading.Event()
    b_started = threading.Event()
    standalone_started = threading.Event()
    a_started = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def execute_hook(service_name: str, hook_name: str) -> bool:
        if (service_name, hook_name) == ("a", "post_pull"):
            hook_entered.set()
            assert allow_hook.wait(timeout=10)
        return True

    def start_service(name: str) -> bool:
        result = _record_start(name, started, lock)
        if name == "b":
            b_started.set()
        elif name == "standalone":
            standalone_started.set()
        elif name == "a":
            a_started.set()
        return result

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", side_effect=start_service),
        patch("haniel.core.runner.sync_disk"),
    ):
        thread = threading.Thread(target=runner.start_services)
        thread.start()
        assert hook_entered.wait(timeout=5)
        assert b_started.wait(timeout=5)
        assert standalone_started.wait(timeout=5)
        assert not a_started.is_set()
        allow_hook.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert started == ["b", "standalone", "a"]


def test_two_updated_legacy_repos_build_and_start_independently(
    tmp_path: Path,
) -> None:
    """A의 post_pull이 막혀 있어도 B의 post_pull과 B의 서비스 시작은 끝난다."""
    _repo_paths(tmp_path, "repo-a", "repo-b")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-b": RepoConfig(url="git@test/b", path="./repo-b"),
        },
        services={
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                hooks=HooksConfig(post_pull="echo build"),
            ),
            "b": ServiceConfig(
                run="echo b",
                repo="repo-b",
                hooks=HooksConfig(post_pull="echo build"),
            ),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a", "repo-b"}

    hook_entered = threading.Event()
    allow_hook = threading.Event()
    b_started = threading.Event()
    a_started = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def execute_hook(service_name: str, hook_name: str) -> bool:
        if (service_name, hook_name) == ("a", "post_pull"):
            hook_entered.set()
            assert allow_hook.wait(timeout=10)
        return True

    def start_service(name: str) -> bool:
        result = _record_start(name, started, lock)
        if name == "b":
            b_started.set()
        else:
            a_started.set()
        return result

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", side_effect=start_service),
        patch("haniel.core.runner.sync_disk"),
    ):
        thread = threading.Thread(target=runner.start_services)
        thread.start()
        assert hook_entered.wait(timeout=5)
        assert b_started.wait(timeout=5)
        assert not a_started.is_set()
        allow_hook.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert started == ["b", "a"]


def test_service_depending_on_updated_repo_service_waits_for_that_service(
    tmp_path: Path,
) -> None:
    """갱신 안 된 C의 서비스가 갱신된 A의 서비스에 after로 의존하면 A 뒤에 시작한다."""
    _repo_paths(tmp_path, "repo-a", "repo-c")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-c": RepoConfig(url="git@test/c", path="./repo-c"),
        },
        services={
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                hooks=HooksConfig(post_pull="echo build"),
            ),
            "c": ServiceConfig(run="echo c", repo="repo-c", after=["a"]),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a"}

    hook_entered = threading.Event()
    allow_hook = threading.Event()
    c_started = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def execute_hook(service_name: str, hook_name: str) -> bool:
        if (service_name, hook_name) == ("a", "post_pull"):
            hook_entered.set()
            assert allow_hook.wait(timeout=10)
        return True

    def start_service(name: str) -> bool:
        if name == "c":
            c_started.set()
        return _record_start(name, started, lock)

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", side_effect=start_service),
        patch("haniel.core.runner.sync_disk"),
    ):
        thread = threading.Thread(target=runner.start_services)
        thread.start()
        assert hook_entered.wait(timeout=5)
        assert not c_started.is_set()
        allow_hook.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert started == ["a", "c"]


def test_unchanged_prerequisite_starts_without_waiting_for_dependent_lanes(
    tmp_path: Path,
) -> None:
    """갱신 안 된 U를 A·B가 after로 기대면 U가 먼저 뜨고 A·B는 서로 다른 갈래로 끝난다."""
    _repo_paths(tmp_path, "repo-a", "repo-b", "repo-c")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-b": RepoConfig(url="git@test/b", path="./repo-b"),
            "repo-c": RepoConfig(url="git@test/c", path="./repo-c"),
        },
        services={
            "u": ServiceConfig(run="echo u", repo="repo-c"),
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                after=["u"],
                hooks=HooksConfig(post_pull="echo build"),
            ),
            "b": ServiceConfig(
                run="echo b",
                repo="repo-b",
                after=["u"],
                hooks=HooksConfig(post_pull="echo build"),
            ),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a", "repo-b"}

    a_hook_entered = threading.Event()
    allow_a_hook = threading.Event()
    b_hook_entered = threading.Event()
    allow_b_hook = threading.Event()
    u_started = threading.Event()
    b_started = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def execute_hook(service_name: str, hook_name: str) -> bool:
        if (service_name, hook_name) == ("a", "post_pull"):
            a_hook_entered.set()
            assert allow_a_hook.wait(timeout=10)
        elif (service_name, hook_name) == ("b", "post_pull"):
            b_hook_entered.set()
            assert allow_b_hook.wait(timeout=10)
        return True

    def start_service(name: str) -> bool:
        result = _record_start(name, started, lock)
        if name == "u":
            u_started.set()
        elif name == "b":
            b_started.set()
        return result

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", side_effect=start_service),
        patch("haniel.core.runner.sync_disk"),
    ):
        thread = threading.Thread(target=runner.start_services)
        thread.start()
        assert a_hook_entered.wait(timeout=5)
        assert b_hook_entered.wait(timeout=5)
        # 의존 대상 U는 두 갈래의 post_pull이 막혀 있는 동안 먼저 시작된다.
        assert u_started.wait(timeout=5)
        allow_b_hook.set()
        # A를 막아 둔 채 B의 갈래만 자기 서비스까지 시작한다.
        assert b_started.wait(timeout=5)
        assert "a" not in started
        allow_a_hook.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert started == ["u", "b", "a"]


def test_manifest_deployment_does_not_wait_for_other_repo_post_pull(
    tmp_path: Path,
) -> None:
    """manifest 배포는 다른 갱신 리포의 post_pull 완료를 기다리지 않는다."""
    _repo_paths(tmp_path, "soulstream", "repo-a")
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest="deploy/release-manifest.json",
            ),
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream"),
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                hooks=HooksConfig(post_pull="echo build"),
            ),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a"}
    runner._startup_manifest_updates = {"soulstream": "old-head"}

    hook_entered = threading.Event()
    allow_hook = threading.Event()
    deploy_entered = threading.Event()

    def execute_hook(service_name: str, hook_name: str) -> bool:
        if (service_name, hook_name) == ("a", "post_pull"):
            hook_entered.set()
            assert allow_hook.wait(timeout=10)
        return True

    def deploy(*_args, **_kwargs) -> None:
        deploy_entered.set()

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", return_value=True),
        patch("haniel.core.runner.run_manifest_deployment", side_effect=deploy),
        patch("haniel.core.runner.sync_disk"),
    ):
        thread = threading.Thread(target=runner.start_services)
        thread.start()
        assert hook_entered.wait(timeout=5)
        assert deploy_entered.wait(timeout=5)
        allow_hook.set()
        thread.join(timeout=10)

    assert not thread.is_alive()


def test_concurrent_lanes_keep_both_completion_records(tmp_path: Path) -> None:
    """두 갈래가 동시에 완료 기록을 남겨도 마커 파일에 두 기록이 모두 남는다."""
    _repo_paths(tmp_path, "repo-a", "repo-b")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-b": RepoConfig(url="git@test/b", path="./repo-b"),
        },
        services={
            "a": ServiceConfig(
                run="echo a",
                repo="repo-a",
                hooks=HooksConfig(post_pull="echo build"),
            ),
            "b": ServiceConfig(
                run="echo b",
                repo="repo-b",
                hooks=HooksConfig(post_pull="echo build"),
            ),
        },
    )
    runner = _runner(config, tmp_path)
    runner._startup_updated_repos = {"repo-a", "repo-b"}

    both_hooks = threading.Barrier(2)

    def execute_hook(_service_name: str, _hook_name: str) -> bool:
        both_hooks.wait(timeout=10)
        return True

    with (
        patch.object(runner, "execute_hook", side_effect=execute_hook),
        patch.object(runner, "_start_service", return_value=True),
        patch("haniel.core.runner.sync_disk"),
        patch(
            "haniel.core.runner.get_head",
            side_effect=lambda path: f"head-{Path(path).name}",
        ),
    ):
        runner.start_services()

    assert read_completed_commits(tmp_path) == {
        "repo-a": "head-repo-a",
        "repo-b": "head-repo-b",
    }


def test_startup_fetch_runs_concurrently_once_per_repo_and_retries_failures(
    tmp_path: Path,
) -> None:
    """미리 받기는 여러 리포가 함께 돌고, 리포마다 한 번만, 예외 난 리포만 다시 받는다."""
    _repo_paths(tmp_path, "repo-a", "repo-b", "repo-c", "haniel-repo")
    config = HanielConfig(
        repos={
            "repo-a": RepoConfig(url="git@test/a", path="./repo-a"),
            "repo-b": RepoConfig(url="git@test/b", path="./repo-b"),
            "repo-c": RepoConfig(url="git@test/c", path="./repo-c"),
            "haniel-repo": RepoConfig(url="git@test/haniel", path="./haniel-repo"),
        },
        services={},
        **{"self": {"repo": "haniel-repo"}},
    )
    runner = _runner(config, tmp_path)

    calls: list[str] = []
    call_lock = threading.Lock()
    two_in_flight = threading.Event()
    active = 0
    max_active = 0
    failed_once: set[str] = set()

    def fake_fetch(*, path: Path, branch: str, timeout: int) -> bool:
        nonlocal active, max_active
        name = path.name
        with call_lock:
            calls.append(name)
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                two_in_flight.set()
        try:
            if name in {"repo-a", "repo-b"}:
                assert two_in_flight.wait(timeout=5)
            if name == "repo-c" and name not in failed_once:
                failed_once.add(name)
                raise RuntimeError("transient fetch failure")
            return False
        finally:
            with call_lock:
                active -= 1

    with patch("haniel.core.runner.fetch_repo", side_effect=fake_fetch):
        runner._apply_startup_updates()

    assert max_active >= 2
    assert sorted(calls) == ["repo-a", "repo-b", "repo-c", "repo-c"]
    assert "haniel-repo" not in calls
