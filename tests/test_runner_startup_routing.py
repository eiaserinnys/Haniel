"""Startup and approved pulls activate one release-manifest control path."""

import threading
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from haniel.core.runner import ServiceRunner
from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.config.release_activation import DEFAULT_RELEASE_MANIFEST
from haniel.core.deployment import DeploymentError
from haniel.core.deployment import DeploymentStateStore
from haniel.core.lifecycle_control import LifecycleConflict
from haniel.core.one_shot_handover import execute_owner_handover
from haniel.core.handover_config import handover_config_digest


def staged_release(target_head: str = "new-head") -> SimpleNamespace:
    return SimpleNamespace(
        target_head=target_head,
        manifest_digest="manifest-digest",
        manifest=SimpleNamespace(release_id="release-1"),
    )


def config_text() -> str:
    return (
        "repos:\n"
        "  soulstream:\n"
        "    url: git@github.com:test/soulstream.git\n"
        "    path: ./soulstream\n"
        "services:\n"
        "  soulstream-orch-server:\n"
        "    run: orch\n"
        "    repo: soulstream\n"
    )


def make_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "soulstream"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()


@patch("haniel.core.runner.run_manifest_deployment")
def test_start_services_routes_manifest_repo_once_in_global_order(
    mock_deploy, tmp_path: Path
) -> None:
    config = HanielConfig(
        repos={
            "before-repo": RepoConfig(url="git@test/before", path="./before"),
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            ),
            "after-repo": RepoConfig(url="git@test/after", path="./after"),
        },
        services={
            "before": ServiceConfig(run="before", repo="before-repo"),
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream"),
            "soulstream-soul-server-ts": ServiceConfig(
                run="soul",
                repo="soulstream",
                after=["soulstream-orch-server"],
            ),
            "after": ServiceConfig(run="after", repo="after-repo"),
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    runner._startup_manifest_updates = {"soulstream": "old-head"}
    events: list[str] = []
    runner._start_service = MagicMock(
        side_effect=lambda name: events.append(f"start:{name}") or True
    )
    mock_deploy.side_effect = lambda *_args, **_kwargs: events.append("deploy")

    runner.start_services()

    assert events == ["start:before", "deploy", "start:after"]
    mock_deploy.assert_called_once_with(
        runner,
        "soulstream",
        ["soulstream-orch-server", "soulstream-soul-server-ts"],
        "old-head",
        desired_running={
            "soulstream-orch-server",
            "soulstream-soul-server-ts",
        },
        expected_operation="upgrade",
        request_id="startup-resume-soulstream",
    )


@pytest.mark.parametrize("recovered", [True, False])
@patch("haniel.core.runner.run_manifest_deployment")
def test_start_services_only_continues_after_recovered_handover(
    mock_deploy, recovered: bool, tmp_path: Path
) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            ),
            "after-repo": RepoConfig(url="git@test/after", path="./after"),
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream"),
            "soulstream-soul-server-ts": ServiceConfig(
                run="soul",
                repo="soulstream",
                after=["soulstream-orch-server"],
            ),
            "after": ServiceConfig(run="after", repo="after-repo"),
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    runner._startup_manifest_updates = {"soulstream": "old-head"}
    startup_lease = runner.lifecycle_control.acquire_deployment(
        "soulstream", "startup-resume-soulstream"
    )
    runner._startup_deployment_leases["soulstream"] = startup_lease
    runner._start_service = MagicMock(return_value=True)
    mock_deploy.side_effect = DeploymentError("injected", recovered=recovered)

    if recovered:
        runner.start_services()
        runner._start_service.assert_called_once_with("after")
    else:
        with pytest.raises(DeploymentError, match="injected"):
            runner.start_services()
        runner._start_service.assert_not_called()

    with runner.lifecycle_control.acquire_deployment(
        "soulstream", f"after-startup-{recovered}"
    ):
        pass


@patch("haniel.core.runner.get_head", side_effect=["old-head", "new-head"])
@patch("haniel.core.runner.pull_repo")
@patch("haniel.core.runner.fetch_repo", return_value=True)
def test_manifest_repo_is_deferred_to_startup_handover(
    mock_fetch, mock_pull, mock_head, tmp_path: Path
) -> None:
    make_repo(tmp_path)
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)

    with (
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(),
        ),
        patch("haniel.core.runner.activate_repo_target", return_value=[]) as activate,
    ):
        runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "old-head"}
    assert runner._startup_updated_repos == set()
    assert runner._repo_states["soulstream"].last_head == "new-head"
    mock_pull.assert_not_called()
    activate.assert_called_once_with(
        tmp_path / "soulstream", "new-head", strategy="merge"
    )


@patch("haniel.core.runner.run_manifest_deployment")
@patch("haniel.core.runner.get_head", side_effect=["old-head", "new-head"])
@patch("haniel.core.runner.pull_repo")
@patch("haniel.core.runner.fetch_repo", return_value=True)
def test_startup_manifest_holds_repo_lock_through_handover(
    mock_fetch,
    mock_pull,
    mock_head,
    mock_deploy,
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    runner._start_service = MagicMock(return_value=True)

    with (
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(),
        ),
        patch("haniel.core.runner.activate_repo_target", return_value=[]),
    ):
        runner._apply_startup_updates()

    assert runner._pull_locks["soulstream"].locked()
    with pytest.raises(LifecycleConflict, match="DEPLOYMENT_LEASE_CONFLICT"):
        runner.lifecycle_control.acquire_deployment("soulstream", "one-shot-race")

    def assert_locked_during_handover(*_args, **_kwargs) -> None:
        assert runner._pull_locks["soulstream"].locked()

    mock_deploy.side_effect = assert_locked_during_handover
    runner.start_services()

    assert not runner._pull_locks["soulstream"].locked()
    with runner.lifecycle_control.acquire_deployment("soulstream", "after-startup"):
        pass


def test_startup_config_drift_after_probe_blocks_live_activation(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(
        config_text().replace(
            "    path: ./soulstream\n",
            "    path: ./soulstream\n"
            f"    release_manifest: {DEFAULT_RELEASE_MANIFEST}\n",
        ),
        encoding="utf-8",
    )
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )

    def drift_after_probe(*_args, **_kwargs):
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + "poll_interval: 61\n",
            encoding="utf-8",
        )
        return staged_release()

    with (
        patch("haniel.core.runner.fetch_repo", return_value=True),
        patch("haniel.core.runner.get_head", return_value="old-head"),
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            side_effect=drift_after_probe,
        ),
        patch("haniel.core.runner.activate_repo_target") as activate,
        patch("haniel.core.runner.reset_repo_to") as reset,
    ):
        runner._apply_startup_updates()

    activate.assert_not_called()
    reset.assert_not_called()
    assert runner._startup_manifest_updates == {}
    error = runner._repo_states["soulstream"].fetch_error
    assert isinstance(error, str)
    assert "CONFIG_DIGEST_MISMATCH" in error


def test_startup_cannot_mix_old_repo_target_with_reloaded_repo_identity(
    tmp_path: Path,
) -> None:
    old_repo = tmp_path / "oldrepo"
    new_repo = tmp_path / "newrepo"
    for path in (old_repo, new_repo):
        path.mkdir()
        (path / ".git").mkdir()
    config_path = tmp_path / "haniel.yaml"
    old_text = config_text().replace("./soulstream", "./oldrepo").replace(
        "    path: ./oldrepo\n",
        "    path: ./oldrepo\n"
        f"    release_manifest: {DEFAULT_RELEASE_MANIFEST}\n",
    )
    config_path.write_text(old_text, encoding="utf-8")
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )
    target_read_started = threading.Event()
    allow_target_read = threading.Event()
    reload_finished = threading.Event()
    errors: list[BaseException] = []
    target_paths: list[Path] = []

    def resolve_old_target(path: Path, _branch: str) -> str:
        target_paths.append(path)
        target_read_started.set()
        assert allow_target_read.wait(timeout=5)
        return "old-target"

    def startup() -> None:
        try:
            runner._apply_startup_updates()
        except BaseException as error:
            errors.append(error)

    def reload() -> None:
        try:
            runner.reload_config()
        except BaseException as error:
            errors.append(error)
        finally:
            reload_finished.set()

    with (
        patch("haniel.core.runner.fetch_repo", return_value=True),
        patch("haniel.core.runner.get_head", return_value="old-head"),
        patch(
            "haniel.core.runner.get_remote_head", side_effect=resolve_old_target
        ),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release("old-target"),
        ) as probe,
        patch("haniel.core.runner.activate_repo_target") as activate,
        patch("haniel.core.runner.reset_repo_to") as reset,
    ):
        startup_thread = threading.Thread(target=startup)
        startup_thread.start()
        assert target_read_started.wait(timeout=2)
        config_path.write_text(
            old_text.replace("./oldrepo", "./newrepo"),
            encoding="utf-8",
        )
        reload_thread = threading.Thread(target=reload)
        reload_thread.start()
        assert not reload_finished.wait(timeout=0.1)
        allow_target_read.set()
        startup_thread.join(timeout=5)
        reload_thread.join(timeout=5)

    assert not startup_thread.is_alive()
    assert not reload_thread.is_alive()
    assert errors == []
    assert target_paths == [old_repo]
    probe.assert_called_once()
    activate.assert_not_called()
    reset.assert_not_called()
    assert runner._repo_states["soulstream"].config.path == "./newrepo"
    assert "CONFIG_DIGEST_MISMATCH" in (
        runner._repo_states["soulstream"].fetch_error or ""
    )


def test_startup_manifest_transaction_blocks_concurrent_reload(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(
        config_text().replace(
            "    path: ./soulstream\n",
            "    path: ./soulstream\n"
            f"    release_manifest: {DEFAULT_RELEASE_MANIFEST}\n",
        ),
        encoding="utf-8",
    )
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )
    runner._start_service = MagicMock(return_value=True)
    probe_entered = threading.Event()
    allow_probe = threading.Event()
    reload_started = threading.Event()
    reload_finished = threading.Event()
    startup_finished = threading.Event()
    errors: list[BaseException] = []

    def blocking_probe(*_args, **_kwargs):
        probe_entered.set()
        assert allow_probe.wait(timeout=5)
        return staged_release()

    def startup() -> None:
        try:
            runner._apply_startup_updates()
            runner.start_services()
        except BaseException as error:
            errors.append(error)
        finally:
            startup_finished.set()

    def reload() -> None:
        reload_started.set()
        try:
            runner.reload_config()
        except BaseException as error:
            errors.append(error)
        finally:
            reload_finished.set()

    with (
        patch("haniel.core.runner.fetch_repo", return_value=True),
        patch("haniel.core.runner.get_head", side_effect=["old-head", "new-head"]),
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            side_effect=blocking_probe,
        ),
        patch("haniel.core.runner.activate_repo_target", return_value=[]),
        patch("haniel.core.runner.run_manifest_deployment"),
    ):
        startup_thread = threading.Thread(target=startup)
        startup_thread.start()
        assert probe_entered.wait(timeout=2)
        reload_thread = threading.Thread(target=reload)
        reload_thread.start()
        assert reload_started.wait(timeout=1)
        assert not reload_finished.wait(timeout=0.1)
        allow_probe.set()
        startup_thread.join(timeout=5)
        reload_thread.join(timeout=5)

    assert not startup_thread.is_alive()
    assert not reload_thread.is_alive()
    assert startup_finished.is_set()
    assert reload_finished.is_set()
    assert errors == []


def test_runtime_probe_and_activation_are_serialized_against_one_shot(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    runner.lifecycle_instance_id = "owner-1"
    runner._repo_states["soulstream"].pending_changes = {"commits": ["new"]}
    probe_entered = threading.Event()
    continue_probe = threading.Event()
    errors: list[BaseException] = []
    activated = False

    def probe(*_args, **_kwargs):
        probe_entered.set()
        assert continue_probe.wait(2)
        return staged_release()

    def activate(*_args, **_kwargs):
        nonlocal activated
        activated = True
        return []

    def head(_path: Path) -> str:
        return "new-head" if activated else "old-head"

    def runtime_pull() -> None:
        try:
            runner.trigger_pull("soulstream")
        except BaseException as error:
            errors.append(error)

    with (
        runner.lifecycle_control.acquire_owner("owner-1"),
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch("haniel.core.runner.get_head", side_effect=head),
        patch("haniel.core.runner.probe_manifest_target", side_effect=probe),
        patch("haniel.core.runner.activate_repo_target", side_effect=activate) as live,
        patch("haniel.core.runner.run_manifest_deployment"),
    ):
        thread = threading.Thread(target=runtime_pull)
        thread.start()
        assert probe_entered.wait(1)
        runner.lifecycle_control.submit_request(
            "one-shot-race",
            {
                "kind": "handover",
                "repo": "soulstream",
                "target_ref": "origin/main",
                "expected_operation": "upgrade",
            },
        )
        with pytest.raises(LifecycleConflict, match="DEPLOYMENT_LEASE_CONFLICT"):
            execute_owner_handover(
                runner,
                control=runner.lifecycle_control,
                repo_name="soulstream",
                target_ref="origin/main",
                expected_operation="upgrade",
                request_id="one-shot-race",
            )
        continue_probe.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    live.assert_called_once()


def test_auto_deploy_waiting_for_deployment_lease_does_not_hold_config_lock(
    tmp_path: Path,
) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
            )
        },
        services={
            "soulstream-server": ServiceConfig(run="server", repo="soulstream")
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    entered_lease = threading.Event()
    allow_lease = threading.Event()
    reader_finished = threading.Event()
    errors: list[BaseException] = []
    original_acquire = runner.lifecycle_control.acquire_deployment

    def blocking_acquire(repo: str, request_id: str):
        entered_lease.set()
        assert allow_lease.wait(timeout=5)
        return original_acquire(repo, request_id)

    def auto_deploy() -> None:
        try:
            runner._run_auto_deploy("soulstream")
        except BaseException as error:
            errors.append(error)

    def read_config_snapshot() -> None:
        runner.get_affected_services("soulstream")
        reader_finished.set()

    with patch.object(
        runner.lifecycle_control,
        "acquire_deployment",
        side_effect=blocking_acquire,
    ):
        deploy_thread = threading.Thread(target=auto_deploy)
        deploy_thread.start()
        assert entered_lease.wait(timeout=2)
        reader_thread = threading.Thread(target=read_config_snapshot)
        reader_thread.start()
        assert reader_finished.wait(timeout=1)
        allow_lease.set()
        deploy_thread.join(timeout=5)
        reader_thread.join(timeout=5)

    assert not deploy_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []


def test_orchestrated_pull_waits_for_startup_repo_lock(tmp_path: Path) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
            )
        },
        services={},
    )
    runner = ServiceRunner(config, config_dir=tmp_path)
    lock = runner._pull_locks["soulstream"]
    runner._startup_repo_locks.add("soulstream")
    lock.acquire()
    finished = threading.Event()
    errors: list[BaseException] = []

    def approved_pull() -> None:
        try:
            runner.trigger_pull(
                "soulstream",
                orchestrator_attempt_id="orch-attempt",
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=approved_pull)
    thread.start()
    try:
        assert not finished.wait(0.1)
    finally:
        lock.release()
    thread.join(timeout=1)

    assert finished.is_set()
    assert errors == []


@patch(
    "haniel.core.runner.discover_remote_release_manifest",
    return_value=DEFAULT_RELEASE_MANIFEST,
)
@patch("haniel.core.runner.pull_repo")
@patch("haniel.core.runner.fetch_repo", return_value=True)
def test_remote_manifest_without_writable_config_blocks_before_pull(
    mock_fetch, mock_pull, mock_discover, tmp_path: Path
) -> None:
    make_repo(tmp_path)
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(url="git@test/soulstream", path="./soulstream")
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    runner = ServiceRunner(config, config_dir=tmp_path)

    with (
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(),
        ),
        patch("haniel.core.runner.activate_repo_target", return_value=[]),
    ):
        runner._apply_startup_updates()

    mock_pull.assert_not_called()
    error = runner._repo_states["soulstream"].fetch_error
    assert isinstance(error, str)
    assert "release manifest" in error


@patch(
    "haniel.core.runner.discover_remote_release_manifest",
    return_value=DEFAULT_RELEASE_MANIFEST,
)
@patch("haniel.core.runner.get_head", side_effect=["old-head", "new-head"])
@patch("haniel.core.runner.pull_repo")
@patch("haniel.core.runner.fetch_repo", return_value=True)
def test_startup_atomically_activates_remote_manifest_before_pull(
    mock_fetch, mock_pull, mock_head, mock_discover, tmp_path: Path
) -> None:
    make_repo(tmp_path)
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(config_text(), encoding="utf-8")
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )

    with (
        patch("haniel.core.runner.get_remote_head", return_value="new-head"),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(),
        ) as probe,
        patch("haniel.core.runner.activate_repo_target", return_value=[]),
    ):
        runner._apply_startup_updates()

    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )
    assert runner._startup_manifest_updates == {"soulstream": "old-head"}
    assert probe.call_args.kwargs["config_digest"] == handover_config_digest(
        config_path
    )
    assert "soulstream" in runner._startup_manifest_reload_plans
    mock_pull.assert_not_called()


@patch("haniel.core.runner.sha256_file_at_commit", return_value="manifest-digest")
@patch("haniel.core.runner.get_remote_head", return_value="new-head")
@patch("haniel.core.runner.run_manifest_deployment")
@patch(
    "haniel.core.runner.discover_remote_release_manifest",
    return_value=DEFAULT_RELEASE_MANIFEST,
)
@patch("haniel.core.runner.get_head", side_effect=["old-head", "new-head"])
@patch("haniel.core.runner.pull_repo", return_value=[])
def test_approved_pull_activates_before_capturing_previous_head(
    mock_pull,
    mock_head,
    mock_discover,
    mock_deploy,
    mock_remote_head,
    mock_manifest_digest,
    tmp_path: Path,
) -> None:
    make_repo(tmp_path)
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(config_text(), encoding="utf-8")
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )
    runner._repo_states["soulstream"].pending_changes = {"commits": ["new"]}

    with (
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(),
        ) as probe,
        patch("haniel.core.runner.activate_repo_target", return_value=[]),
    ):
        runner.trigger_pull("soulstream")

    args, kwargs = mock_deploy.call_args
    assert args == (
        runner,
        "soulstream",
        ["soulstream-orch-server"],
        "old-head",
    )
    assert kwargs["branch"] == "main"
    assert kwargs["journal_attempt_id"]
    assert kwargs["expected_operation"] == "upgrade"
    assert kwargs["request_id"].startswith("runtime-")
    assert kwargs["config_digest"] == handover_config_digest(config_path)
    assert probe.call_args.kwargs["config_digest"] == kwargs["config_digest"]
    mock_remote_head.assert_called_once()
    mock_manifest_digest.assert_not_called()


@patch("haniel.core.runner.get_head", return_value="new-head")
@patch("haniel.core.runner.fetch_repo", return_value=False)
def test_startup_resumes_manifest_pull_interrupted_before_state_machine(
    mock_fetch, mock_head, tmp_path: Path
) -> None:
    make_repo(tmp_path)
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    store = DeploymentStateStore(tmp_path / ".haniel" / "deployments")
    store.begin("soulstream", "old-head", "old-head", "startup-pull-pending")
    runner = ServiceRunner(config, config_dir=tmp_path)

    with patch(
        "haniel.core.runner.probe_manifest_target",
        return_value=staged_release("new-head"),
    ) as probe:
        runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "old-head"}
    request_id = runner._startup_manifest_request_ids["soulstream"]
    assert request_id.startswith("startup-soulstream-")
    assert probe.call_args.kwargs["request_id"] == request_id
    probe.assert_called_once()


def test_interrupted_fresh_install_preserves_operation_and_absent_rollback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "soulstream"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    manifest = repo / DEFAULT_RELEASE_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "target"], cwd=repo, check=True)
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url=str(repo),
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(run="orch", repo="soulstream")
        },
    )
    store = DeploymentStateStore(tmp_path / ".haniel" / "deployments")
    store.begin_handover(
        "soulstream",
        previous_head="absent",
        target_ref=current_head,
        manifest_identity=DEFAULT_RELEASE_MANIFEST,
        request_id="fresh-before-crash",
        expected_operation="fresh_install",
    )
    store.bind_handover_target(
        "soulstream",
        request_id="fresh-before-crash",
        target_head=current_head,
        release_id="release-1",
        manifest_digest="digest-1",
    )
    runner = ServiceRunner(config, config_dir=tmp_path)

    with (
        patch("haniel.core.runner.fetch_repo", return_value=False),
        patch(
            "haniel.core.runner.probe_manifest_target",
            return_value=staged_release(current_head),
        ) as probe,
        patch("haniel.core.runner.run_manifest_deployment") as deploy,
        patch("haniel.core.runner.reset_repo_to") as reset,
    ):
        runner._apply_startup_updates()
        runner.start_services()

    probe.assert_called_once()
    assert probe.call_args.kwargs["expected_operation"] == "fresh_install"
    deploy.assert_called_once()
    assert deploy.call_args.args[3] == "absent"
    assert deploy.call_args.kwargs["expected_operation"] == "fresh_install"
    reset.assert_not_called()


@patch(
    "haniel.core.runner.discover_remote_release_manifest",
    return_value=DEFAULT_RELEASE_MANIFEST,
)
@patch("haniel.core.runner.get_head", return_value="current-head")
@patch("haniel.core.runner.fetch_repo", return_value=False)
def test_startup_activation_without_new_commits_still_runs_manifest_once(
    mock_fetch, mock_head, mock_discover, tmp_path: Path
) -> None:
    make_repo(tmp_path)
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text(config_text(), encoding="utf-8")
    runner = ServiceRunner(
        load_config(config_path), config_dir=tmp_path, config_path=config_path
    )

    with patch(
        "haniel.core.runner.probe_manifest_target",
        return_value=staged_release("current-head"),
    ) as probe:
        runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "current-head"}
    request_id = runner._startup_manifest_request_ids["soulstream"]
    assert request_id.startswith("startup-soulstream-")
    assert probe.call_args.kwargs["request_id"] == request_id
    probe.assert_called_once()
    journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read(
        "soulstream"
    )
    assert journal is not None
    assert journal["release_id"] == "release-1"
    assert journal["target_head"] == "current-head"

    reload_finished = threading.Event()
    errors: list[BaseException] = []

    def reload() -> None:
        try:
            runner.reload_config()
        except BaseException as error:
            errors.append(error)
        finally:
            reload_finished.set()

    reload_thread = threading.Thread(target=reload)
    reload_thread.start()
    assert not reload_finished.wait(timeout=0.1)
    with patch("haniel.core.runner.run_manifest_deployment"):
        runner.start_services()
    reload_thread.join(timeout=5)
    assert not reload_thread.is_alive()
    assert reload_finished.is_set()
    assert errors == []


def test_runner_start_closes_nonterminal_manifest_journal_before_startup_work(
    tmp_path: Path,
) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@test/soulstream",
                path="./soulstream",
                release_manifest=DEFAULT_RELEASE_MANIFEST,
            )
        },
        services={},
    )
    store = DeploymentStateStore(tmp_path / ".haniel" / "deployments")
    store.begin("soulstream", "old", "new", "release-042")
    store.transition("soulstream", "verifying", message="health probe running")
    runner = ServiceRunner(config, config_dir=tmp_path)

    with (
        patch.object(runner, "_init_repo_states"),
        patch.object(runner, "_start_mcp_server"),
        patch.object(runner, "_start_slack_bot"),
        patch.object(runner, "_start_orch_client"),
        patch.object(runner, "_apply_startup_updates"),
        patch.object(runner, "start_services"),
        patch("haniel.core.runner.threading.Thread"),
    ):
        runner.start()

    journal = store.read("soulstream")
    assert journal is not None
    assert journal["state"] == "interrupted"
    assert journal["interrupted_from"] == "verifying"
    assert "runner restarted" in journal["interruption_reason"]
