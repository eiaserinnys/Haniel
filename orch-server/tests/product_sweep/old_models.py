"""Fixed old participants from the mixed-version design experiment."""

from statemachine import State

from .node_model import Base

class OldOrchReq(Base):
    class req(State.Parallel):
        class status(State.Compound):
            class open_(State.Compound):
                pending = State(initial=True)
                retry = State()
                deploying = State()

            class closed(State.Compound):
                success = State(initial=True)
                rejected = State()
                superseded = State()

        class belief(State.Compound):
            b_none = State(initial=True)
            b_probe = State()
            b_running = State()

    approve = (
        req.status.open_.pending.to(req.status.open_.deploying, cond="up_and_idle")
        | req.belief.b_none.to(req.belief.b_running, cond="up_and_pending", on="send_permit")
        | req.belief.b_none.to(req.belief.b_probe, cond="up_and_retry", on="send_probe")
    )
    auto_request = (
        req.belief.b_none.to(req.belief.b_probe, cond="up_and_pending", on="send_probe")
        | req.status.open_.retry.to.itself(on="send_auto_reject")
        | req.status.open_.deploying.to.itself()
        | req.status.open_.pending.to.itself(cond="not_idle")
        | req.status.closed.success.to.itself()
        | req.status.closed.rejected.to.itself()
        | req.status.closed.superseded.to.itself()
    )
    plan_execute = (
        req.belief.b_probe.to(req.belief.b_running, on="send_permit")
        | req.status.open_.pending.to(req.status.open_.deploying, cond="is_probe")
        | req.status.open_.retry.to(req.status.open_.deploying, cond="is_probe")
    )
    plan_closed = req.belief.b_probe.to(req.belief.b_none, on="note_closed_plan")
    probe_deadline = req.belief.b_probe.to(req.belief.b_none)
    link_down = (
        req.belief.b_probe.to(req.belief.b_none)
        | req.belief.b_none.to.itself()
        | req.belief.b_running.to.itself()
    )
    started = req.belief.b_running.to.itself()
    lease_deadline = (
        req.belief.b_running.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond="is_running_and_newer")
        | req.status.open_.deploying.to(req.status.open_.retry, cond="is_running")
    )
    cur_success = (
        req.status.open_.deploying.to(req.status.closed.success)
        | req.belief.b_running.to(req.belief.b_none)
    )
    cur_failed = (
        req.status.open_.deploying.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.deploying.to(req.status.open_.retry)
        | req.belief.b_running.to(req.belief.b_none)
    )
    own_success = (
        req.status.open_.to(req.status.closed.success)
        | req.belief.b_probe.to(req.belief.b_none)
        | req.belief.b_running.to(req.belief.b_none)
        | req.status.closed.success.to.itself()
        | req.status.closed.rejected.to.itself(on="tag")
        | req.status.closed.superseded.to.itself(on="tag")
    )
    reject = (
        req.status.open_.pending.to(req.status.closed.rejected)
        | req.status.open_.retry.to(req.status.closed.rejected)
        | req.belief.b_probe.to(req.belief.b_none)
    )
    newer_commit = (
        req.status.open_.pending.to(req.status.closed.superseded)
        | req.status.open_.retry.to(req.status.closed.superseded)
        | req.belief.b_probe.to(req.belief.b_none)
        | req.status.open_.deploying.to.itself()
        | req.status.closed.success.to.itself()
        | req.status.closed.rejected.to.itself()
        | req.status.closed.superseded.to.itself()
    )

    def up_and_idle(self):
        return self.fact("up") and self.has("b_none")

    def up_and_pending(self):
        return self.fact("up") and self.has("pending")

    def up_and_retry(self):
        return self.fact("up") and self.has("retry")

    def not_idle(self):
        return not self.has("b_none") or not self.fact("up")

    def is_probe(self):
        return self.has("b_probe")

    def is_running(self):
        return self.has("b_running")

    def is_running_and_newer(self):
        return self.has("b_running") and self.fact("newer")

    def newer(self):
        return self.fact("newer")

    def send_permit(self):
        self.out("send_permit")

    def send_probe(self):
        self.out("send_probe")

    def send_auto_reject(self):
        self.out("auto_reject")

    def note_closed_plan(self):
        self.out("closed_plan")

    def tag(self):
        self.out("tag")


# ================= 3. 옛 노드 실행 (708dcb3 기준) 및 새 노드 실행 =================
class OldNodeRun(Base):
    class run(State.Parallel):
        class exec_(State.Compound):
            arrived = State(initial=True)
            asking = State()
            queued = State()
            running = State()

            class done(State.Compound):
                ok = State(initial=True)
                failed = State()
                declined = State()

        class report(State.Compound):
            r_none = State(initial=True)

    ask = run.exec_.arrived.to(run.exec_.asking, on="send_auto_request")
    give_up = run.exec_.asking.to(run.exec_.done.declined)
    permit = (
        run.exec_.arrived.to(run.exec_.done.declined, cond="unexpected_auto")
        | run.exec_.arrived.to(run.exec_.running, cond="slot_is_free", on="send_started")
        | run.exec_.arrived.to(run.exec_.queued)
        | run.exec_.asking.to(run.exec_.running, cond="slot_is_free", on="send_started")
        | run.exec_.asking.to(run.exec_.queued)
        | run.exec_.queued.to.itself(on="send_reval_fail")
        | run.exec_.running.to.itself(on="send_reval_fail")
        | run.exec_.done.ok.to.itself(on="send_reval_fail")
        | run.exec_.done.failed.to.itself(on="send_reval_fail")
        | run.exec_.done.declined.to.itself(on="send_reval_fail")
    )
    own_start = run.exec_.arrived.to(run.exec_.running)
    slot_free = run.exec_.queued.to(run.exec_.running, on="send_started")
    finish_ok = run.exec_.running.to(run.exec_.done.ok, on="send_bundle_ok")
    finish_fail = run.exec_.running.to(run.exec_.done.failed, on="send_bundle_fail")
    terminal_ack = run.report.r_none.to.itself()
    link_up = (
        run.exec_.queued.to.itself()
        | run.exec_.running.to.itself()
        | run.exec_.done.ok.to.itself()
        | run.exec_.done.failed.to.itself()
        | run.exec_.done.declined.to.itself()
    )
    link_down = (
        run.exec_.asking.to(run.exec_.done.declined)
        | run.exec_.queued.to.itself()
        | run.exec_.running.to.itself()
        | run.exec_.done.ok.to.itself()
        | run.exec_.done.failed.to.itself()
        | run.exec_.done.declined.to.itself()
    )

    def unexpected_auto(self):
        return self.fact("unexpected_auto")

    def slot_is_free(self):
        return self.fact("free")

    def send_auto_request(self):
        self.out("auto_req")

    def send_started(self):
        self.out("started")

    def send_reval_fail(self):
        self.out("reval_fail")

    def send_bundle_ok(self):
        self.out("bundle")

    def send_bundle_fail(self):
        self.out("bundle")

class OldEvidence(Base):
    class ev(State.Compound):
        ev_none = State(initial=True)
        ev_result = State()        # 성공 결과만 옴
        ev_settled = State()       # 체크아웃 대조만 옴
        ev_both = State()          # 성공으로 종결
        ev_failed = State()        # 실패로 종결

    res = (
        ev.ev_none.to(ev.ev_result)
        | ev.ev_settled.to(ev.ev_both, on="terminal_ok")
        | ev.ev_result.to.itself()
        | ev.ev_both.to.itself(on="dup")
        | ev.ev_failed.to.itself(on="dup")
    )
    settled = (
        ev.ev_none.to(ev.ev_settled)
        | ev.ev_result.to(ev.ev_both, on="terminal_ok")
        | ev.ev_settled.to.itself()
        | ev.ev_both.to.itself(on="dup")
        | ev.ev_failed.to.itself(on="dup")
    )
    fail = (
        ev.ev_none.to(ev.ev_failed, on="terminal_fail")
        | ev.ev_result.to(ev.ev_failed, on="terminal_fail")
        | ev.ev_settled.to(ev.ev_failed, on="terminal_fail")
        | ev.ev_failed.to.itself(on="dup")
        | ev.ev_both.to.itself(on="dup")
    )

    def terminal_ok(self):
        self.out("terminal_ok")

    def terminal_fail(self):
        self.out("terminal_fail")

    def dup(self):
        self.out("dup")
