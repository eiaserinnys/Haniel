"""Connect the exploration to the production definitions and intake rules."""

from functools import lru_cache

from haniel_orch import deploy_attempt_intake as intake
from haniel_orch.statechart.columns import REGION_COLUMNS, attempt_leaf_is_consistent
from haniel_orch.statechart.definitions import AttemptEvidence, Facts, OrchRequest
from haniel_orch.statechart.restore import NoTransition, advance_with_todo, restore, to_row


@lru_cache(maxsize=None)
def _step(cls, items, event, facts):
    row = dict(items)
    try:
        after, todo = advance_with_todo(cls, row, event, dict(facts))
    except NoTransition:
        return None
    return after, tuple(todo)


def _advance(cls, row, event, facts=None):
    return _step(cls, tuple(sorted(row.items())), event, tuple(sorted((facts or {}).items())))


@lru_cache(maxsize=None)
def verify_request_row(status, belief, lock, tag):
    row = {"status": status, "belief": belief, "lock": lock, "tag": tag}
    columns = REGION_COLUMNS[OrchRequest.__name__]
    stored = {columns[region]: leaf for region, leaf in row.items()}
    restored_row = {region: stored[column] for region, column in columns.items()}
    machine = OrchRequest(model=Facts(restore(OrchRequest, restored_row), {}))
    assert to_row(OrchRequest, machine) == row


def evidence_step(leaf, message):
    events = {"res": ("result_ok", {"execute_mode": True}),
              "set": ("settled_ok", {"execute_mode": True}),
              "fail": ("fail", {})}
    event, facts = events[message]
    row = {"ev": leaf}
    _verify_evidence_row(leaf)
    result = _advance(AttemptEvidence, row, event, facts)
    if result is not None:
        after, _ = result
        _verify_evidence_row(after["ev"])
    return result


@lru_cache(maxsize=None)
def _verify_evidence_row(leaf):
    row = {"ev": leaf}
    columns = REGION_COLUMNS[AttemptEvidence.__name__]
    stored = {columns[region]: value for region, value in row.items()}
    restored_row = {region: stored[column] for region, column in columns.items()}
    machine = AttemptEvidence(model=Facts(restore(AttemptEvidence, restored_row), {}))
    assert to_row(AttemptEvidence, machine) == row


def supersede_active_attempt(before, after):
    if not before.cur or after.st != "success":
        return after
    index = before.cur - 1
    leaf = before.ev[index]
    assert attempt_leaf_is_consistent(leaf, "superseded"), (before, after)
    _verify_evidence_row(leaf)
    return after


def request_step(system, c, event, *, later=False, human=False):
    row = {"status": c.st, "belief": c.bl, "lock": c.lk, "tag": c.tag}
    protocol = getattr(system, "protocol", 1)
    facts = {}
    if event == "approve":
        if c.ph not in ("up", "n"):
            return None
        if c.st == "pending":
            event, facts = "begin_attempt", {"manual_single": True, "via_probe": False}
        else:
            event = "open_probe"
    elif event == "plan_execute":
        from .plan_rule import plan_requires_inspection
        if plan_requires_inspection(not human, c.lk == "l_on", protocol):
            event = "plan_rejected"
        else:
            event, facts = "begin_attempt", {"manual_single": human, "via_probe": True}
    elif event == "cur_success":
        event = "attempt_succeeded"
    elif event == "cur_failed":
        event, facts = "attempt_failed", {"newer": c.newer}
    elif event == "success_evidence":
        facts = {"no_later": not later}
    elif event == "node_failed":
        facts = {"no_active_attempt": c.cur == 0}
    elif event == "acked":
        result = intake.intake_received(node_protocol=protocol, active=True)
        event, facts = result.event, result.facts
    elif event == "started":
        result = intake.intake_progress(active=True)
        event, facts = result.event, result.facts
    elif event == "declined":
        result = intake.intake_terminal(node_protocol=protocol, belief=c.bl, newer=c.newer)
        event, facts = result.event, result.facts
    elif event == "send_failed":
        result = intake.intake_send_failed(node_protocol=protocol, newer=c.newer)
        event, facts = result.event, result.facts
    elif event in {"ack_deadline", "grace_deadline", "lease_deadline"}:
        kinds = {"ack_deadline": intake.DEADLINE_ACK, "grace_deadline": intake.DEADLINE_GRACE,
                 "lease_deadline": intake.DEADLINE_LEASE}
        result = intake.intake_deadline(node_protocol=protocol, deadline_kind=kinds[event],
                                        belief=c.bl, newer=c.newer)
        if result is None:
            return None
        event, facts = result.event, result.facts
    elif event in {"hello_holds", "hello_missing"}:
        held = {str(c.cur)} if event == "hello_holds" else set()
        result = intake.intake_hello(node_protocol=protocol, belief=c.bl, held=held,
                                     attempt_id=str(c.cur), newer=c.newer)
        if result is None:
            return None
        event, facts = result.event, result.facts
    elif event == "link_down":
        result = intake.intake_link_down()
        event, facts = result.event, result.facts

    if event in {"late_failed", "node_failed"} and "p81" in system.without:
        from .mutations import without_p81
        return _advance(without_p81(), row, event, facts)
    result = _advance(OrchRequest, row, event, facts)
    if result is None and event in {"success_evidence", "late_failed", "node_succeeded", "node_failed"}:
        return row, ()
    return result
