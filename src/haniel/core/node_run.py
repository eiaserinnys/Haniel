"""Node execution statechart and single-step restoration."""

from statemachine import State, StateChart
from statemachine.orderedset import OrderedSet


class Facts:
    def __init__(self, state=None, facts=None):
        self.state = state
        self.facts = facts or {}
        self.todo = []


class Base(StateChart):
    allow_event_without_transition = False
    catch_errors_as_events = False
    validate_trap_states = False

    def fact(self, name):
        return bool(self.model.facts[name])

    def has(self, *names):
        return any(name in self.configuration_values for name in names)

    def out(self, symbol):
        self.model.todo.append(symbol)


class NodeRun(Base):
    class run(State.Parallel):
        class exec_(State.Compound):
            arrived = State(initial=True)
            asking = State()  # 자동 배포 허가를 청했고 답을 기다림
            queued = State()  # 허가를 받아 줄 서 있음
            running = State()

            class done(State.Compound):
                ok = State(initial=True)
                failed = State()
                declined = State()  # 실행하지 않음

        class report(State.Compound):
            r_none = State(initial=True)
            r_owed = State()  # 종결 묶음을 빚짐 (보낼 것 파일에 있다)
            r_acked = State()

    ask = run.exec_.arrived.to(run.exec_.asking, on="send_auto_request")
    give_up = run.exec_.asking.to(run.exec_.done.declined)
    permit = (
        run.exec_.arrived.to(run.exec_.done.declined, cond="refuse", on="send_declined")
        | run.exec_.arrived.to(
            run.exec_.running, cond="slot_is_free", on="send_started"
        )
        | run.exec_.arrived.to(run.exec_.queued, on="send_acked")
        | run.exec_.asking.to(
            run.exec_.done.declined, cond="refuse", on="send_declined"
        )
        | run.exec_.asking.to(run.exec_.running, cond="slot_is_free", on="send_started")
        | run.exec_.asking.to(run.exec_.queued, on="send_acked")
        | run.exec_.queued.to.itself(on="send_acked")
        | run.exec_.running.to.itself(on="send_started")
        | run.exec_.done.ok.to.itself(on="send_bundle")
        | run.exec_.done.failed.to.itself(on="send_bundle")
        | run.exec_.done.declined.to.itself(on="send_declined")
    )
    own_start = run.exec_.arrived.to(run.exec_.running, on="send_started")
    slot_free = run.exec_.queued.to(
        run.exec_.done.declined, cond="refuse", on="send_declined"
    ) | run.exec_.queued.to(run.exec_.running, cond="not_refuse", on="send_started")
    finish_ok = run.exec_.running.to(
        run.exec_.done.ok, on="send_bundle_ok"
    ) | run.report.r_none.to(run.report.r_owed, cond="is_running")
    finish_fail = run.exec_.running.to(
        run.exec_.done.failed, on="send_bundle_fail"
    ) | run.report.r_none.to(run.report.r_owed, cond="is_running")
    terminal_ack = (
        run.report.r_owed.to(run.report.r_acked)
        | run.report.r_acked.to.itself()
        | run.report.r_none.to.itself()
    )
    # 다시 붙으면 지금 아는 것을 다시 알린다
    link_up = (
        run.report.r_owed.to.itself(on="send_bundle")
        | run.exec_.queued.to.itself(on="send_acked")
        | run.exec_.running.to.itself(on="send_started")
    )
    # 노드가 끊김을 안다. 허가를 기다리던 것은 포기한다
    link_down = (
        run.exec_.asking.to(run.exec_.done.declined)
        | run.exec_.queued.to.itself()
        | run.exec_.running.to.itself()
        | run.exec_.done.ok.to.itself()
        | run.exec_.done.failed.to.itself()
    )

    def refuse(self):
        return (
            self.fact("holds_other")
            or self.fact("unexpected_auto")
            or bool(self.model.facts.get("revalidation_failed", False))
        )

    def not_refuse(self):
        return not self.refuse()

    def slot_is_free(self):
        return self.fact("free")

    def is_running(self):
        return self.has("running")

    def send_auto_request(self):
        self.out("auto_req")

    def send_declined(self):
        self.out("declined")

    def send_started(self):
        self.out("started")

    def send_acked(self):
        self.out("acked")

    def send_bundle(self):
        self.out("bundle")

    def send_bundle_ok(self):
        self.out("bundle")

    def send_bundle_fail(self):
        self.out("bundle")


def restore(row):
    states = set()
    for region, leaf in row.items():
        state = NodeRun.states_map[leaf]
        ancestors = list(state.ancestors())
        if region not in {ancestor.id for ancestor in ancestors}:
            raise ValueError(f"{leaf} is not in {region}")
        states.update([state, *ancestors])
    return OrderedSet(
        state.value for state in sorted(states, key=lambda x: x.document_order)
    )


def _row(machine):
    result = {}
    for state in machine.configuration:
        if state.states:
            continue
        ancestor = state
        while ancestor.parent is not None and ancestor.parent.id != "run":
            ancestor = ancestor.parent
        result[ancestor.id] = state.id
    return result


def initial_row():
    return _row(NodeRun(model=Facts()))


def step(row, event, facts=None):
    model = Facts(restore(row), facts)
    machine = NodeRun(model=model)
    machine.send(event)
    return _row(machine), model.todo
