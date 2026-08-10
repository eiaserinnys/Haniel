"""Real process, Git, spool, and release-child config handover contracts."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.core.deployment_state import DeploymentStateStore
from haniel.core.deployment_command_runner import (
    CommandSpec,
    DeploymentCommandError,
    subprocess_command_runner,
)
from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.lifecycle_locks import (
    ConfigTransactionLock,
    ConfigTransactionLockTimeout,
    SerialFileLock,
)
from haniel.core.lifecycle_request_server import LifecycleRequestServer
from haniel.core.one_shot_handover import (
    _resolve_bound_manifest_environment,
    execute_owner_handover,
)
from haniel.core.release_manifest import ReleaseManifest
from haniel.core.service_environment import service_process_environment
from haniel.core.service_environment import read_service_environment_file
from haniel.core.runner import ServiceRunner
from haniel.core.runner_deployment import (
    RunnerDeploymentAdapter,
    run_manifest_deployment,
)


def test_config_write_transaction_serializes_across_processes(tmp_path: Path) -> None:
    """The config writer boundary must cover a separate CLI/runtime process."""

    from haniel.core.service_lifecycle import config_file_transaction

    config_path = tmp_path / "haniel.yaml"
    config_path.write_text("services: {}\n", encoding="utf-8")
    child = None
    acquired_line: list[str] = []
    acquired = threading.Event()

    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from haniel.core.service_lifecycle import config_file_transaction",
            "print('attempt', flush=True)",
            "with config_file_transaction(Path(sys.argv[1])):",
            "    print('acquired', flush=True)",
        )
    )

    def read_acquired() -> None:
        assert child is not None and child.stdout is not None
        acquired_line.append(child.stdout.readline().strip())
        acquired.set()

    with config_file_transaction(config_path):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "attempt"
        reader = threading.Thread(target=read_acquired, daemon=True)
        reader.start()
        assert not acquired.wait(timeout=0.2)

    try:
        assert acquired.wait(timeout=5)
        reader.join(timeout=1)
        assert acquired_line == ["acquired"]
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess fixture")
def test_config_lock_timeout_does_not_wedge_unrelated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from haniel.core.service_lifecycle import config_file_transaction

    blocked_dir = tmp_path / "blocked"
    unrelated_dir = tmp_path / "unrelated"
    blocked_dir.mkdir()
    unrelated_dir.mkdir()
    blocked = blocked_dir / "haniel.yaml"
    unrelated = unrelated_dir / "haniel.yaml"
    blocked.write_text("services: {}\n", encoding="utf-8")
    unrelated.write_text("services: {}\n", encoding="utf-8")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from haniel.core.lifecycle_locks import ConfigTransactionLock",
                    "with ConfigTransactionLock(Path(sys.argv[1]), operation='holder'):",
                    "    print('locked', flush=True)",
                    "    sys.stdin.readline()",
                )
            ),
            str(blocked),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    monkeypatch.setattr(
        ConfigTransactionLock, "DEFAULT_TIMEOUT_SECONDS", 0.2, raising=False
    )
    blocked_done = threading.Event()
    unrelated_done = threading.Event()
    failures: list[BaseException] = []

    def enter_blocked() -> None:
        try:
            with config_file_transaction(blocked):
                pass
        except BaseException as error:
            failures.append(error)
        finally:
            blocked_done.set()

    def enter_unrelated() -> None:
        with config_file_transaction(unrelated):
            unrelated_done.set()

    blocked_thread = threading.Thread(target=enter_blocked, daemon=True)
    unrelated_thread = threading.Thread(target=enter_unrelated, daemon=True)
    try:
        blocked_thread.start()
        time.sleep(0.05)
        unrelated_thread.start()
        assert unrelated_done.wait(timeout=0.5)
        assert blocked_done.wait(timeout=0.5)
        assert len(failures) == 1
        assert getattr(failures[0], "code", None) == "CONFIG_LOCK_TIMEOUT"
    finally:
        if holder.stdin is not None:
            holder.stdin.write("release\n")
            holder.stdin.flush()
        holder.wait(timeout=5)
        blocked_thread.join(timeout=2)
        unrelated_thread.join(timeout=2)


def test_configs_in_same_directory_have_distinct_file_lock_identity(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("services: {}\n", encoding="utf-8")
    second.write_text("services: {}\n", encoding="utf-8")
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from haniel.core.service_lifecycle import config_file_transaction",
            "with config_file_transaction(Path(sys.argv[1]), timeout_seconds=0.5):",
            "    print('acquired', flush=True)",
        )
    )

    from haniel.core.service_lifecycle import config_file_transaction

    with config_file_transaction(first):
        result = subprocess.run(
            [sys.executable, "-c", script, str(second)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )

    assert result.returncode == 0
    assert result.stdout.strip() == "acquired"


def test_config_transaction_uses_cross_session_file_identity(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    lock_path = ConfigTransactionLock.lock_path(config_path)
    transaction = ConfigTransactionLock(config_path)

    assert lock_path.parent == tmp_path / ".haniel"
    assert lock_path.name.endswith(".config.lock")
    assert isinstance(transaction._serial, SerialFileLock)
    assert transaction._serial.path == lock_path


def test_config_lock_timeout_reports_named_holder_evidence(tmp_path: Path) -> None:
    from haniel.core.service_lifecycle import config_file_transaction

    config_path = tmp_path / "haniel.yaml"
    config_path.write_text("services: {}\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with config_file_transaction(config_path, operation="clone"):
            entered.set()
            release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)
    try:
        with pytest.raises(ConfigTransactionLockTimeout) as blocked:
            with config_file_transaction(
                config_path,
                operation="reload",
                timeout_seconds=0.1,
            ):
                pass
        assert "clone" in str(blocked.value)
        assert "pid=" in str(blocked.value)
    finally:
        release.set()
        holder.join(timeout=2)


def test_serial_file_lock_uses_bounded_canonical_wait(tmp_path: Path) -> None:
    path = tmp_path / "serial.lock"
    with SerialFileLock(path, timeout_seconds=1, operation="holder"):
        with pytest.raises(ConfigTransactionLockTimeout):
            with SerialFileLock(path, timeout_seconds=0.05, operation="waiter"):
                pass


def test_serial_file_lock_local_identity_expands_user_before_resolving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    alias = Path("~/locks/serial.lock")
    canonical = (tmp_path / "locks" / "serial.lock").resolve()

    with SerialFileLock(alias, timeout_seconds=1, operation="alias") as lock:
        key = os.path.normcase(str(canonical))
        assert SerialFileLock._local_locks[key] is lock._local_lock


@pytest.mark.skipif(os.name == "nt", reason="POSIX process contention stress")
def test_serial_file_lock_multiprocess_stress_has_no_starvation(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "multiprocess.lock"
    script = "\n".join(
        (
            "import sys, time",
            "from pathlib import Path",
            "from haniel.core.lifecycle_locks import SerialFileLock",
            "path = Path(sys.argv[1])",
            "for iteration in range(12):",
            "    with SerialFileLock(path, timeout_seconds=10, operation=f'child-{sys.argv[2]}'):",
            "        time.sleep(0.002)",
            "print('12', flush=True)",
        )
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(8)
    ]
    started_at = time.monotonic()
    try:
        results = [worker.communicate(timeout=12) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)

    assert time.monotonic() - started_at < 12
    assert [worker.returncode for worker in workers] == [0] * len(workers)
    assert [stdout.strip() for stdout, _stderr in results] == ["12"] * len(workers)
    assert all("Traceback" not in stderr for _stdout, stderr in results)


@pytest.mark.skipif(os.name != "nt", reason="Windows process lock contract")
def test_windows_config_lock_contends_across_independent_processes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text("services: {}\n", encoding="utf-8")
    holder_script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from haniel.core.lifecycle_locks import ConfigTransactionLock",
            "with ConfigTransactionLock(Path(sys.argv[1]), timeout_seconds=2, operation='windows-holder'):",
            "    print('locked', flush=True)",
            "    sys.stdin.readline()",
        )
    )
    contender_script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from haniel.core.lifecycle_locks import ConfigTransactionLock, ConfigTransactionLockTimeout",
            "try:",
            "    with ConfigTransactionLock(Path(sys.argv[1]), timeout_seconds=float(sys.argv[2]), operation='windows-contender'):",
            "        print('acquired', flush=True)",
            "except ConfigTransactionLockTimeout as error:",
            "    print('timeout:' + str(error), flush=True)",
        )
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(config_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", contender_script, str(config_path), "0.2"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        assert blocked.returncode == 0
        assert blocked.stdout.startswith("timeout:CONFIG_LOCK_TIMEOUT:")
        assert "windows-holder" in blocked.stdout
    finally:
        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 0

    acquired = subprocess.run(
        [sys.executable, "-c", contender_script, str(config_path), "2"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert acquired.returncode == 0
    assert acquired.stdout.strip() == "acquired"


def test_named_config_lock_wait_allows_eight_second_clone_contention(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    config_path.write_text("services: {}\n", encoding="utf-8")
    entered = threading.Event()

    def hold_clone() -> None:
        with ConfigTransactionLock(config_path, operation="clone"):
            entered.set()
            time.sleep(8)

    holder = threading.Thread(target=hold_clone)
    holder.start()
    assert entered.wait(1)
    started_at = time.monotonic()
    try:
        with ConfigTransactionLock(config_path, operation="reload"):
            pass
    finally:
        holder.join(timeout=10)

    elapsed = time.monotonic() - started_at
    assert 7.5 <= elapsed < ConfigTransactionLock.DEFAULT_TIMEOUT_SECONDS


def test_config_file_lock_stress_has_no_starved_waiter(tmp_path: Path) -> None:
    from haniel.core.service_lifecycle import config_file_transaction

    config_path = tmp_path / "haniel.yaml"
    config_path.write_text("services: {}\n", encoding="utf-8")
    entered: list[int] = []
    failures: list[BaseException] = []
    start = threading.Event()

    def contend(index: int) -> None:
        start.wait()
        try:
            with config_file_transaction(
                config_path,
                operation=f"stress-{index}",
                timeout_seconds=3,
            ):
                entered.append(index)
                time.sleep(0.005)
        except BaseException as error:
            failures.append(error)

    workers = [threading.Thread(target=contend, args=(index,)) for index in range(12)]
    with config_file_transaction(config_path, operation="stress-holder"):
        for worker in workers:
            worker.start()
        start.set()
        time.sleep(0.05)
    for worker in workers:
        worker.join(timeout=4)

    assert failures == []
    assert sorted(entered) == list(range(12))
    assert all(not worker.is_alive() for worker in workers)


def _write_config(
    path: Path,
    *,
    env_file: Path,
    service_name: str,
    command: str = "node app.js",
    url: str = "https://example.invalid/app.git",
    ready: str = "delay:0.01",
) -> None:
    path.write_text(
        "\n".join(
            [
                "poll_interval: 60",
                "repos:",
                "  app:",
                f"    url: {json.dumps(url)}",
                "    branch: main",
                "    path: ./repo",
                "    release_manifest: deploy/release.json",
                "services:",
                f"  {service_name}:",
                f"    run: {json.dumps(command)}",
                "    cwd: ./repo",
                "    repo: app",
                f"    release_env_file: {json.dumps(str(env_file))}",
                f"    ready: {ready}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_config_write_lock_freezes_config_file_bytes_during_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config mutation cannot publish disk bytes during handover approval."""

    import haniel.core.service_lifecycle as lifecycle
    from haniel.config.io import write_config as actual_write_config

    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    original_bytes = config_path.read_bytes()
    writer_started = threading.Event()
    write_attempted = threading.Event()
    errors: list[BaseException] = []

    def observed_write_config(path: Path, config: HanielConfig) -> None:
        write_attempted.set()
        actual_write_config(path, config)

    def mutate_config() -> None:
        writer_started.set()
        try:
            lifecycle.register_repo(
                runner,
                name="extra",
                repo_config={
                    "url": "https://example.invalid/extra.git",
                    "path": "./extra",
                },
            )
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(lifecycle, "write_config", observed_write_config)
    with lifecycle.CONFIG_WRITE_LOCK:
        writer = threading.Thread(target=mutate_config, daemon=True)
        writer.start()
        assert writer_started.wait(timeout=1)
        assert not write_attempted.wait(timeout=0.2)
        assert config_path.read_bytes() == original_bytes

    writer.join(timeout=2)
    assert not writer.is_alive()
    assert errors == []
    assert write_attempted.is_set()
    assert config_path.read_bytes() != original_bytes
    assert "extra" in load_config(config_path).repos


def test_handover_config_keeps_writer_lock_through_resident_snapshot_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk identity and resident generation publish under one writer lock."""

    import haniel.core.handover_config as handover_config
    import haniel.core.service_lifecycle as lifecycle

    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    (tmp_path / "repo").mkdir()
    digest = handover_config.handover_config_digest(config_path)
    replace_entered = threading.Event()
    release_replace = threading.Event()
    competing_writer_entered = threading.Event()
    errors: list[BaseException] = []
    original_replace = runner._replace_config_snapshot

    def blocking_replace(candidate: HanielConfig, expected_generation: int) -> int:
        replace_entered.set()
        assert release_replace.wait(timeout=5)
        return original_replace(candidate, expected_generation)

    def prepare() -> None:
        try:
            runner.prepare_handover_config("app", digest)
        except BaseException as error:
            errors.append(error)

    def competing_writer() -> None:
        with lifecycle.CONFIG_WRITE_LOCK:
            competing_writer_entered.set()

    monkeypatch.setattr(runner, "_replace_config_snapshot", blocking_replace)
    handover = threading.Thread(target=prepare)
    handover.start()
    assert replace_entered.wait(timeout=2)
    writer = threading.Thread(target=competing_writer)
    writer.start()
    try:
        assert not competing_writer_entered.wait(timeout=0.2)
    finally:
        release_replace.set()
    handover.join(timeout=5)
    writer.join(timeout=5)

    assert not handover.is_alive()
    assert not writer.is_alive()
    assert errors == []
    assert competing_writer_entered.is_set()


def test_reload_union_stops_removed_writer_with_real_process_manager(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite://new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="new-writer")
    old_service = ServiceConfig(
        run=f"{sys.executable} sleeper.py", repo="app", ready="delay:0.01"
    )
    old_config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={"old-writer": old_service},
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "sleeper.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    runner = ServiceRunner(old_config, tmp_path, config_path=config_path)
    runner.process_manager.start_service("old-writer", old_service)
    try:
        plan = runner.prepare_handover_config(
            "app", module.handover_config_digest(config_path)
        )
        adapter = RunnerDeploymentAdapter(
            runner,
            "app",
            list(plan.new_affected),
            tmp_path / "repo",
            "a" * 40,
            "b" * 40,
            "request-1",
            quiesce_services=list(plan.quiesce_services),
            config_digest=plan.config_digest,
        )

        receipt = adapter.stop()

        assert receipt["quiesced_services"] == ["new-writer", "old-writer"]
        assert receipt["stopped_services"] == ["old-writer"]
        assert receipt["already_stopped_services"] == ["new-writer"]
        assert receipt["config_digest"] == plan.config_digest
        assert not runner.process_manager.is_running("old-writer")
    finally:
        runner.process_manager.stop_all()


def test_retry_reload_keeps_still_running_removed_writer_in_quiescence(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="new-writer")
    old_service = ServiceConfig(
        run=f"{sys.executable} sleeper.py", repo="app", ready="delay:0.01"
    )
    dependent_service = ServiceConfig(
        run=f"{sys.executable} sleeper.py",
        after=["old-writer"],
        ready="delay:0.01",
    )
    old_config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="https://example.invalid/app.git",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        },
        services={
            "old-writer": old_service,
            "old-dependent": dependent_service,
        },
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "sleeper.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    runner = ServiceRunner(old_config, tmp_path, config_path=config_path)
    runner.process_manager.start_service("old-writer", old_service)
    runner.process_manager.start_service("old-dependent", dependent_service)
    try:
        digest = module.handover_config_digest(config_path)
        first = runner.prepare_handover_config("app", digest)
        assert first.quiesce_services == (
            "new-writer",
            "old-dependent",
            "old-writer",
        )

        # The first attempt fails during detached staging, before quiescence.
        # The old direct writer can exit before the retry while its transitive
        # dependent remains live. The retry must derive the old graph from the
        # immutable process-start snapshots rather than using only currently
        # running services as graph seeds.
        assert runner.process_manager.stop_service("old-writer")
        assert not runner.process_manager.is_running("old-writer")
        assert runner.process_manager.is_running("old-dependent")
        retry = runner.prepare_handover_config("app", digest)

        assert retry.quiesce_services == (
            "new-writer",
            "old-dependent",
        )
        adapter = RunnerDeploymentAdapter(
            runner,
            "app",
            list(retry.new_affected),
            tmp_path / "repo",
            "a" * 40,
            "b" * 40,
            "request-retry",
            quiesce_services=list(retry.quiesce_services),
            config_digest=retry.config_digest,
        )
        receipt = adapter.stop()
        assert receipt["stopped_services"] == ["old-dependent"]
        assert not runner.process_manager.is_running("old-dependent")
        assert not runner.process_manager.is_running("old-writer")
    finally:
        runner.process_manager.stop_all()


def test_reload_cannot_apply_a_stale_snapshot_after_handover_reload(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=env_file,
        service_name="app",
        command="old-command",
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    real_load_config = load_config
    stale_loaded = threading.Event()
    allow_stale_reload = threading.Event()
    handover_finished = threading.Event()
    errors: list[BaseException] = []

    def paused_load(path: Path) -> HanielConfig:
        snapshot = real_load_config(path)
        stale_loaded.set()
        assert allow_stale_reload.wait(timeout=5)
        return snapshot

    def reload() -> None:
        try:
            runner.reload_config()
        except BaseException as error:
            errors.append(error)

    def handover_reload() -> None:
        try:
            digest = module.handover_config_digest(config_path)
            runner.prepare_handover_config("app", digest)
        except BaseException as error:
            errors.append(error)
        finally:
            handover_finished.set()

    with patch("haniel.config.load_config", side_effect=paused_load):
        reload_thread = threading.Thread(target=reload)
        reload_thread.start()
        assert stale_loaded.wait(timeout=2)
        _write_config(
            config_path,
            env_file=env_file,
            service_name="app",
            command="new-command",
        )
        handover_thread = threading.Thread(target=handover_reload)
        handover_thread.start()
        # A correct reload holds the same lock while reading, so the handover
        # cannot apply the new snapshot until the paused old read is committed.
        assert not handover_finished.wait(timeout=0.1)
        allow_stale_reload.set()
        reload_thread.join(timeout=5)
        handover_thread.join(timeout=5)

    assert not reload_thread.is_alive()
    assert not handover_thread.is_alive()
    assert errors == []
    assert runner.config.services["app"].run == "new-command"
    assert module.handover_config_digest(config_path) == (
        runner.prepare_handover_config(
            "app", module.handover_config_digest(config_path)
        ).config_digest
    )


def test_service_definition_reload_cannot_apply_stale_partial_config(
    tmp_path: Path,
) -> None:
    from haniel.config.io import read_config as real_read_config
    from haniel.core.service_lifecycle import reload_service_definition

    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=env_file,
        service_name="app",
        command="old-command",
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    stale_loaded = threading.Event()
    allow_stale_reload = threading.Event()
    handover_finished = threading.Event()
    errors: list[BaseException] = []

    def paused_read(path: Path) -> HanielConfig:
        snapshot = real_read_config(path)
        stale_loaded.set()
        assert allow_stale_reload.wait(timeout=5)
        return snapshot

    def reload_service() -> None:
        try:
            reload_service_definition(runner, "app")
        except BaseException as error:
            errors.append(error)

    def handover_reload() -> None:
        try:
            digest = module.handover_config_digest(config_path)
            runner.prepare_handover_config("app", digest)
        except BaseException as error:
            errors.append(error)
        finally:
            handover_finished.set()

    with (
        patch("haniel.core.service_lifecycle.read_config", side_effect=paused_read),
        patch(
            "haniel.core.service_lifecycle._start_enabled_service",
            return_value=True,
        ),
    ):
        service_thread = threading.Thread(target=reload_service)
        service_thread.start()
        assert stale_loaded.wait(timeout=2)
        _write_config(
            config_path,
            env_file=env_file,
            service_name="app",
            command="new-command",
        )
        handover_thread = threading.Thread(target=handover_reload)
        handover_thread.start()
        assert not handover_finished.wait(timeout=0.1)
        allow_stale_reload.set()
        service_thread.join(timeout=5)
        handover_thread.join(timeout=5)

    assert not service_thread.is_alive()
    assert not handover_thread.is_alive()
    assert errors == []
    assert runner.config.services["app"].run == "new-command"


def test_request_bound_env_digest_rejects_swap_between_config_check_and_resolve(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    digest = module.handover_config_digest(config_path)
    plan = runner.prepare_handover_config("app", digest)
    manifest = ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "release-bound-env",
            "environment_service": "app",
            "requires_service_env_file": True,
            "post_start_verify": [{"name": "health", "command": "true"}],
            "recovery": {
                "strategy": "rollback",
                "command": {"name": "restore", "command": "true"},
            },
        }
    )
    original_check = module.require_handover_config_digest

    def check_then_swap(path: Path, expected: str) -> None:
        original_check(path, expected)
        replacement = tmp_path / "replacement.env"
        replacement.write_text("DATABASE_URL=sqlite:///unapproved\n", encoding="utf-8")
        replacement.replace(env_file)

    with patch(
        "haniel.core.one_shot_handover.require_handover_config_digest",
        side_effect=check_then_swap,
    ):
        with pytest.raises(ValueError, match="SERVICE_ENV_FILE_CHANGED"):
            _resolve_bound_manifest_environment(
                runner,
                "app",
                ["app"],
                manifest,
                digest,
                plan.service_environment_map(),
            )


def test_config_drift_after_probe_fails_before_live_checkout(tmp_path: Path) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///first\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    runner.lifecycle_instance_id = "owner-1"
    (tmp_path / "repo").mkdir()
    digest = module.handover_config_digest(config_path)
    control = LifecycleControl(config_path)
    control.submit_request(
        "request-1",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
            "config_digest": digest,
        },
    )
    staged = MagicMock(
        target_head="b" * 40,
        manifest_digest="c" * 64,
        manifest=MagicMock(release_id="release-1"),
    )

    def drift_after_probe(*_args, **_kwargs):
        env_file.write_text("DATABASE_URL=sqlite:///changed\n", encoding="utf-8")
        return staged

    with (
        patch("haniel.core.one_shot_handover.get_head", return_value="a" * 40),
        patch(
            "haniel.core.one_shot_handover.probe_manifest_target",
            side_effect=drift_after_probe,
        ),
        patch("haniel.core.one_shot_handover.activate_repo_target") as activate,
    ):
        result = execute_owner_handover(
            runner,
            control=control,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            config_digest=digest,
        )

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "CONFIG_DIGEST_MISMATCH"
    activate.assert_not_called()
    journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read("app")
    assert journal is not None
    assert journal["config_digest"] == digest


def test_one_shot_recovery_failure_uses_same_code_in_result_and_journal(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config = HanielConfig(
        repos={
            "app": RepoConfig(
                url="unused",
                path="./repo",
                release_manifest="deploy/release.json",
            )
        }
    )
    runner = ServiceRunner(config, tmp_path, config_path=config_path)
    control = LifecycleControl(config_path)
    control.submit_request(
        "request-recovery-failed",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "target",
            "expected_operation": "upgrade",
        },
    )
    staged = MagicMock(
        target_head="b" * 40,
        manifest_digest="c" * 64,
        manifest=MagicMock(release_id="release-1"),
    )

    with (
        patch(
            "haniel.core.one_shot_handover.get_head",
            side_effect=["a" * 40, "b" * 40],
        ),
        patch(
            "haniel.core.one_shot_handover.probe_manifest_target",
            return_value=staged,
        ),
        patch("haniel.core.one_shot_handover.activate_repo_target", return_value=[]),
        patch(
            "haniel.core.one_shot_handover.run_manifest_deployment",
            side_effect=RuntimeError("programming defect"),
        ),
        patch(
            "haniel.core.one_shot_handover.reset_repo_to",
            side_effect=DeploymentCommandError(
                "COMMAND_TIMEOUT", "reset", "reset timed out"
            ),
        ),
    ):
        result = execute_owner_handover(
            runner,
            control=control,
            repo_name="app",
            target_ref="target",
            expected_operation="upgrade",
            request_id="request-recovery-failed",
        )

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "RECOVERY_FAILED"
    journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read("app")
    assert journal is not None
    assert journal["state"] == "failed"
    assert journal["error_code"] == "RECOVERY_FAILED"


def test_one_shot_programming_runtime_error_escapes_owner_boundary(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = ServiceRunner(
        HanielConfig(
            repos={
                "app": RepoConfig(
                    url="unused",
                    path="./repo",
                    release_manifest="deploy/release.json",
                )
            }
        ),
        tmp_path,
        config_path=config_path,
    )
    control = LifecycleControl(config_path)
    control.submit_request(
        "request-programming-error",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "target",
            "expected_operation": "upgrade",
        },
    )

    with (
        patch("haniel.core.one_shot_handover.get_head", return_value="a" * 40),
        patch(
            "haniel.core.one_shot_handover.probe_manifest_target",
            side_effect=RuntimeError("programming defect"),
        ),
    ):
        with pytest.raises(RuntimeError, match="programming defect"):
            execute_owner_handover(
                runner,
                control=control,
                repo_name="app",
                target_ref="target",
                expected_operation="upgrade",
                request_id="request-programming-error",
            )


def test_config_bound_runtime_without_snapshot_fails_before_service_start(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///first\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=env_file,
        service_name="app",
        command=f"{sys.executable} sleeper.py",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "sleeper.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    digest = module.handover_config_digest(config_path)
    adapter = RunnerDeploymentAdapter(
        runner,
        "app",
        ["app"],
        tmp_path / "repo",
        "a" * 40,
        "b" * 40,
        "request-1",
        config_digest=digest,
    )
    env_file.write_text("DATABASE_URL=sqlite:///changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="SERVICE_ENV_FILE_CHANGED"):
        adapter.start_and_wait({"app"})

    assert not runner.process_manager.is_running("app")


def test_bound_runtime_uses_approved_snapshot_after_source_swap(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    observed = tmp_path / "runtime-database.txt"
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=env_file,
        service_name="app",
        command=f"{sys.executable} sleeper.py {observed}",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "sleeper.py").write_text(
        (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(os.environ['DATABASE_URL'])\n"
            "time.sleep(60)\n"
        ),
        encoding="utf-8",
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    digest = module.handover_config_digest(config_path)
    plan = runner.prepare_handover_config("app", digest)
    adapter = RunnerDeploymentAdapter(
        runner,
        "app",
        ["app"],
        tmp_path / "repo",
        "a" * 40,
        "b" * 40,
        "request-1",
        config_digest=digest,
        service_environment_bindings=plan.service_environment_map(),
    )
    replacement = tmp_path / "replacement-runtime.env"
    replacement.write_text("DATABASE_URL=sqlite:///unapproved\n", encoding="utf-8")
    replacement.replace(env_file)
    try:
        adapter.start_and_wait({"app"})
        deadline = time.monotonic() + 5
        while not observed.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observed.read_text(encoding="utf-8") == "sqlite:///approved"
        assert runner.process_manager.is_running("app")
    finally:
        runner.process_manager.stop_all()


@pytest.mark.parametrize("changed_field", ["url", "branch", "path"])
def test_reload_rejects_unbound_existing_checkout_fetch_identity(
    tmp_path: Path,
    changed_field: str,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    content = config_path.read_text(encoding="utf-8")
    if changed_field == "url":
        content = content.replace(
            "https://example.invalid/app.git", "https://example.invalid/other.git"
        )
    else:
        if changed_field == "branch":
            content = content.replace("    branch: main", "    branch: release")
        else:
            content = content.replace("    path: ./repo", "    path: ./other-repo")
    config_path.write_text(content, encoding="utf-8")
    digest = module.handover_config_digest(config_path)

    with pytest.raises(RuntimeError, match="CONFIG_RELOAD_UNSAFE"):
        runner.prepare_handover_config("app", digest)

    assert runner._repo_states["app"].config.url == "https://example.invalid/app.git"
    assert runner._repo_states["app"].config.branch == "main"


def test_reload_accepts_same_canonical_repo_and_manifest_path(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    path: ./repo", f"    path: {json.dumps(str(tmp_path / 'repo'))}"
        ),
        encoding="utf-8",
    )
    digest = module.handover_config_digest(config_path)

    plan = runner.prepare_handover_config("app", digest)

    assert plan.config_digest == digest
    assert runner._repo_states["app"].config.path == str(tmp_path / "repo")


@pytest.mark.skipif(os.name != "nt", reason="Windows canonical path semantics")
def test_windows_mixed_case_bound_env_path_is_same_identity(tmp_path: Path) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "Service.Env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    mixed_case_path = Path(str(env_file).swapcase())
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=mixed_case_path, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    digest = module.handover_config_digest(config_path)
    plan = runner.prepare_handover_config("app", digest)
    binding = plan.service_environment_map()["app"]
    service = runner._enabled_services["app"]
    manifest = ReleaseManifest.model_validate(
        {
            "schema_version": "haniel.release.v1",
            "release_id": "windows-path",
            "environment_service": "app",
            "requires_service_env_file": True,
            "post_start_verify": [{"name": "health", "command": "true"}],
            "recovery": {
                "strategy": "rollback",
                "command": {"name": "restore", "command": "true"},
            },
        }
    )

    child_environment = _resolve_bound_manifest_environment(
        runner,
        "app",
        ["app"],
        manifest,
        digest,
        plan.service_environment_map(),
    )

    environment = service_process_environment(
        tmp_path,
        service,
        expected_env_path=binding.path,
        expected_env_sha256=binding.sha256,
    )

    assert child_environment["HANIEL_SERVICE_ENV_FILE_SHA256"] == binding.sha256
    assert environment["DATABASE_URL"] == "sqlite:///approved"


@pytest.mark.parametrize(
    "unsupported_yaml",
    [
        "shutdown:\n  timeout: 99\n",
        "backoff:\n  base_delay: 99\n",
        "dashboard:\n  enabled: false\n",
        "mcp:\n  enabled: false\n",
        "install:\n  directories:\n    - ./new-dir\n",
        "webhooks:\n  - url: https://example.invalid/hook\n",
        (
            "orchestrator_client:\n"
            "  enabled: true\n"
            "  url: wss://example.invalid/ws\n"
            "  token: placeholder\n"
            "  node_id: node-1\n"
        ),
    ],
)
def test_handover_reload_rejects_config_owned_by_initialized_subsystems(
    tmp_path: Path,
    unsupported_yaml: str,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="app")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    original_config = runner.config
    config_path.write_text(
        unsupported_yaml + config_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    digest = module.handover_config_digest(config_path)

    with pytest.raises(RuntimeError, match="CONFIG_RELOAD_UNSAFE"):
        runner.prepare_handover_config("app", digest)

    assert runner.config is original_config


@pytest.mark.parametrize("reader_kind", ["affected-services", "orchestrator-plan"])
def test_config_reload_reader_sees_old_snapshot_until_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
) -> None:
    runner_module = importlib.import_module("haniel.core.runner")
    config_module = importlib.import_module("haniel.core.handover_config")
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///new\n", encoding="utf-8")
    config_path = tmp_path / "haniel.yaml"
    _write_config(config_path, env_file=env_file, service_name="old-writer")
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    _write_config(config_path, env_file=env_file, service_name="new-writer")
    digest = config_module.handover_config_digest(config_path)
    original_graph = runner_module.DependencyGraph
    graph_started = threading.Event()
    allow_graph = threading.Event()
    reader_finished = threading.Event()
    observed: dict[str, object] = {}

    def blocking_graph(services):
        graph_started.set()
        assert allow_graph.wait(timeout=5)
        return original_graph(services)

    monkeypatch.setattr(runner_module, "DependencyGraph", blocking_graph)
    reload_thread = threading.Thread(
        target=lambda: runner.prepare_handover_config("app", digest)
    )
    reload_thread.start()
    assert graph_started.wait(timeout=5)

    def read_affected() -> None:
        if reader_kind == "affected-services":
            observed["affected"] = runner.get_affected_services("app")
        else:
            observed["planner"] = runner._deploy_retry_planner("app")
        reader_finished.set()

    reader_thread = threading.Thread(target=read_affected)
    reader_thread.start()
    reader_finished_before_commit = reader_finished.wait(timeout=1)
    allow_graph.set()
    reload_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    assert not reload_thread.is_alive()
    assert not reader_thread.is_alive()
    assert reader_finished_before_commit
    if reader_kind == "affected-services":
        assert observed["affected"] == ["old-writer"]
        assert runner.get_affected_services("app") == ["new-writer"]
    else:
        assert observed["planner"] is not None


def test_release_child_reads_immutable_env_snapshot_after_source_swap(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///approved\n", encoding="utf-8")
    expected_digest = read_service_environment_file(env_file).sha256
    ready = tmp_path / "ready"
    proceed = tmp_path / "proceed"
    script = tmp_path / "read_after_swap.py"
    script.write_text(
        """
import json
import hashlib
import os
import sys
import time
from pathlib import Path

ready = Path(sys.argv[1])
proceed = Path(sys.argv[2])
ready.write_text("ready", encoding="utf-8")
while not proceed.exists():
    time.sleep(0.01)
content = Path(os.environ["HANIEL_SERVICE_ENV_FILE"]).read_text(encoding="utf-8")
print(json.dumps({"content_sha256": hashlib.sha256(content.encode()).hexdigest()}))
""".lstrip(),
        encoding="utf-8",
    )
    outcome: dict[str, object] = {}

    def execute() -> None:
        try:
            outcome["result"] = subprocess_command_runner(tmp_path)(
                CommandSpec(
                    name="immutable-env",
                    command=(
                        f"{sys.executable} {script.name} {ready.name} {proceed.name}"
                    ),
                ),
                {
                    "HANIEL_SERVICE_ENV_FILE": str(env_file),
                    "HANIEL_SERVICE_ENV_FILE_SHA256": expected_digest,
                },
            )
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    replacement = tmp_path / "replacement.env"
    replacement.write_text("DATABASE_URL=sqlite:///unapproved\n", encoding="utf-8")
    replacement.replace(env_file)
    proceed.write_text("go", encoding="utf-8")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in outcome
    result = outcome["result"]
    assert result.json_data == {  # type: ignore[union-attr]
        "content_sha256": hashlib.sha256(
            b"DATABASE_URL=sqlite:///approved\n"
        ).hexdigest()
    }


@pytest.mark.parametrize("swap_before_live_phase", [False, True])
def test_resident_spool_reload_uses_new_env_and_quiesces_old_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_before_live_phase: bool,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    repo = tmp_path / "repo"
    deploy = repo / "deploy"
    deploy.mkdir(parents=True)
    old_database = tmp_path / "old-secret.sqlite"
    new_database = tmp_path / "new-secret.sqlite"
    runtime_database = tmp_path / "runtime-database.txt"
    old_env = tmp_path / "old-service.env"
    new_env = tmp_path / "new-service.env"
    old_env.write_text(f"DATABASE_URL={old_database}\n", encoding="utf-8")
    new_env.write_text(f"DATABASE_URL={new_database}\n", encoding="utf-8")
    (repo / ".env.soul-server-ts").write_text(
        f"DATABASE_URL={old_database}\n", encoding="utf-8"
    )
    (repo / "sleeper.py").write_text(
        """
import os
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(os.environ["DATABASE_URL"], encoding="utf-8")
time.sleep(60)
""".lstrip(),
        encoding="utf-8",
    )
    (repo / "release_phase.py").write_text(
        """
import json
import os
import sqlite3
import sys
from pathlib import Path

if "DATABASE_URL" in os.environ:
    raise SystemExit(31)
env_file = Path(os.environ["HANIEL_SERVICE_ENV_FILE"])
database = Path(env_file.read_text(encoding="utf-8").split("=", 1)[1].strip())
phase = sys.argv[1]
if phase == "preflight":
    print(json.dumps({
        "schema_version": "soulstream.database-release.v1",
        "ok": True,
        "operation": "upgrade",
        "journal_path": str(Path(os.environ["HANIEL_DEPLOYMENT_JOURNAL"]).with_name("database-release.json")),
    }))
elif phase == "apply":
    with sqlite3.connect(database) as connection:
        connection.execute("create table release_ledger (id text primary key)")
        connection.execute("insert into release_ledger values ('expected')")
    print(json.dumps({"ok": True, "phase": "applied"}))
elif phase == "health":
    with sqlite3.connect(database) as connection:
        assert connection.execute("select id from release_ledger").fetchall() == [("expected",)]
    print(json.dumps({"ok": True, "phase": "verified"}))
else:
    print(json.dumps({"ok": True, "phase": phase}))
""".lstrip(),
        encoding="utf-8",
    )
    python = json.dumps(sys.executable)
    command = lambda phase: {  # noqa: E731 - compact manifest fixture builder
        "name": phase,
        "command": f"{python} release_phase.py {phase}",
    }
    manifest = {
        "schema_version": "haniel.release.v1",
        "release_id": "release-config-env-contract",
        "environment_service": "new-writer",
        "requires_service_env_file": True,
        "migration": {
            "destructive": True,
            "operation": "discover",
            "result_contract": "soulstream.database-release.v1",
            "preflight": command("preflight"),
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
            "apply": command("apply"),
        },
        "post_start_verify": [command("health")],
        "recovery": {"strategy": "rollback", "command": command("restore")},
    }
    (deploy / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Haniel Test",
            "-c",
            "user.email=haniel@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    config_path = tmp_path / "haniel.yaml"
    sleeper = f"{sys.executable} sleeper.py {runtime_database}"
    _write_config(
        config_path,
        env_file=old_env,
        service_name="old-writer",
        command=sleeper,
        url=str(remote),
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    runner.process_manager.start_service(
        "old-writer", runner._enabled_services["old-writer"]
    )
    _write_config(
        config_path,
        env_file=new_env,
        service_name="new-writer",
        command=sleeper,
        url=str(remote),
    )
    digest = module.handover_config_digest(config_path)
    request_id = "request-config-env"
    runner.lifecycle_instance_id = "owner-1"
    runner.lifecycle_control.submit_request(
        request_id,
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
            "config_digest": digest,
        },
    )
    monkeypatch.setenv("DATABASE_URL", str(old_database))
    server = LifecycleRequestServer(
        control=runner.lifecycle_control,
        runner=runner,
        instance_id="owner-1",
    )
    if swap_before_live_phase:
        deployment_module = importlib.import_module("haniel.core.runner_deployment")
        original_require = deployment_module.require_handover_config_digest
        swapped = False

        def check_then_swap(path: Path, expected: str) -> None:
            nonlocal swapped
            original_require(path, expected)
            if swapped:
                return
            swapped = True
            replacement = tmp_path / "live-replacement.env"
            replacement.write_text(
                f"DATABASE_URL={tmp_path / 'unapproved.sqlite'}\n",
                encoding="utf-8",
            )
            replacement.replace(new_env)

        monkeypatch.setattr(
            deployment_module,
            "require_handover_config_digest",
            check_then_swap,
        )
    try:
        with runner.lifecycle_control.acquire_owner("owner-1"):
            server.handle_request(request_id)
        result = runner.lifecycle_control.read_result(request_id)

        if swap_before_live_phase:
            terminal = result["terminal"]
            assert terminal["ok"] is False
            assert terminal["error"]["code"] == "CONFIG_DIGEST_MISMATCH"
            assert runner.process_manager.is_running("old-writer")
            assert not new_database.exists()
            assert not (tmp_path / "unapproved.sqlite").exists()
            return

        assert result["terminal"]["ok"] is True
        assert result["terminal"]["config_digest"] == digest
        assert not runner.process_manager.is_running("old-writer")
        assert runner.process_manager.is_running("new-writer")
        assert runtime_database.read_text(encoding="utf-8") == str(new_database)
        assert not old_database.exists()
        with sqlite3.connect(new_database) as connection:
            assert connection.execute("select id from release_ledger").fetchall() == [
                ("expected",)
            ]
        request = json.loads(
            runner.lifecycle_control.request_path(request_id).read_text(
                encoding="utf-8"
            )
        )
        journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read("app")
        serialized = json.dumps(
            {"request": request, "result": result, "journal": journal}
        )
        assert journal is not None
        assert journal["config_digest"] == digest
        assert journal["quiescence_receipt"]["quiesced_services"] == [
            "new-writer",
            "old-writer",
        ]
        assert journal["quiescence_receipt"]["config_digest"] == digest
        assert "old-secret" not in serialized
        assert "new-secret" not in serialized
    finally:
        runner.process_manager.stop_all()


def test_upgrade_recovery_uses_approved_env_after_apply_source_drift(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("haniel.core.handover_config")
    repo = tmp_path / "repo"
    deploy = repo / "deploy"
    deploy.mkdir(parents=True)
    database = tmp_path / "release.sqlite"
    backup = tmp_path / "release.backup.sqlite"
    env_file = tmp_path / "service.env"
    runtime_database = tmp_path / "runtime-database.txt"
    apply_started = tmp_path / "apply-started"
    continue_apply = tmp_path / "continue-apply"
    env_file.write_text(f"DATABASE_URL={database}\n", encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute("create table release_ledger (value text not null)")
        connection.execute("insert into release_ledger values ('old')")
    (repo / "app.py").write_text(
        (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(os.environ['DATABASE_URL'])\n"
            "time.sleep(60)\n"
        ),
        encoding="utf-8",
    )
    (repo / "release_phase.py").write_text(
        f"""
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

env_file = Path(os.environ["HANIEL_SERVICE_ENV_FILE"])
database = Path(env_file.read_text(encoding="utf-8").split("=", 1)[1].strip())
backup = Path({str(backup)!r})
apply_started = Path({str(apply_started)!r})
continue_apply = Path({str(continue_apply)!r})
phase = sys.argv[1]
if phase == "preflight":
    print(json.dumps({{
        "schema_version": "soulstream.database-release.v1",
        "ok": True,
        "operation": "upgrade",
        "journal_path": str(database.with_name("database-release.json")),
    }}))
elif phase == "backup":
    shutil.copy2(database, backup)
    print(json.dumps({{"ok": True, "phase": "backup_created"}}))
elif phase == "verify-backup":
    assert backup.is_file()
    print(json.dumps({{"ok": True, "phase": "backup_verified"}}))
elif phase == "apply":
    with sqlite3.connect(database) as connection:
        connection.execute("update release_ledger set value = 'new'")
    apply_started.write_text("ready", encoding="utf-8")
    while not continue_apply.exists():
        time.sleep(0.01)
    print(json.dumps({{"ok": True, "phase": "applied"}}))
elif phase == "restore":
    shutil.copy2(backup, database)
    print(json.dumps({{"ok": True, "phase": "recovered"}}))
elif phase == "health":
    print(json.dumps({{"ok": True, "phase": "verified"}}))
""".lstrip(),
        encoding="utf-8",
    )
    python = json.dumps(sys.executable)
    command = lambda phase: {  # noqa: E731 - compact manifest fixture builder
        "name": phase,
        "command": f"{python} release_phase.py {phase}",
    }
    manifest = {
        "schema_version": "haniel.release.v1",
        "release_id": "release-recovery-env-snapshot",
        "environment_service": "app",
        "requires_service_env_file": True,
        "migration": {
            "destructive": True,
            "operation": "discover",
            "result_contract": "soulstream.database-release.v1",
            "preflight": command("preflight"),
            "backup": command("backup"),
            "verify_backup": command("verify-backup"),
            "apply": command("apply"),
        },
        "post_start_verify": [command("health")],
        "recovery": {"strategy": "rollback", "command": command("restore")},
    }
    (deploy / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Haniel Test",
            "-c",
            "user.email=haniel@example.invalid",
            "commit",
            "-m",
            "working release",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    previous_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config_path = tmp_path / "haniel.yaml"
    _write_config(
        config_path,
        env_file=env_file,
        service_name="app",
        command=f"{sys.executable} app.py {runtime_database}",
        url=str(repo),
        ready="delay:1",
    )
    runner = ServiceRunner(load_config(config_path), tmp_path, config_path=config_path)
    runner.lifecycle_instance_id = "owner-recovery"
    runner.process_manager.start_service("app", runner._enabled_services["app"])
    (repo / "app.py").write_text("raise SystemExit(23)\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Haniel Test",
            "-c",
            "user.email=haniel@example.invalid",
            "commit",
            "-m",
            "failing target",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    target_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = module.handover_config_digest(config_path)
    plan = runner.prepare_handover_config("app", digest)
    errors: list[BaseException] = []

    def deploy_release() -> None:
        try:
            run_manifest_deployment(
                runner,
                "app",
                ["app"],
                previous_head,
                expected_operation="upgrade",
                request_id="request-recovery",
                quiesce_services=["app"],
                config_digest=digest,
                service_environment_bindings=plan.service_environment_map(),
            )
        except BaseException as error:
            errors.append(error)

    try:
        with runner.lifecycle_control.acquire_owner("owner-recovery"):
            worker = threading.Thread(target=deploy_release)
            worker.start()
            deadline = time.monotonic() + 30
            while not apply_started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert apply_started.exists()
            replacement = tmp_path / "replacement-after-apply.env"
            replacement.write_text(
                f"DATABASE_URL={tmp_path / 'unapproved.sqlite'}\n",
                encoding="utf-8",
            )
            replacement.replace(env_file)
            continue_apply.write_text("go", encoding="utf-8")
            worker.join(timeout=20)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert getattr(errors[0], "recovered", False) is True
        assert (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == previous_head
        )
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "select value from release_ledger"
            ).fetchone() == ("old",)
        assert runner.process_manager.is_running("app")
        assert runtime_database.read_text(encoding="utf-8") == str(database)
        assert not (tmp_path / "unapproved.sqlite").exists()
        journal = DeploymentStateStore(tmp_path / ".haniel" / "deployments").read("app")
        assert journal is not None
        assert journal["state"] == "failed"
        assert journal["recovered"] is True
        assert journal["target_head"] == target_head
    finally:
        continue_apply.touch(exist_ok=True)
        runner.process_manager.stop_all()
