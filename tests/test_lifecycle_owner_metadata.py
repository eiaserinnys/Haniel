"""Resident owner lease and identity sidecar concurrency contracts."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from haniel.core.lifecycle_control import LifecycleControl
from haniel.core.lifecycle_locks import FileLease, LifecycleConflict
from haniel.core.lifecycle_storage import atomic_json
from haniel.core.one_shot_handover import execute_manifest_handover_once
from haniel.core.runner_deployment_identity import require_resident_owner


def _hold_owner(config_path: str, ready, release) -> None:
    with LifecycleControl(Path(config_path)).acquire_owner("process-owner"):
        ready.set()
        release.wait(timeout=10)


def _deny_lifetime_lock_read(control: LifecycleControl):
    original = Path.read_text

    def read_text(path: Path, *args, **kwargs):
        if path == control.owner_lock_path:
            raise PermissionError(13, "lifetime lock is not readable")
        return original(path, *args, **kwargs)

    return patch.object(Path, "read_text", read_text)


def test_active_owner_consumers_use_metadata_when_lifetime_lock_is_not_readable(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)

    with control.acquire_owner("instance-a"), _deny_lifetime_lock_read(control):
        assert control.read_active_owner()["instance_id"] == "instance-a"

        stop_request = control.request_stop(
            expected_instance="instance-a", request_id="stop-1"
        )
        assert stop_request.attached is False

        require_resident_owner(control, "instance-a", "runtime-1")

        with pytest.raises(TimeoutError, match="REQUEST_TIMEOUT"):
            execute_manifest_handover_once(
                config_path=config,
                repo_name="app",
                target_ref="origin/main",
                expected_operation="upgrade",
                request_id="timeout-1",
                start_owner=False,
                wait_timeout=0,
            )

    assert control.read_result("timeout-1")["terminal"]["error"]["code"] == (
        "REQUEST_TIMEOUT"
    )


def test_active_owner_consumers_accept_unsupported_process_start_lookup(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)

    with (
        control.acquire_owner("instance-a"),
        patch(
            "haniel.core.lifecycle_control.process_start_identity", return_value=None
        ),
    ):
        assert control.read_active_owner()["instance_id"] == "instance-a"
        control.request_stop(expected_instance="instance-a", request_id="stop-1")
        require_resident_owner(control, "instance-a", "runtime-1")
        with pytest.raises(TimeoutError, match="REQUEST_TIMEOUT"):
            execute_manifest_handover_once(
                config_path=config,
                repo_name="app",
                target_ref="origin/main",
                expected_operation="upgrade",
                request_id="timeout-1",
                start_owner=False,
                wait_timeout=0,
            )

    assert control.read_result("timeout-1")["terminal"]["error"]["code"] == (
        "REQUEST_TIMEOUT"
    )


def test_reader_waits_for_new_metadata_during_owner_acquisition(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.owner_path.parent.mkdir(parents=True)
    atomic_json(
        control.owner_path,
        {
            "schema_version": "haniel.lifecycle.owner.v1",
            "config_identity": control.identity,
            "instance_id": "old-owner",
            "pid": 1,
            "process_start_identity": "old-process",
        },
    )
    write_started = threading.Event()
    allow_write = threading.Event()
    original_write = control._write_owner

    def blocking_write(instance_id: str):
        write_started.set()
        assert allow_write.wait(timeout=2)
        return original_write(instance_id)

    with patch.object(control, "_write_owner", side_effect=blocking_write):
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner_future = pool.submit(control.acquire_owner, "new-owner")
            assert write_started.wait(timeout=2)
            reader_future = pool.submit(control.read_active_owner)
            time.sleep(0.05)
            assert not reader_future.done()
            allow_write.set()
            owner = owner_future.result(timeout=2)
            try:
                assert reader_future.result(timeout=2)["instance_id"] == "new-owner"
            finally:
                owner.__exit__(None, None, None)


def test_shutdown_does_not_remove_same_instance_metadata_from_new_process(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    owner = control.acquire_owner("shared-instance")
    replacement = {
        **owner.metadata(),
        "pid": os.getpid() + 1,
        "process_start_identity": "replacement-process-start",
    }
    atomic_json(control.owner_path, replacement)

    owner.__exit__(None, None, None)

    assert control.read_owner() == replacement


def test_shutdown_does_not_remove_different_instance_metadata(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    owner = control.acquire_owner("old-instance")
    replacement = {**owner.metadata(), "instance_id": "new-instance"}
    atomic_json(control.owner_path, replacement)

    owner.__exit__(None, None, None)

    assert control.read_owner() == replacement


def test_shutdown_does_not_remove_different_config_metadata(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    owner = control.acquire_owner("instance-a")
    replacement = {**owner.metadata(), "config_identity": "different-config"}
    atomic_json(control.owner_path, replacement)

    owner.__exit__(None, None, None)

    assert control.read_owner() == replacement


def test_shutdown_removes_exact_owner_metadata(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_owner("instance-a"):
        assert control.owner_path.exists()

    assert not control.owner_path.exists()


def test_windows_general_permission_error_is_not_a_lease_conflict(
    tmp_path: Path,
) -> None:
    denied = PermissionError(13, "ACL denied")
    denied.winerror = 5

    with (
        patch("haniel.core.lifecycle_locks.os.name", "nt"),
        patch.object(Path, "touch", side_effect=denied),
        pytest.raises(PermissionError, match="ACL denied"),
    ):
        FileLease(tmp_path / "denied.lock", "owner", "LEASE_CONFLICT")


@pytest.mark.parametrize("winerror", [32, 33])
def test_windows_sharing_and_lock_violations_are_stable_lease_conflicts(
    tmp_path: Path, winerror: int
) -> None:
    contention = PermissionError(13, "sharing conflict")
    contention.winerror = winerror

    with (
        patch("haniel.core.lifecycle_locks.os.name", "nt"),
        patch.object(Path, "touch", side_effect=contention),
        pytest.raises(LifecycleConflict, match="OS lease is already held"),
    ):
        FileLease(tmp_path / f"contention-{winerror}.lock", "owner", "LEASE_CONFLICT")


def test_cross_process_reader_uses_owner_sidecar_while_lifetime_lock_is_held(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    config = tmp_path / "haniel.yaml"
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_owner, args=(str(config), ready, release))
    process.start()
    try:
        assert ready.wait(timeout=5)
        metadata = LifecycleControl(config).read_active_owner()
        assert metadata["instance_id"] == "process-owner"
        assert metadata["pid"] == process.pid
        assert metadata["process_start_identity"]
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
    assert process.exitcode == 0
