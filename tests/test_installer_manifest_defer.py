"""Mechanical install must defer manifest-aware live mutations."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from haniel.config import HanielConfig, HooksConfig, RepoConfig
from haniel.commands.install import _print_completion
from haniel.installer.mechanical import MechanicalInstaller
from haniel.installer.orchestrator import InstallOrchestrator
from haniel.installer.state import InstallPhase, InstallState


def config_for(
    path: Path,
    *,
    manifest: str | None,
    url: str = "https://example.test/app.git",
    branch: str = "main",
) -> HanielConfig:
    return HanielConfig(
        repos={
            "app": RepoConfig(
                url=url,
                branch=branch,
                path=str(path),
                release_manifest=manifest,
                hooks=HooksConfig(post_pull="build-app"),
            )
        },
        services={},
    )


def create_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
    )
    (repo / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True)
    return repo, remote


def test_existing_manifest_repo_defer_verifies_identity_without_pull_or_post_pull(
    tmp_path: Path,
) -> None:
    repo, remote = create_repo_with_remote(tmp_path)
    state = InstallState()
    installer = MechanicalInstaller(
        config_for(repo, manifest="deploy/release.json", url=str(remote)),
        tmp_path,
        state,
        defer_manifest_handover=True,
    )

    with patch.object(installer, "_run_repo_hook") as hook:
        installer.clone_repos()

    hook.assert_not_called()
    assert state.is_step_complete("repos")


def test_existing_manifest_repo_defer_rejects_origin_or_branch_drift(
    tmp_path: Path,
) -> None:
    repo, _remote = create_repo_with_remote(tmp_path)

    for suffix, url, branch, code in (
        ("origin", str(tmp_path / "different.git"), "main", "REPO_IDENTITY_MISMATCH"),
        ("branch", str(tmp_path / "remote.git"), "missing", "REPO_BRANCH_MISSING"),
    ):
        state = InstallState()
        installer = MechanicalInstaller(
            config_for(
                repo,
                manifest="deploy/release.json",
                url=url,
                branch=branch,
            ),
            tmp_path / suffix,
            state,
            defer_manifest_handover=True,
        )

        installer.clone_repos()

        assert not state.is_step_complete("repos")
        assert state.failed_steps[-1].step == "repos:app"
        assert code in state.failed_steps[-1].error


def test_new_manifest_repo_defer_clones_but_does_not_run_post_pull(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state = InstallState()
    installer = MechanicalInstaller(
        config_for(repo, manifest="deploy/release.json"),
        tmp_path,
        state,
        defer_manifest_handover=True,
    )

    with (
        patch("haniel.installer.mechanical.subprocess.run") as run,
        patch.object(installer, "_run_repo_hook") as hook,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = "cloned"
        run.return_value.stderr = ""
        installer.clone_repos()

    assert run.call_count == 1
    assert run.call_args.args[0][:3] == ["git", "clone", "--branch"]
    clone_target = Path(run.call_args.args[0][-1])
    assert clone_target == repo.with_name(f".{repo.name}.haniel-initial")
    assert not repo.exists()
    hook.assert_not_called()


def test_legacy_repo_keeps_pull_and_post_pull_behavior(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    state = InstallState()
    installer = MechanicalInstaller(
        config_for(repo, manifest=None),
        tmp_path,
        state,
        defer_manifest_handover=True,
    )

    with (
        patch("haniel.installer.mechanical.subprocess.run") as run,
        patch.object(installer, "_run_repo_hook") as hook,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = "updated"
        run.return_value.stderr = ""
        installer.clone_repos()

    run.assert_called_once()
    hook.assert_called_once()


def test_deferred_finalize_never_stops_or_replaces_resident_service(
    tmp_path: Path,
) -> None:
    state = InstallState(phase=InstallPhase.FINALIZE)
    orchestrator = InstallOrchestrator(
        HanielConfig(),
        tmp_path,
        state,
        defer_manifest_handover=True,
    )
    finalizer = MagicMock()
    finalizer.check_all_configs_filled.return_value = True
    orchestrator._finalizer = finalizer

    assert orchestrator.run_finalize_phase() is True

    finalizer.generate_config_files.assert_called_once_with()
    finalizer.register_service.assert_not_called()
    assert "service-registration-deferred" in state.completed_steps
    assert state.phase == InstallPhase.COMPLETE


def test_deferred_completion_never_claims_service_was_registered(
    tmp_path: Path,
    capsys,
) -> None:
    state = InstallState(
        phase=InstallPhase.COMPLETE,
        completed_steps=["service-registration-deferred"],
    )
    orchestrator = MagicMock()
    orchestrator.state = state
    orchestrator.finalizer.get_completion_summary.return_value = {
        "generated_files": [],
        "service": {"name": "haniel"},
    }

    _print_completion(orchestrator, tmp_path / "haniel.yaml", HanielConfig())

    rendered = capsys.readouterr().out
    assert "Service registered:" not in rendered
    assert "Service registration deferred to manifest handover" in rendered
