"""Release-manifest restart filters: candidate planning and handover behavior."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.deployment import (
    CommandResult,
    CommandSpec,
    DeploymentCallbacks,
    DeploymentCoordinator,
    DeploymentError,
    DeploymentStateStore,
    ReleaseManifest,
)
from haniel.core.git import (
    fetch_repo,
    get_changed_paths_between,
    get_head,
    get_remote_head,
)
from haniel.core.release_manifest import ServiceRestartFilter
from haniel.core.restart_filter import (
    RestartFilterCandidates,
    paths_all_ignored,
    plan_restart_skips,
)
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import RunnerDeploymentAdapter


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    return path


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return get_head(repo)


def _command_payload(name: str) -> dict[str, object]:
    return {"name": name, "command": f"run-{name}", "timeout_seconds": 30}


def _manifest_payload(
    *,
    migration: bool = True,
    contract: bool = False,
    filters: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "haniel.release.v1",
        "release_id": "release-042",
        "post_start_verify": [_command_payload("verify")],
        "recovery": {
            "strategy": "rollback",
            "command": _command_payload("restore"),
        },
    }
    if migration:
        migration_payload: dict[str, object] = {
            "preflight": _command_payload("preflight"),
            "apply": _command_payload("migrate"),
        }
        if contract:
            migration_payload["operation"] = "discover"
        payload["migration"] = migration_payload
    if filters is not None:
        payload["service_restart_filters"] = filters
    return payload


def _filters(**patterns: list[str]) -> dict[str, ServiceRestartFilter]:
    return {
        name: ServiceRestartFilter(ignore_paths=value)
        for name, value in patterns.items()
    }


# ---------------------------------------------------------------------------
# Manifest declaration
# ---------------------------------------------------------------------------


def test_manifest_without_filters_keeps_the_existing_shape() -> None:
    manifest = ReleaseManifest.model_validate(_manifest_payload())

    assert manifest.service_restart_filters == {}


def test_manifest_accepts_declared_filters() -> None:
    manifest = ReleaseManifest.model_validate(
        _manifest_payload(filters={"app": {"ignore_paths": ["docs/*"]}})
    )

    assert manifest.service_restart_filters["app"].ignore_paths == ["docs/*"]


def test_filter_rejects_empty_ignore_paths() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(
            _manifest_payload(filters={"app": {"ignore_paths": []}})
        )


def test_filter_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(
            _manifest_payload(
                filters={"app": {"ignore_paths": ["docs/*"], "always_restart": True}}
            )
        )


# ---------------------------------------------------------------------------
# Candidate planning
# ---------------------------------------------------------------------------


def test_changed_paths_all_inside_filters_make_candidates() -> None:
    skips = plan_restart_skips(
        affected=["api", "worker"],
        filters=_filters(api=["docs/*"], worker=["docs/*"]),
        changed_paths=["docs/a.md", "docs/nested/b.md"],
        dependencies={},
    )

    assert skips == frozenset({"api", "worker"})


def test_one_unlisted_path_keeps_the_service_restarting() -> None:
    skips = plan_restart_skips(
        affected=["api"],
        filters=_filters(api=["docs/*"]),
        changed_paths=["docs/a.md", "src/app.py"],
        dependencies={},
    )

    assert skips == frozenset()


def test_patterns_match_across_slashes_like_fnmatchcase() -> None:
    assert paths_all_ignored(["docs/nested/deep.md"], ["docs*"])
    assert not paths_all_ignored(["src/docs.md"], ["docs*"])


def test_no_changed_paths_means_no_candidates() -> None:
    assert (
        plan_restart_skips(
            affected=["api"],
            filters=_filters(api=["docs/*"]),
            changed_paths=[],
            dependencies={},
        )
        == frozenset()
    )


def test_service_without_a_filter_is_never_a_candidate() -> None:
    skips = plan_restart_skips(
        affected=["api", "worker"],
        filters=_filters(api=["docs/*"]),
        changed_paths=["docs/a.md"],
        dependencies={},
    )

    assert skips == frozenset({"api"})


def test_candidate_depending_on_a_restarting_service_is_dropped() -> None:
    skips = plan_restart_skips(
        affected=["orch", "soul-server"],
        filters={"soul-server": ServiceRestartFilter(ignore_paths=["docs/*"])},
        changed_paths=["docs/a.md"],
        dependencies={"soul-server": ["orch"]},
    )

    assert skips == frozenset()


def test_transitive_dependents_of_a_restarting_service_are_dropped() -> None:
    skips = plan_restart_skips(
        affected=["base", "middle", "top"],
        filters=_filters(middle=["docs/*"], top=["docs/*"]),
        changed_paths=["docs/a.md"],
        dependencies={"middle": ["base"], "top": ["middle"]},
    )

    assert skips == frozenset()


def test_dependency_outside_affected_does_not_drop_a_candidate() -> None:
    skips = plan_restart_skips(
        affected=["api"],
        filters=_filters(api=["docs/*"]),
        changed_paths=["docs/a.md"],
        dependencies={"api": ["database"]},
    )

    assert skips == frozenset({"api"})


# ---------------------------------------------------------------------------
# Two-commit diff
# ---------------------------------------------------------------------------


def test_changed_paths_between_commits(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "docs" / "a.md").write_text("b", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("b", encoding="utf-8")
    target = _commit(repo, "target")

    assert set(get_changed_paths_between(repo, base, target)) == {
        "docs/a.md",
        "src/app.py",
    }
    assert get_changed_paths_between(repo, base, base) == []


def test_changed_paths_between_keeps_both_sides_of_a_move(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "moved.py").write_text("x", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "docs").mkdir()
    os.rename(repo / "src" / "moved.py", repo / "docs" / "moved.py")
    target = _commit(repo, "move")

    assert set(get_changed_paths_between(repo, base, target)) == {
        "src/moved.py",
        "docs/moved.py",
    }


def test_changed_paths_between_unknown_ref_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "value.txt").write_text("x", encoding="utf-8")
    head = _commit(repo, "base")

    assert get_changed_paths_between(repo, "0" * 40, head) is None


# ---------------------------------------------------------------------------
# Coordinator confirmation
# ---------------------------------------------------------------------------


def _execute(
    tmp_path: Path,
    payload: dict[str, object],
    *,
    preflight_json: dict[str, object] | None = None,
    candidates: RestartFilterCandidates | None = None,
    dropped: frozenset[str] = frozenset(),
    writer_services: tuple[str, ...] = (),
    stop_receipt: dict[str, object] | None = None,
) -> tuple[list[str], dict[str, object] | None, Exception | None]:
    events: list[str] = []
    manifest = ReleaseManifest.model_validate(payload)

    def run(spec: CommandSpec, _environment: dict[str, str]) -> CommandResult | None:
        events.append(f"command:{spec.name}")
        if spec.name == "preflight" and preflight_json is not None:
            return CommandResult(stdout="{}", json_data=preflight_json)
        return None

    def apply_restart_skips(services: frozenset[str]) -> frozenset[str]:
        applied = frozenset(services) - dropped
        if applied:
            events.append("skip:" + ",".join(sorted(applied)))
        return applied

    callbacks = DeploymentCallbacks(
        build=lambda: events.append("build"),
        stop=lambda: (events.append("stop"), stop_receipt)[1],
        start_and_wait=lambda: events.append("start"),
        rollback=lambda: events.append("rollback"),
        prepare_roll_forward=lambda: events.append("prepare-roll-forward"),
        writer_services=writer_services,
        affected_services=writer_services,
        restart_candidates=candidates,
        apply_restart_skips=apply_restart_skips,
    )
    store = DeploymentStateStore(tmp_path / "state")
    coordinator = DeploymentCoordinator(state_store=store, command_runner=run)
    error: Exception | None = None
    try:
        coordinator.execute(
            repo_name="app",
            previous_head="old",
            target_head="new",
            manifest=manifest,
            callbacks=callbacks,
            expected_operation="upgrade",
        )
    except Exception as exc:  # noqa: BLE001 - tests inspect the terminal error
        error = exc
    return events, store.read("app"), error


def _contract_preflight(**extra: object) -> dict[str, object]:
    return {
        "schema_version": "soulstream.database-release.v1",
        "ok": True,
        "operation": "upgrade",
        "journal_path": "/tmp/database-release.json",
        **extra,
    }


def _candidates(*services: str) -> RestartFilterCandidates:
    return RestartFilterCandidates(services=frozenset(services), changed_path_count=2)


def test_contract_upgrade_with_zero_pending_skips_stop_and_receipt(
    tmp_path: Path,
) -> None:
    validate = MagicMock()
    with (
        patch("haniel.core.deployment.validate_quiescence_receipt", validate),
        patch.object(DeploymentStateStore, "write_quiescence_receipt") as write_receipt,
    ):
        events, journal, error = _execute(
            tmp_path,
            _manifest_payload(contract=True),
            preflight_json=_contract_preflight(pending_count=0),
            candidates=_candidates("worker"),
            writer_services=("app", "worker"),
            stop_receipt=None,
        )

    assert error is None
    assert "skip:worker" in events
    assert "stop" in events
    assert "command:migrate" in events
    assert "start" in events
    assert journal is not None
    assert journal["state"] == "success"
    assert journal["restart_skipped_services"] == ["worker"]
    validate.assert_not_called()
    write_receipt.assert_not_called()
    assert not (tmp_path / "state" / "app.quiescence.json").exists()
    states = [entry["state"] for entry in journal["history"]]
    assert states.index("migrating") < states.index("starting")


def test_contract_upgrade_with_additive_pending_skips_stop_and_receipt(
    tmp_path: Path,
) -> None:
    validate = MagicMock()
    with (
        patch("haniel.core.deployment.validate_quiescence_receipt", validate),
        patch.object(DeploymentStateStore, "write_quiescence_receipt") as write_receipt,
    ):
        events, journal, error = _execute(
            tmp_path,
            _manifest_payload(contract=True),
            preflight_json=_contract_preflight(
                pending_count=1, pending_additive_only=True
            ),
            candidates=_candidates("worker"),
            writer_services=("app", "worker"),
            stop_receipt=None,
        )

    assert error is None
    assert "skip:worker" in events
    assert "command:migrate" in events
    assert journal is not None
    assert journal["state"] == "success"
    assert journal["restart_skipped_services"] == ["worker"]
    validate.assert_not_called()
    write_receipt.assert_not_called()


def test_zero_pending_with_no_running_candidates_keeps_the_old_handover(
    tmp_path: Path,
) -> None:
    events, journal, error = _execute(
        tmp_path,
        _manifest_payload(contract=True),
        preflight_json=_contract_preflight(pending_count=0),
        candidates=_candidates("standalone"),
        dropped=frozenset({"standalone"}),
        writer_services=("app", "standalone"),
        stop_receipt=None,
    )

    assert isinstance(error, DeploymentError)
    assert "QUIESCENCE_REQUIRED" in str(error)
    assert "skip:" not in events
    assert journal is not None
    assert "restart_skipped_services" not in journal


def test_only_applied_candidates_reach_the_journal_and_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="haniel.core.deployment")

    events, journal, error = _execute(
        tmp_path,
        _manifest_payload(contract=True),
        preflight_json=_contract_preflight(pending_count=0),
        candidates=_candidates("worker", "standalone"),
        dropped=frozenset({"standalone"}),
        writer_services=("app", "worker", "standalone"),
        stop_receipt=None,
    )

    assert error is None
    assert "skip:worker" in events
    assert "skip:standalone" not in events
    assert journal is not None
    assert journal["restart_skipped_services"] == ["worker"]
    assert (
        "Skipping restarts for app at new: left running: worker; "
        "restarting: app, standalone (2 changed path(s) all matched the "
        "ignore_paths of the services left running)" in caplog.text
    )


def test_all_affected_services_skipped_reports_restarting_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="haniel.core.deployment")

    events, journal, error = _execute(
        tmp_path,
        _manifest_payload(contract=True),
        preflight_json=_contract_preflight(pending_count=0),
        candidates=_candidates("worker"),
        writer_services=("worker",),
        stop_receipt=None,
    )

    assert error is None
    assert "skip:worker" in events
    assert journal is not None
    assert journal["restart_skipped_services"] == ["worker"]
    assert "restarting: none" in caplog.text


@pytest.mark.parametrize(
    "preflight",
    [
        pytest.param(_contract_preflight(), id="missing"),
        pytest.param(_contract_preflight(pending_count=1), id="one"),
        pytest.param(
            _contract_preflight(pending_count=1, pending_additive_only=False),
            id="one-not-additive",
        ),
        pytest.param(
            _contract_preflight(pending_count=1, pending_additive_only="true"),
            id="one-string-true",
        ),
        pytest.param(_contract_preflight(pending_count="0"), id="string-zero"),
        pytest.param(_contract_preflight(pending_count=False), id="false"),
        pytest.param(
            _contract_preflight(pending_count=-1, pending_additive_only=True),
            id="negative-additive",
        ),
    ],
)
def test_contract_upgrade_without_zero_pending_keeps_the_old_handover(
    tmp_path: Path, preflight: dict[str, object]
) -> None:
    events, journal, error = _execute(
        tmp_path,
        _manifest_payload(contract=True),
        preflight_json=preflight,
        candidates=_candidates("worker"),
        writer_services=("app", "worker"),
        stop_receipt=None,
    )

    assert isinstance(error, DeploymentError)
    assert "QUIESCENCE_REQUIRED" in str(error)
    assert not any(event.startswith("skip:") for event in events)
    assert "rollback" in events
    assert journal is not None
    assert "restart_skipped_services" not in journal


def test_non_contract_manifest_skips_candidates_without_preflight(
    tmp_path: Path,
) -> None:
    events, journal, error = _execute(
        tmp_path,
        _manifest_payload(),
        candidates=_candidates("worker"),
        stop_receipt=None,
    )

    assert error is None
    assert "skip:worker" in events
    assert "stop" in events
    assert "start" in events
    assert journal is not None
    assert journal["state"] == "success"
    assert journal["restart_skipped_services"] == ["worker"]


# ---------------------------------------------------------------------------
# Adapter stop/start/rollback sets
# ---------------------------------------------------------------------------


def _adapter_fixture(
    tmp_path: Path,
) -> tuple[ServiceRunner, Path, str]:
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.txt").write_text("old", encoding="utf-8")
    head = _commit(repo, "old")
    config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="git@github.com:test/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={
            "app": ServiceConfig(run="app", repo="app", ready="port:9999"),
            "worker": ServiceConfig(
                run="worker", repo="app", after=["app"], ready="port:9998"
            ),
            "standalone": ServiceConfig(
                run="standalone", repo="app", ready="port:9997"
            ),
        },
    )
    return ServiceRunner(config, config_dir=tmp_path), repo, head


def _mock_processes(
    runner: ServiceRunner, names: list[str]
) -> tuple[list[str], dict[str, bool]]:
    running = {name: True for name in names}
    events: list[str] = []

    def stop(name: str) -> bool:
        events.append(f"stop:{name}")
        running[name] = False
        return True

    def start(name: str) -> bool:
        events.append(f"start:{name}")
        running[name] = True
        return True

    runner.process_manager.is_running = MagicMock(
        side_effect=lambda name: running[name]
    )
    runner.process_manager.stop_service = MagicMock(side_effect=stop)
    runner.process_manager.get_pid = MagicMock(
        side_effect=lambda name: 1000 if running[name] else None
    )
    runner.process_manager.wait_for_ready = MagicMock(
        side_effect=lambda name, timeout=None: events.append(f"ready:{name}") or True
    )
    runner.process_manager.platform.is_port_owned_by_process_tree = MagicMock(
        return_value=True
    )
    runner._start_service = MagicMock(side_effect=start)
    runner.execute_hook = MagicMock(return_value=True)
    return events, running


def test_skipped_service_stays_out_of_stop_and_start(tmp_path: Path) -> None:
    runner, repo, head = _adapter_fixture(tmp_path)
    events, running = _mock_processes(runner, ["app", "worker", "standalone"])
    affected = ["app", "worker", "standalone"]
    adapter = RunnerDeploymentAdapter(
        runner=runner,
        repo_name="app",
        affected=affected,
        repo_path=repo,
        previous_head=head,
        target_head=head,
        quiesce_services=affected,
    )
    adapter.apply_restart_skips(frozenset({"standalone"}))

    receipt = adapter.stop()

    assert "stop:standalone" not in events
    assert set(receipt["stopped_services"]) == {"app", "worker"}
    assert receipt["quiesced_services"] == ["app", "worker"]
    assert running["standalone"] is True

    events.clear()
    adapter.start_and_wait({"app", "worker", "standalone"})

    assert "start:standalone" not in events
    assert {"start:app", "start:worker"} <= set(events)


def test_candidate_already_stopped_stays_in_stop_and_start_sets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner, repo, head = _adapter_fixture(tmp_path)
    events, running = _mock_processes(runner, ["app", "worker"])
    running["standalone"] = False
    affected = ["app", "worker", "standalone"]
    adapter = RunnerDeploymentAdapter(
        runner=runner,
        repo_name="app",
        affected=affected,
        repo_path=repo,
        previous_head=head,
        target_head=head,
        quiesce_services=affected,
    )

    with caplog.at_level(logging.INFO):
        applied = adapter.apply_restart_skips(frozenset({"worker", "standalone"}))

    assert applied == frozenset({"worker"})
    assert adapter.restart_skipped == frozenset({"worker"})
    assert "standalone" in caplog.text

    receipt = adapter.stop()

    assert "stop:worker" not in events
    assert "stop:standalone" not in events
    assert set(receipt["stopped_services"]) == {"app"}
    assert set(receipt["already_stopped_services"]) == {"standalone"}
    assert set(receipt["quiesced_services"]) == {"app", "standalone"}
    assert running["worker"] is True

    events.clear()
    adapter.start_and_wait(set(affected))

    assert "start:standalone" in events
    assert "start:worker" not in events
    assert running["standalone"] is True


def test_rollback_does_not_start_a_skipped_service(tmp_path: Path) -> None:
    runner, repo, head = _adapter_fixture(tmp_path)
    events, _running = _mock_processes(runner, ["app", "worker", "standalone"])
    affected = ["app", "worker", "standalone"]
    adapter = RunnerDeploymentAdapter(
        runner=runner,
        repo_name="app",
        affected=affected,
        repo_path=repo,
        previous_head=head,
        target_head=head,
        desired_running=set(affected),
        quiesce_services=affected,
    )
    adapter.apply_restart_skips(frozenset({"standalone"}))
    runner._commit_repo_observation = MagicMock(return_value=True)

    with (
        patch("haniel.core.runner_deployment.reset_repo_to") as reset,
        patch("haniel.core.runner_deployment.get_head", return_value=head),
    ):
        adapter.rollback()

    reset.assert_called_once_with(repo, head)
    assert "start:standalone" not in events
    assert {"start:app", "start:worker"} <= set(events)


# ---------------------------------------------------------------------------
# Pull path wiring
# ---------------------------------------------------------------------------


def _remote_with_update(
    tmp_path: Path,
    name: str,
    *,
    manifest: dict[str, object],
    changed: dict[str, str],
) -> tuple[Path, Path, str]:
    source = _init_repo(tmp_path / f"{name}-source")
    (source / "value.txt").write_text("old", encoding="utf-8")
    _commit(source, "old")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True)
    live = tmp_path / f"{name}-live"
    subprocess.run(["git", "clone", str(remote), str(live)], check=True)
    previous = get_head(live)
    (source / "deploy").mkdir()
    (source / "deploy" / "release.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    for relative, content in changed.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _commit(source, "new")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "main")
    return live, remote, previous


def _pull_runner(tmp_path: Path, live: Path, remote: Path) -> ServiceRunner:
    return ServiceRunner(
        HanielConfig(
            repos={
                "app": RepoConfig(
                    url=str(remote),
                    path=str(live),
                    release_manifest="deploy/release.json",
                )
            },
            services={
                "app-service": ServiceConfig(run="unused", repo="app"),
            },
        ),
        config_dir=tmp_path,
    )


def _pull_with_staged_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filters: dict[str, object] | None,
    changed: dict[str, str],
) -> tuple[MagicMock, Path, Path]:
    payload = _manifest_payload(filters=filters)
    live, remote, _previous = _remote_with_update(
        tmp_path, "app", manifest=payload, changed=changed
    )
    fetch_repo(path=live, branch="main")
    target = get_remote_head(live, "main")
    runner = _pull_runner(tmp_path, live, remote)
    runner._repo_states["app"].pending_changes = {"commits": [target]}
    staged = SimpleNamespace(
        target_head=target,
        manifest_digest="d" * 64,
        manifest=ReleaseManifest.model_validate(payload),
    )
    monkeypatch.setattr(
        "haniel.core.runner.probe_manifest_target", lambda *_a, **_k: staged
    )
    deploy = MagicMock()
    monkeypatch.setattr("haniel.core.runner.run_manifest_deployment", deploy)

    runner.trigger_pull("app")
    return deploy, live, remote


def test_pull_passes_restart_candidates_when_all_changed_paths_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy, _live, _remote = _pull_with_staged_manifest(
        tmp_path,
        monkeypatch,
        filters={"app-service": {"ignore_paths": ["docs/*", "deploy/*"]}},
        changed={"docs/guide.md": "new"},
    )

    candidates = deploy.call_args.kwargs["restart_candidates"]

    assert candidates is not None
    assert candidates.services == frozenset({"app-service"})
    assert candidates.changed_path_count == 2


def test_pull_without_a_matching_changed_path_passes_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy, _live, _remote = _pull_with_staged_manifest(
        tmp_path,
        monkeypatch,
        filters={"app-service": {"ignore_paths": ["docs/*", "deploy/*"]}},
        changed={"docs/guide.md": "new", "src/app.py": "new"},
    )

    assert deploy.call_args.kwargs["restart_candidates"] is None


def test_pull_without_declared_filters_passes_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy, _live, _remote = _pull_with_staged_manifest(
        tmp_path,
        monkeypatch,
        filters=None,
        changed={"docs/guide.md": "new"},
    )

    assert deploy.call_args.kwargs["restart_candidates"] is None


def test_candidate_planning_rejects_same_commit_and_unavailable_diff(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "value.txt").write_text("old", encoding="utf-8")
    head = _commit(repo, "old")
    runner = ServiceRunner(
        HanielConfig(services={"app": ServiceConfig(run="app")}),
        config_dir=tmp_path,
    )
    manifest = ReleaseManifest.model_validate(
        _manifest_payload(filters={"app": {"ignore_paths": ["docs/*"]}})
    )

    assert (
        runner._restart_filter_candidates(
            repo_path=repo,
            previous_head=head,
            target_head=head,
            manifest=manifest,
            affected=["app"],
        )
        is None
    )
    assert (
        runner._restart_filter_candidates(
            repo_path=repo,
            previous_head="0" * 40,
            target_head=head,
            manifest=manifest,
            affected=["app"],
        )
        is None
    )
