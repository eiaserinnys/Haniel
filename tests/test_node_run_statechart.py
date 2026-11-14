"""Contracts for the node execution definition."""

import pytest

from haniel.core.node_run import initial_row, step


def test_queued_execution_and_terminal_ack():
    row = initial_row()
    row, todo = step(
        row, "permit", {"holds_other": False, "unexpected_auto": False, "free": False}
    )
    assert row == {"exec_": "queued", "report": "r_none"}
    assert todo == ["acked"]
    row, todo = step(row, "slot_free", {"holds_other": False, "unexpected_auto": False})
    assert row["exec_"] == "running" and todo == ["started"]
    row, todo = step(row, "finish_ok")
    assert row == {"exec_": "ok", "report": "r_owed"} and todo == ["bundle"]
    row, todo = step(row, "link_up")
    assert todo == ["bundle"]
    row, todo = step(row, "terminal_ack")
    assert row["report"] == "r_acked" and todo == []


@pytest.mark.parametrize("fact", ["holds_other", "unexpected_auto"])
def test_permit_refusal(fact):
    facts = {"holds_other": False, "unexpected_auto": False, "free": False}
    facts[fact] = True
    row, todo = step(initial_row(), "permit", facts)
    assert row["exec_"] == "declined" and todo == ["declined"]


def test_slot_revalidation_failure_does_not_start():
    row, _ = step(
        initial_row(),
        "permit",
        {"holds_other": False, "unexpected_auto": False, "free": False},
    )
    row, todo = step(
        row,
        "slot_free",
        {"holds_other": False, "unexpected_auto": False, "revalidation_failed": True},
    )
    assert row == {"exec_": "declined", "report": "r_none"}
    assert todo == ["declined"]


def test_every_reachable_configuration_round_trips():
    from statemachine.exceptions import TransitionNotAllowed
    from haniel.core.node_run import Facts, NodeRun, _row, restore

    events = (
        "ask",
        "give_up",
        "permit",
        "own_start",
        "slot_free",
        "finish_ok",
        "finish_fail",
        "terminal_ack",
        "link_up",
        "link_down",
    )
    facts = (
        {"holds_other": False, "unexpected_auto": False, "free": False},
        {"holds_other": False, "unexpected_auto": False, "free": True},
        {"holds_other": True, "unexpected_auto": False, "free": False},
    )
    first = initial_row()
    pending = [first]
    seen = set()
    while pending:
        row = pending.pop()
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        assert _row(NodeRun(model=Facts(restore(row)))) == row
        for event in events:
            for fact in facts:
                try:
                    next_row, _ = step(row, event, fact)
                except TransitionNotAllowed:
                    continue
                if tuple(sorted(next_row.items())) not in seen:
                    pending.append(next_row)
    assert len(seen) == 9


@pytest.mark.parametrize(
    ("row", "event", "expected", "todo"),
    [
        (
            {"exec_": "arrived", "report": "r_none"},
            "ask",
            {"exec_": "asking", "report": "r_none"},
            ["auto_req"],
        ),
        (
            {"exec_": "asking", "report": "r_none"},
            "give_up",
            {"exec_": "declined", "report": "r_none"},
            [],
        ),
        (
            {"exec_": "arrived", "report": "r_none"},
            "own_start",
            {"exec_": "running", "report": "r_none"},
            ["started"],
        ),
        (
            {"exec_": "running", "report": "r_none"},
            "finish_fail",
            {"exec_": "failed", "report": "r_owed"},
            ["bundle"],
        ),
        (
            {"exec_": "ok", "report": "r_owed"},
            "terminal_ack",
            {"exec_": "ok", "report": "r_acked"},
            [],
        ),
        (
            {"exec_": "queued", "report": "r_none"},
            "permit",
            {"exec_": "queued", "report": "r_none"},
            ["acked"],
        ),
        (
            {"exec_": "running", "report": "r_none"},
            "permit",
            {"exec_": "running", "report": "r_none"},
            ["started"],
        ),
        (
            {"exec_": "ok", "report": "r_owed"},
            "permit",
            {"exec_": "ok", "report": "r_owed"},
            ["bundle"],
        ),
        (
            {"exec_": "failed", "report": "r_owed"},
            "permit",
            {"exec_": "failed", "report": "r_owed"},
            ["bundle"],
        ),
        (
            {"exec_": "declined", "report": "r_none"},
            "permit",
            {"exec_": "declined", "report": "r_none"},
            ["declined"],
        ),
        (
            {"exec_": "queued", "report": "r_none"},
            "link_up",
            {"exec_": "queued", "report": "r_none"},
            ["acked"],
        ),
        (
            {"exec_": "running", "report": "r_none"},
            "link_up",
            {"exec_": "running", "report": "r_none"},
            ["started"],
        ),
        (
            {"exec_": "failed", "report": "r_owed"},
            "link_up",
            {"exec_": "failed", "report": "r_owed"},
            ["bundle"],
        ),
        (
            {"exec_": "asking", "report": "r_none"},
            "link_down",
            {"exec_": "declined", "report": "r_none"},
            [],
        ),
        (
            {"exec_": "queued", "report": "r_none"},
            "link_down",
            {"exec_": "queued", "report": "r_none"},
            [],
        ),
        (
            {"exec_": "running", "report": "r_none"},
            "link_down",
            {"exec_": "running", "report": "r_none"},
            [],
        ),
    ],
)
def test_named_transitions(row, event, expected, todo):
    assert step(row, event) == (expected, todo)


@pytest.mark.parametrize(
    ("start", "facts", "expected", "symbol"),
    [
        (
            "arrived",
            {"holds_other": False, "unexpected_auto": False, "free": True},
            "running",
            "started",
        ),
        (
            "asking",
            {"holds_other": False, "unexpected_auto": False, "free": True},
            "running",
            "started",
        ),
        (
            "asking",
            {"holds_other": False, "unexpected_auto": False, "free": False},
            "queued",
            "acked",
        ),
        (
            "asking",
            {"holds_other": True, "unexpected_auto": False, "free": False},
            "declined",
            "declined",
        ),
    ],
)
def test_permit_branches(start, facts, expected, symbol):
    row, todo = step({"exec_": start, "report": "r_none"}, "permit", facts)
    assert row == {"exec_": expected, "report": "r_none"}
    assert todo == [symbol]


@pytest.mark.parametrize("report", ["r_none", "r_acked"])
def test_terminal_ack_self_transitions(report):
    row = {"exec_": "ok", "report": report}
    assert step(row, "terminal_ack") == (row, [])
