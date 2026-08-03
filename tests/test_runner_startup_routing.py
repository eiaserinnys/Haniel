"""Startup and approved pulls activate one release-manifest control path."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.config.release_activation import DEFAULT_RELEASE_MANIFEST
from haniel.core.deployment import DeploymentError
from haniel.core.deployment import DeploymentStateStore
from haniel.core.runner import ServiceRunner


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
    runner._start_service = MagicMock(return_value=True)
    mock_deploy.side_effect = DeploymentError("injected", recovered=recovered)

    if recovered:
        runner.start_services()
        runner._start_service.assert_called_once_with("after")
    else:
        with pytest.raises(DeploymentError, match="injected"):
            runner.start_services()
        runner._start_service.assert_not_called()


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

    runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "old-head"}
    assert runner._startup_updated_repos == set()
    assert runner._repo_states["soulstream"].last_head == "new-head"


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

    runner._apply_startup_updates()

    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )
    assert runner._startup_manifest_updates == {"soulstream": "old-head"}
    mock_pull.assert_called_once()


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
    mock_remote_head.assert_called_once()
    mock_manifest_digest.assert_called_once()


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

    runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "old-head"}


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

    runner._apply_startup_updates()

    assert runner._startup_manifest_updates == {"soulstream": "current-head"}
    journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read(
        "soulstream"
    )
    assert journal is not None
    assert journal["release_id"] == "startup-activation-pending"


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
