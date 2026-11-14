"""Mixed-version reachability using the product orchestrator."""

from .mixed_base import ASK, MC4, OPEN_STATES, OWN, MixedBase, evidence_step, old_evidence_step, verify_request_row
from .product_steps import supersede_active_attempt


class MixedSystem(MixedBase):
    def orch_recv(self, c: MC4, msg: str, k: int) -> list[MC4]:
        if msg == "auto_req":
            if c.opened >= self.max_attempts or c.pcnt >= self.max_probes:
                return [c]
            r = self.orch(c, "auto_request", hole="받은 글")
            if not r:
                return [c]
            c2, todo = r
            if "send_probe" in todo:
                pid = c2.pcnt + 1
                c2 = c2._replace(pk=pid, pcnt=pid, pa=True)
                return self.osend(c2, "probe", pid)
            return [c2]

        if msg == "plan_closed" or msg == "plan_exec" or msg.startswith("plan_exec_fj:"):
            if c.bl != "b_probe" or c.pk != k:
                return [c]
            auto = c.pa
            if msg == "plan_closed":
                r = self.orch(c, "plan_closed", hole="받은 글")
                return [r[0]] if r else [c]

            fj = int(msg.split(":", 1)[1]) if msg.startswith("plan_exec_fj:") else 0
            # P8-1 재확인(탐침 후 사람 승인 필요가 선 경우) 및 길 1(미확인 닫힘 시도의 실패 저널 발견)
            from .plan_rule import failed_journal_applies, plan_requires_inspection
            lock_from_probe_journal = auto and fj != 0 and failed_journal_applies(fj, c.unconf)
            lock_from_intervening_fail = plan_requires_inspection(auto, c.mlock, self.protocol)
            if self.orch_ver == "new" and (lock_from_probe_journal or lock_from_intervening_fail):
                event = "plan_rejected" if lock_from_intervening_fail else "plan_failed_journal"
                r = self.orch(c, event, hole="받은 글")
                if not r:
                    return [c]
                c2 = r[0]
                return [c2]

            r = self.orch(c, "plan_execute", hole="받은 글")
            if not r:
                return [c]
            c2, todo = r
            if "send_permit" in todo:
                return self.open_attempt(c2, auto)
            return [c2]

        if self.orch_ver == "old":
            if msg in ("acked", "declined", "own_fail"):
                return [c]
            if msg == "started":
                if k != c.cur or c.stale_gen:
                    return [c]
                r = self.orch(c, "started", hole="받은 글")
                if not r:
                    return [c]
                c2 = r[0]._replace(sm=tuple(sorted(set(c.sm) | {k})))
                return [c2]
            if msg == "own_ok":
                r = self.orch(c, "own_success", hole="받은 글")
                return [r[0]] if r else [c]
            if msg in ("res", "set", "fail"):
                if k != c.cur:
                    return [c]
                ev = c.ev[k - 1]
                r = old_evidence_step(ev, msg)
                if r is None:
                    self.holes[("evidence", msg, ev)] = self.holes.get(("evidence", msg, ev), 0) + 1
                    return [c]
                row, todo = r
                evs = list(c.ev)
                evs[k - 1] = row["ev"]
                c2 = c._replace(ev=tuple(evs), sm=tuple(sorted(set(c.sm) | {k})))
                if "terminal_ok" in todo or "terminal_fail" in todo:
                    ok = "terminal_ok" in todo
                    r2 = self.orch(c2, "cur_success" if ok else "cur_failed", hole="받은 글")
                    if r2:
                        c2 = r2[0]
                return [c2]
            raise ValueError(msg)

        # 새 오케스트레이터
        if msg in ("acked", "started", "declined"):
            if k != c.cur:
                return [c]
            r = self.orch(c, msg, hole="받은 글")
            if not r:
                return [c]
            c2 = r[0]
            if msg == "started":
                c2 = c2._replace(sm=tuple(sorted(set(c2.sm) | {k})))
            return [c2]

        if msg == "own_ok":
            event = "node_succeeded" if c.st in OPEN_STATES else "success_evidence"
            r = self.orch(c, event, later=True, hole="받은 글")
            if not r:
                return [c]
            return self._maybe_send_tack(supersede_active_attempt(c, r[0]), OWN)

        if msg == "own_fail":
            ev_name = "node_failed"
            r = self.orch(c, ev_name, hole="받은 글")
            if not r:
                return [c]
            c2 = r[0]
            return self._maybe_send_tack(c2, OWN)

        if msg in ("res", "set", "fail"):
            if k == c.cur:
                started = self.orch(c, "started", hole="받은 글")
                if started:
                    c = started[0]
            ev = c.ev[k - 1]
            r = evidence_step(ev, msg)
            if r is None:
                self.holes[("evidence", msg, ev)] = self.holes.get(("evidence", msg, ev), 0) + 1
                return [c]
            row, todo = r
            evs = list(c.ev)
            evs[k - 1] = row["ev"]
            c2 = c._replace(ev=tuple(evs), sm=tuple(sorted(set(c.sm) | {k})))
            if "terminal_ok" in todo or "terminal_fail" in todo:
                ok = "terminal_ok" in todo
                if k == c2.cur:
                    r2 = self.orch(c2, "cur_success" if ok else "cur_failed", hole="받은 글")
                else:
                    from .later_rule import no_later_attempt
                    protocol = 1 if self.peer_has_cap else None
                    later = not no_later_attempt(protocol, k, c2.opened, c2.sm)
                    fail_ev = "late_failed"
                    r2 = self.orch(c2, "success_evidence" if ok else fail_ev, later=later, hole="받은 글")
                if r2:
                    after = r2[0]
                    c2 = supersede_active_attempt(c2, after) if ok and k != c2.cur else after
                return self._maybe_send_tack(c2, k)
            if "dup" in todo:
                return self._maybe_send_tack(c2, k)
            return [c2]
        raise ValueError(msg)

    def successors(self, c: MC4):
        if self.orch_ver == "new":
            verify_request_row(c.st, c.bl, "l_on" if c.mlock else "l_off", c.tag)
        out = []

        def add(label: str, kind: str, res):
            if res is None:
                return
            if isinstance(res, MC4):
                res = [res]
            for r in res:
                if r is not None and r != c:
                    out.append((label, kind, r))

        up_o = c.ph in ("up", "n")

        # 사람
        if c.opened < self.max_attempts and c.pcnt < self.max_probes:
            r = self.orch(c, "approve")
            if r:
                c2, todo = r
                if "send_permit" in todo:
                    add("사람이 승인한다", "human", self.open_attempt(c2, False))
                elif "send_probe" in todo:
                    pid = c2.pcnt + 1
                    c2 = c2._replace(pk=pid, pcnt=pid, pa=False)
                    add("사람이 다시 승인한다 (계획을 묻는다)", "human", self.osend(c2, "probe", pid))
        r = self.orch(c, "reject")
        if r:
            add("사람이 거절한다", "human", r[0])

        # 마감
        if c.bl == "b_probe":
            r = self.orch(c, "probe_deadline")
            add("계획 답의 마감이 지난다", "time", r and r[0])

        if self.orch_ver == "new" and self.peer_has_cap:
            if c.bl == "b_unknown":
                r = self.orch(c, "ack_deadline")
                add("닿았는지 모름의 마감이 지난다", "time", r and r[0])
            if c.bl == "b_queued" and not up_o:
                r = self.orch(c, "grace_deadline")
                add("끊긴 채 줄 서 있음의 유예 마감이 지난다", "time", r and r[0])
            if c.bl == "b_running":
                i = self.inst_get(c, c.cur)
                renewing = i is not None and i[1] == "running" and c.ph == "up"
                if not renewing:
                    r = self.orch(c, "lease_deadline")
                    add("실행 임대의 마감이 지난다", "time", r and r[0])
        else:
            # 옛 오케스트레이터, 또는 새 오케스트레이터가 능력 없는 옛 노드에 '지금 마감'(실행 임대)을 쓰는 경우
            if c.bl in ("b_unknown", "b_running"):
                i = self.inst_get(c, c.cur)
                if self.orch_ver == "old":
                    renewing = i is not None and i[1] == "running" and c.ph == "up" and not c.stale_gen
                else:
                    renewing = i is not None and i[1] == "running" and c.ph == "up"
                if not renewing:
                    r = self.orch(c, "lease_deadline")
                    add("실행 임대의 마감이 지난다", "time", r and r[0])

        a = self.inst_get(c, ASK)
        if a and a[1] == "asking":
            add("노드가 허가를 기다리다 포기한다", "time", self.node(c, ASK, "give_up"))

        # 더 새 커밋
        if self.newer and not c.newer:
            r = self.orch(c._replace(newer=True), "newer_commit")
            add("더 새 커밋이 온다", "env", r and r[0])

        # 연결 및 오케스트레이터 재시작
        if self.orestart and c.ors < 1 and c.ph == "up":
            add("오케스트레이터가 재시작한다", "env", self.orch_restart(c))
        if c.ph == "up":
            if self.asym:
                add("오케스트레이터가 먼저 끊김을 안다", "env", self.drop(c, "o"))
                add("노드가 먼저 끊김을 안다", "env", self.drop(c, "n"))
            else:
                add("연결이 끊긴다", "env", self.drop(c, "both"))
        elif c.ph in ("o", "n"):
            add("다른 쪽도 끊김을 알아챈다", "link", self.drop(c, "both"))
        elif c.ph == "or":
            add("노드가 끊김을 알아챈다", "link", self.drop(c, "both"))
        else:
            add("다시 붙는다", "relink", self.reconnect(c))

        # 노드 재시작
        if self.restart and c.rs < 1:
            was_running = any(i[1] == "running" for i in c.inst)
            if self.node_ver == "new":
                keep = tuple(i for i in c.inst if i[2] == "r_owed" and i[1] in ("ok", "failed"))
            else:
                keep = ()
            n = c._replace(
                inst=keep, slot=None, boot=True, rs=c.rs + 1,
                cut=c.cut or (was_running and self.restart == "manifest"),
                fjournal=0 if (was_running and self.restart == "manifest") else c.fjournal,
            )
            add("노드가 재시작한다", "env", self.drop(n, "both"))
        if c.boot:
            holds = any(i[1] in ("queued", "running") or (i[1] == "ok" and i[2] == "r_owed") for i in c.inst)
            if c.tgt or holds:
                add("노드가 켜지며 받아 올 것이 없음을 본다", "system", c._replace(boot=False))
            else:
                n = self.node(c._replace(boot=False), OWN, "own_start")
                if n is not None:
                    add("노드가 켜지며 스스로 받아 온다", "system", n._replace(slot=OWN))

        # 노드: 자동 배포를 청한다
        auto_idle = (
            not any(i[0] == ASK or i[1] in ("queued", "running") or i[2] == "r_owed" for i in c.inst)
            if self.node_ver == "old"
            else not c.inst
        )
        if (
            self.auto and auto_idle and not c.boot and not c.tgt
            and c.ph in ("up", "o", "or") and c.slot is None and c.asks < self.max_asks
        ):
            add("노드가 자동 배포를 청한다", "system", self.node(c._replace(asks=c.asks + 1), ASK, "ask"))

        # 노드: 실행 자리
        if c.slot is None and not any(i[1] == "running" for i in c.inst):
            add("다른 저장소의 배포가 시작된다", "env", c._replace(slot="other"))
        if c.slot == "other":
            add("앞 배포가 끝난다", "system", self.give_slot(c._replace(slot=None)))
        for i in c.inst:
            if i[1] == "running":
                n = self.node(c, i[0], "finish_ok")
                if n is not None:
                    add(
                        "노드 배포가 끝까지 간다",
                        "system",
                        self.give_slot(n._replace(slot=None, tgt=True, cut=False, fjournal=0)),
                    )
                n = self.node(c, i[0], "finish_fail")
                if n is not None:
                    fj = i[0] if (self.auto and self.manifest and i[0] != OWN) else n.fjournal
                    uf = True if self.auto else n.unack_fail
                    add(
                        "노드 배포가 실패해 되돌린다",
                        "system",
                        self.give_slot(n._replace(slot=None, tgt=False, unack_fail=uf, fjournal=fj)),
                    )

        # 받기
        if c.qon and c.ph in ("up", "or"):
            msg, k = c.qon[0]
            add(f"노드가 받는다: {msg} {k}", "system", self.node_recv(c._replace(qon=c.qon[1:]), msg, k))
        if c.qno and c.ph == "up":
            msg, k = c.qno[0]
            add(f"오케스트레이터가 받는다: {msg} {k}", "system", self.orch_recv(c._replace(qno=c.qno[1:]), msg, k))
        return out
