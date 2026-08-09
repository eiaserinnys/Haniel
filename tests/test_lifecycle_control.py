"""Resident owner, request spool, deployment lease, and instance-safe stop."""

import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from haniel.core.lifecycle_control import (
    LifecycleConflict,
    LifecycleControl,
    config_identity,
)
from haniel.core.lifecycle_request_server import LifecycleRequestServer
from haniel.core.one_shot_handover import (
    _start_resident_owner,
    execute_manifest_handover_once,
)


def _submit_in_process(
    config_path: str,
    request_id: str,
    payload: dict[str, str],
    start,
    outcomes,
) -> None:
    start.wait()
    try:
        submission = LifecycleControl(Path(config_path)).submit_request(
            request_id, payload
        )
        outcomes.put(("ok", submission.attached))
    except Exception as error:
        outcomes.put(("error", type(error).__name__, str(error)))


def _ack_in_process(
    config_path: str,
    request_id: str,
    state: str,
    detail: dict[str, object],
    start,
    outcomes,
) -> None:
    start.wait()
    try:
        LifecycleControl(Path(config_path)).ack(request_id, state, detail)
        outcomes.put(("ok", state))
    except Exception as error:
        outcomes.put(("error", type(error).__name__, str(error)))


def _run_processes(context, processes, start, outcomes) -> list[tuple]:
    for process in processes:
        process.start()
    start.set()
    results = [outcomes.get(timeout=5) for _process in processes]
    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        assert process.exitcode == 0
    return results


def test_config_identity_is_canonical_path_sha256(tmp_path: Path) -> None:
    config = tmp_path / "nested" / ".." / "haniel.yaml"
    same = tmp_path / "haniel.yaml"

    assert config_identity(config) == config_identity(same)
    assert len(config_identity(config)) == 64


def test_os_owner_lock_is_exclusive_and_metadata_is_instance_bound(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_owner("instance-a") as owner:
        assert owner.metadata()["instance_id"] == "instance-a"
        with pytest.raises(LifecycleConflict, match="LIFECYCLE_OWNER_CONFLICT"):
            control.acquire_owner("instance-b")


def test_stale_owner_metadata_is_archived_only_after_os_lease_is_acquired(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.owner_path.parent.mkdir(parents=True)
    control.owner_path.write_text(
        json.dumps({"instance_id": "stale", "pid": 99999999}), encoding="utf-8"
    )

    with control.acquire_owner("instance-new"):
        assert control.read_owner()["instance_id"] == "instance-new"
        assert len(list(control.root.glob("owner.json.stale-*"))) == 1


def test_free_os_lease_archives_metadata_even_when_pid_was_reused(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.owner_path.parent.mkdir(parents=True)
    metadata = {
        "instance_id": "other-instance",
        "pid": os.getpid(),
        "config_identity": control.identity,
    }
    control.owner_path.write_text(json.dumps(metadata), encoding="utf-8")

    with control.acquire_owner("replacement-instance"):
        current = control.read_owner()
        assert current["instance_id"] == "replacement-instance"
        assert current["process_start_identity"]

    archived = list(control.root.glob("owner.json.stale-*"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8")) == metadata


def test_active_owner_rejects_metadata_from_reused_pid_or_other_instance(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_owner("current-instance"):
        metadata = control.read_owner()
        metadata["instance_id"] = "stale-instance"
        metadata["pid"] = os.getpid()
        metadata["process_start_identity"] = "reused-pid"
        control.owner_path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(LifecycleConflict, match="process identity is stale"):
            control.read_active_owner()


def test_same_request_attaches_and_changed_identity_conflicts(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    payload = {"kind": "handover", "repo": "app", "target_ref": "abc"}

    first = control.submit_request("request-1", payload)
    attached = control.submit_request("request-1", payload)

    assert first.attached is False
    assert attached.attached is True
    with pytest.raises(LifecycleConflict, match="REQUEST_IDENTITY_CONFLICT"):
        control.submit_request("request-1", {**payload, "target_ref": "different"})


def test_concurrent_changed_request_identity_has_one_atomic_winner(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    worker_count = 2
    barrier = threading.Barrier(worker_count)

    def submit(index: int):
        barrier.wait()
        try:
            return control.submit_request(
                "request-1",
                {
                    "kind": "handover",
                    "repo": "app",
                    "target_ref": f"target-{index}",
                },
            )
        except Exception as error:  # Assert the public error type below.
            return error

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        outcomes = list(pool.map(submit, range(worker_count)))

    submissions = [
        outcome for outcome in outcomes if not isinstance(outcome, Exception)
    ]
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(submissions) == 1
    assert submissions[0].attached is False
    assert len(errors) == worker_count - 1
    assert all(
        isinstance(error, LifecycleConflict)
        and "REQUEST_IDENTITY_CONFLICT" in str(error)
        for error in errors
    )


def test_concurrent_same_request_identity_creates_once_and_attaches_once(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    barrier = threading.Barrier(2)
    payload = {"kind": "handover", "repo": "app", "target_ref": "target"}

    def submit():
        barrier.wait()
        return control.submit_request("request-1", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: submit(), range(2)))

    assert sorted(outcome.attached for outcome in outcomes) == [False, True]


def test_cross_process_changed_request_identity_has_one_atomic_winner(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    config = tmp_path / "haniel.yaml"
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_submit_in_process,
            args=(
                str(config),
                "request-1",
                {
                    "kind": "handover",
                    "repo": "app",
                    "target_ref": f"target-{index}",
                },
                start,
                outcomes,
            ),
        )
        for index in range(2)
    ]

    results = _run_processes(context, processes, start, outcomes)

    assert len([result for result in results if result[0] == "ok"]) == 1
    errors = [result for result in results if result[0] == "error"]
    assert len(errors) == 1
    assert errors[0][1:] == (
        "LifecycleConflict",
        "REQUEST_IDENTITY_CONFLICT: request_id has different payload",
    )
    stored = json.loads(
        LifecycleControl(config).request_path("request-1").read_text(encoding="utf-8")
    )
    winning_target = stored["payload"]["target_ref"]
    assert winning_target in {"target-0", "target-1"}


def test_cross_process_same_request_creates_once_and_attaches_once(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    config = tmp_path / "haniel.yaml"
    start = context.Event()
    outcomes = context.Queue()
    payload = {"kind": "handover", "repo": "app", "target_ref": "target"}
    processes = [
        context.Process(
            target=_submit_in_process,
            args=(str(config), "request-1", payload, start, outcomes),
        )
        for _index in range(2)
    ]

    results = _run_processes(context, processes, start, outcomes)

    assert sorted(result[1] for result in results) == [False, True]


def test_cross_process_ack_update_is_serialized_without_loss(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)
    control.submit_request("request-1", {"kind": "handover", "repo": "app"})

    for state, detail in (
        ("accepted", {"owner_instance": "owner-1"}),
        ("quiesced", {"stopped_services": ["app"]}),
        ("terminal", {"ok": True}),
    ):
        start = context.Event()
        outcomes = context.Queue()
        processes = [
            context.Process(
                target=_ack_in_process,
                args=(str(config), "request-1", state, detail, start, outcomes),
            )
            for _index in range(2)
        ]
        results = _run_processes(context, processes, start, outcomes)
        assert results == [("ok", state), ("ok", state)]

    assert [entry["state"] for entry in control.read_result("request-1")["acks"]] == [
        "accepted",
        "quiesced",
        "terminal",
    ]


def test_start_owner_binds_the_initial_request_before_background_startup(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"

    with patch("haniel.core.one_shot_handover.subprocess.Popen") as popen:
        _start_resident_owner(config, "request-1")

    argv = popen.call_args.args[0]
    assert argv[-3:] == [str(config), "--initial-request-id", "request-1"]


def test_upgrade_without_resident_owner_refuses_synthetic_quiescence(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"

    with (
        patch("haniel.core.one_shot_handover._start_resident_owner") as start_owner,
        pytest.raises(LifecycleConflict, match="LIFECYCLE_OWNER_REQUIRED"),
    ):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            start_owner=True,
            wait_timeout=0,
        )

    start_owner.assert_not_called()
    assert not LifecycleControl(config).request_path("request-1").exists()


def test_fresh_without_owner_and_without_start_owner_fails_before_spool(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"

    with pytest.raises(LifecycleConflict, match="LIFECYCLE_OWNER_MISSING"):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="fresh_install",
            request_id="request-1",
            start_owner=False,
            wait_timeout=0,
        )

    assert not LifecycleControl(config).request_path("request-1").exists()


def test_fresh_owner_start_failure_cancels_spooled_request(tmp_path: Path) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)

    with (
        patch(
            "haniel.core.one_shot_handover._start_resident_owner",
            side_effect=OSError("secret start detail"),
        ),
        pytest.raises(RuntimeError, match="OWNER_START_FAILED"),
    ):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="fresh_install",
            request_id="request-1",
            start_owner=True,
            wait_timeout=0,
        )

    terminal = control.read_result("request-1")["terminal"]
    assert terminal["error"]["code"] == "OWNER_START_FAILED"
    assert "secret start detail" not in terminal["error"]["message"]


def test_terminal_timeout_cancels_request_before_late_owner_can_execute(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)

    with (
        control.acquire_owner("owner-1"),
        pytest.raises(TimeoutError, match="REQUEST_TIMEOUT"),
    ):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            start_owner=False,
            wait_timeout=0,
        )

    runner = MagicMock()
    server = LifecycleRequestServer(
        control=control,
        runner=runner,
        instance_id="owner-1",
    )
    server.handle_request("request-1")

    runner.assert_not_called()
    assert control.read_result("request-1")["terminal"]["error"]["code"] == (
        "REQUEST_TIMEOUT"
    )


def test_retry_of_cancelled_request_returns_stable_error_instead_of_type_error(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)
    control.submit_request(
        "request-1",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
        },
    )
    control.cancel_request(
        "request-1",
        code="REQUEST_TIMEOUT",
        message="request timed out",
    )

    with (
        control.acquire_owner("owner-1"),
        pytest.raises(RuntimeError, match="REQUEST_TIMEOUT"),
    ):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            start_owner=False,
            wait_timeout=0.1,
        )


def test_timeout_cannot_cancel_request_already_accepted_by_resident_owner(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    control = LifecycleControl(config)
    control.submit_request(
        "request-1",
        {
            "kind": "handover",
            "repo": "app",
            "target_ref": "origin/main",
            "expected_operation": "upgrade",
        },
    )
    control.ack("request-1", "accepted", {"owner_instance": "owner-1"})

    with (
        control.acquire_owner("owner-1"),
        pytest.raises(TimeoutError, match="REQUEST_IN_PROGRESS"),
    ):
        execute_manifest_handover_once(
            config_path=config,
            repo_name="app",
            target_ref="origin/main",
            expected_operation="upgrade",
            request_id="request-1",
            start_owner=False,
            wait_timeout=0,
        )

    result = control.read_result("request-1")
    assert result.get("terminal") is None
    assert [ack["state"] for ack in result["acks"]] == ["accepted"]


def test_ack_progression_and_terminal_result_are_atomic(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.submit_request("request-1", {"kind": "handover", "repo": "app"})

    control.ack("request-1", "accepted", {"owner_instance": "instance-a"})
    control.ack("request-1", "quiesced", {"stopped_services": ["app"]})
    control.ack("request-1", "terminal", {"ok": True})

    result = control.read_result("request-1")
    assert [entry["state"] for entry in result["acks"]] == [
        "accepted",
        "quiesced",
        "terminal",
    ]
    assert result["terminal"]["ok"] is True


def test_concurrent_duplicate_acks_do_not_lose_or_duplicate_phases(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.submit_request("request-1", {"kind": "handover", "repo": "app"})

    phases = [
        ("accepted", {"owner_instance": "owner-1"}),
        ("quiesced", {"stopped_services": ["app"]}),
        ("terminal", {"ok": True}),
    ]
    for state, detail in phases:
        barrier = threading.Barrier(2)

        def acknowledge(_index: int) -> None:
            barrier.wait()
            control.ack("request-1", state, detail)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(acknowledge, range(2)))

    result = control.read_result("request-1")
    assert [entry["state"] for entry in result["acks"]] == [
        "accepted",
        "quiesced",
        "terminal",
    ]


def test_retry_does_not_move_accepted_ack_backwards(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.submit_request("request-1", {"kind": "handover", "repo": "app"})
    control.ack("request-1", "accepted", {"owner_instance": "instance-a"})
    control.ack("request-1", "quiesced", {"stopped_services": ["app"]})

    control.ack("request-1", "accepted", {"owner_instance": "instance-a"})

    assert [ack["state"] for ack in control.read_result("request-1")["acks"]] == [
        "accepted",
        "quiesced",
    ]


def test_stop_rejects_wrong_instance_without_enqueuing(tmp_path: Path) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_owner("instance-a"):
        with pytest.raises(LifecycleConflict, match="EXPECTED_INSTANCE_MISMATCH"):
            control.request_stop(expected_instance="instance-b", request_id="stop-1")

    assert not control.request_path("stop-1").exists()


def test_stop_rejects_stale_metadata_without_active_os_owner(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.owner_path.parent.mkdir(parents=True)
    control.owner_path.write_text(
        json.dumps(
            {
                "instance_id": "stale-instance",
                "lease_identity": "stale-instance",
                "process_start_identity": "reused-pid",
                "config_identity": control.identity,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LifecycleConflict, match="LIFECYCLE_OWNER_MISSING"):
        control.request_stop(
            expected_instance="stale-instance",
            request_id="stop-1",
        )

    assert not control.request_path("stop-1").exists()


def test_repo_deployment_lease_blocks_other_request_but_allows_attach(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")

    with control.acquire_deployment("app", "request-1") as first:
        attached = control.acquire_deployment("app", "request-1")
        assert attached.attached is True
        with pytest.raises(LifecycleConflict, match="DEPLOYMENT_LEASE_CONFLICT"):
            control.acquire_deployment("app", "request-2")
        first.acknowledge_quiesced(["app"])

    assert control.read_result("request-1")["acks"][-1]["state"] == "quiesced"


@pytest.mark.parametrize("runner_mode", ["winsw", "foreground-fallback"])
def test_resident_server_stops_only_the_expected_instance(
    tmp_path: Path, runner_mode: str
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    runner = MagicMock(mode=runner_mode)
    server = LifecycleRequestServer(
        control=control,
        runner=runner,
        instance_id="instance-a",
    )
    control.submit_request(
        "stop-1", {"kind": "stop", "expected_instance": "instance-a"}
    )

    server._handle_stop("stop-1", {"kind": "stop", "expected_instance": "instance-a"})

    runner.stop.assert_called_once_with()
    result = control.read_result("stop-1")
    assert [ack["state"] for ack in result["acks"]] == ["accepted", "terminal"]
    assert result["terminal"]["owner_instance"] == "instance-a"


def test_spool_failures_are_terminal_and_do_not_block_later_requests(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    runner = MagicMock()
    runner.stop.side_effect = [RuntimeError("private runtime detail"), None]
    server = LifecycleRequestServer(
        control=control,
        runner=runner,
        instance_id="instance-a",
    )
    malformed = control.request_path("a-malformed")
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not-json", encoding="utf-8")
    mismatched = control.request_path("b-config-mismatch")
    mismatched.write_text(
        json.dumps(
            {
                "schema_version": "haniel.lifecycle.request.v1",
                "request_id": "b-config-mismatch",
                "config_identity": "wrong-config",
                "payload": {"kind": "stop", "expected_instance": "instance-a"},
            }
        ),
        encoding="utf-8",
    )
    control.submit_request("c-unknown", {"kind": "unsupported"})
    control.submit_request(
        "c-runtime",
        {
            "kind": "runtime-handover",
            "repo": "app",
            "target_ref": "target",
            "expected_operation": "upgrade",
            "executor_instance": "instance-a",
        },
    )
    control.submit_request("d-malformed-envelope", {"kind": "handover"})
    control.submit_request(
        "e-handler", {"kind": "stop", "expected_instance": "instance-a"}
    )
    control.submit_request(
        "f-valid", {"kind": "stop", "expected_instance": "instance-a"}
    )

    server.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            if control.read_result("f-valid").get("terminal"):
                break
            time.sleep(0.01)
    finally:
        server.close()

    assert control.read_result("a-malformed")["terminal"]["error"]["code"] == (
        "MALFORMED_REQUEST"
    )
    assert control.read_result("b-config-mismatch")["terminal"]["error"]["code"] == (
        "REQUEST_IDENTITY_CONFLICT"
    )
    assert control.read_result("c-unknown")["terminal"]["error"]["code"] == (
        "UNSUPPORTED_REQUEST_KIND"
    )
    assert control.read_result("c-runtime").get("terminal") is None
    assert (
        control.read_result("d-malformed-envelope")["terminal"]["error"]["code"]
        == "MALFORMED_REQUEST"
    )
    handler_error = control.read_result("e-handler")["terminal"]["error"]
    assert handler_error["code"] == "REQUEST_HANDLER_FAILED"
    assert "private runtime detail" not in handler_error["message"]
    assert control.read_result("f-valid")["terminal"]["ok"] is True


def test_restarted_owner_terminalizes_orphaned_runtime_request(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    control.submit_request(
        "runtime-1",
        {
            "kind": "runtime-handover",
            "repo": "app",
            "target_ref": "target",
            "expected_operation": "upgrade",
            "executor_instance": "old-owner",
        },
    )
    control.ack("runtime-1", "accepted", {"owner_instance": "old-owner"})
    server = LifecycleRequestServer(
        control=control,
        runner=MagicMock(),
        instance_id="new-owner",
    )

    server.handle_request("runtime-1")

    terminal = control.read_result("runtime-1")["terminal"]
    assert terminal["error"]["code"] == "RUNTIME_OWNER_LOST"


def test_terminal_write_failure_is_isolated_from_next_spool_request(
    tmp_path: Path,
) -> None:
    control = LifecycleControl(tmp_path / "haniel.yaml")
    malformed = control.request_path("a-malformed")
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not-json", encoding="utf-8")
    control.submit_request(
        "b-valid", {"kind": "stop", "expected_instance": "instance-a"}
    )
    runner = MagicMock()
    server = LifecycleRequestServer(
        control=control,
        runner=runner,
        instance_id="instance-a",
    )
    original_ack = control.ack

    def flaky_ack(request_id, state, detail):
        if request_id == "a-malformed":
            raise OSError("result disk unavailable")
        return original_ack(request_id, state, detail)

    with patch.object(control, "ack", side_effect=flaky_ack):
        server.start()
        deadline = time.monotonic() + 2
        try:
            while time.monotonic() < deadline:
                if control.read_result("b-valid").get("terminal"):
                    break
                time.sleep(0.01)
        finally:
            server.close()

    assert control.read_result("b-valid")["terminal"]["ok"] is True
