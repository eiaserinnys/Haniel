"""Node-side approval sequencer: FIFO order, one execution, client-bound lifetime."""

from __future__ import annotations

import asyncio
import logging
import threading

from haniel.integrations.deploy_execution_sequencer import DeployExecutionSequencer


def _approval(number: int) -> dict:
    return {
        "deploy_id": f"node-a:repo:main:{number:040d}",
        "orchestrator_attempt_id": f"attempt-{number}",
        "connection_generation": "generation-1",
    }


async def test_submit_before_start_is_logged_and_dropped(caplog) -> None:
    async def execute(_approval: dict) -> None:
        raise AssertionError("nothing may execute before start")

    sequencer = DeployExecutionSequencer(execute)
    with caplog.at_level(logging.WARNING):
        assert sequencer.submit(_approval(1)) is False
    assert "attempt-1" in caplog.text


async def test_executes_in_submission_order_one_at_a_time() -> None:
    started: dict[str, asyncio.Event] = {}
    gates: dict[str, asyncio.Event] = {}
    for number in (1, 2, 3):
        started[f"attempt-{number}"] = asyncio.Event()
        gates[f"attempt-{number}"] = asyncio.Event()
    order: list[str] = []
    running = 0
    peak = 0

    async def execute(approval: dict) -> None:
        nonlocal running, peak
        attempt = approval["orchestrator_attempt_id"]
        running += 1
        peak = max(peak, running)
        order.append(attempt)
        started[attempt].set()
        await gates[attempt].wait()
        running -= 1

    sequencer = DeployExecutionSequencer(execute)
    sequencer.start()
    for number in (3, 1, 2):
        assert sequencer.submit(_approval(number)) is True

    await asyncio.wait_for(started["attempt-3"].wait(), 2)
    assert sequencer.queued == 2
    assert sequencer.executing is not None
    assert sequencer.executing["orchestrator_attempt_id"] == "attempt-3"
    gates["attempt-3"].set()
    await asyncio.wait_for(started["attempt-1"].wait(), 2)
    gates["attempt-1"].set()
    await asyncio.wait_for(started["attempt-2"].wait(), 2)
    gates["attempt-2"].set()
    await sequencer.close()

    assert order == ["attempt-3", "attempt-1", "attempt-2"]
    assert peak == 1


async def test_failed_execution_is_logged_and_the_next_one_still_runs(caplog) -> None:
    second_started = asyncio.Event()

    async def execute(approval: dict) -> None:
        if approval["orchestrator_attempt_id"] == "attempt-1":
            raise RuntimeError("boom")
        second_started.set()

    sequencer = DeployExecutionSequencer(execute)
    sequencer.start()
    with caplog.at_level(logging.WARNING):
        sequencer.submit(_approval(1))
        sequencer.submit(_approval(2))
        await asyncio.wait_for(second_started.wait(), 2)
    await sequencer.close()

    assert "attempt-1" in caplog.text
    assert "boom" in caplog.text


async def test_close_drops_queued_approvals_and_leaves_the_running_thread(
    caplog,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_deploy() -> None:
        entered.set()
        release.wait(timeout=5)
        finished.set()

    async def execute(_approval: dict) -> None:
        await asyncio.to_thread(blocking_deploy)

    sequencer = DeployExecutionSequencer(execute)
    sequencer.start()
    sequencer.submit(_approval(1))
    sequencer.submit(_approval(2))
    assert await asyncio.to_thread(entered.wait, 2)

    with caplog.at_level(logging.WARNING):
        await sequencer.close()
        assert sequencer.submit(_approval(3)) is False

    assert "attempt-2" in caplog.text
    assert "attempt-3" in caplog.text
    assert "keeps running in its thread" in caplog.text
    assert sequencer.queued == 0
    assert sequencer.executing is None
    assert not finished.is_set(), "close must not interrupt the running thread"
    release.set()
    assert finished.wait(timeout=2)
