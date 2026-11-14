"""Bounded product exploration based on the design experiment."""

from collections import namedtuple

from .node_model import NodeRun, step as node_step
from .product_steps import evidence_step, request_step, supersede_active_attempt, verify_request_row

MAXA = 2      # 요청 한 건에 열 수 있는 시도 수
MAXP = 3      # 계획 묻기 수
QMAX = 10
OWN = 0       # 노드가 스스로 한 배포의 번호
ASK = 9       # 허가를 청하고 기다리는 실행의 임시 번호

C = namedtuple("C", "st bl lk tag cur opened pk pcnt pa ev sm newer inst slot tgt cut boot dbl rs asks qon qno ph ors au ff v3 jf unconfirmed")
# lk: 사람 승인 필요. au: 자동 허가로 연 시도 번호들. ff: 실제 실행 실패가 있었고 그 뒤 사람 승인이 연 시도가 없다.
# v3: 약속 다를 어겼다(ff인데 자동 허가로 실행을 시작했다). jf: 저널이 「시도의 실행이 실패했다」로 남아 있다(journal을 켠 갈래만)
OPEN = {"pending", "again", "retry", "deploying"}
# ph: up, o(오케스트레이터만 끊김을 앎), n(노드만 앎), both, or(오케스트레이터가 재시작함. 노드는 아직 모르고, 죽기 전에 보낸 글은 닿는다)


class System:
    def __init__(self, *, auto=False, restart=None, asym=False, newer=False, orestart=False, without=()):
        self.auto, self.restart, self.asym, self.newer = auto, restart, asym, newer
        self.orestart = orestart
        self.without = set(without)     # 장치를 하나씩 빼 본다: hello, grace, refuse, startrule, p81, p83, journal
        self.journal = restart == "manifest" and "journal" not in self.without
        self.holes: dict = {}
        self.overflow = 0

    def initial(self):
        return C("pending", "b_none", "l_off", "t_off", 0, 0, 0, 0, False, ("ev_none",) * MAXA, (), False, (), None,
                 False, False, False, False, 0, 0, (), (), "up", 0, (), False, False, 0, ())

    # ── 보내기 ──
    def osend(self, c, msg, k):
        """오케스트레이터가 보낸다. 가능한 결과 구성들을 돌려준다."""
        if c.ph == "up":
            q = c.qon + ((msg, k),)
            if len(q) > QMAX:
                self.overflow += 1
                return []
            outs = [c._replace(qon=q)]
            if self.orestart and c.ors < 1:
                outs.append(self.orch_restart(c))   # 걸음은 적었고, 보내기 전에 죽는다
            return outs
        return [c]      # 노드만 끊김을 아는 동안 보낸 것은 조용히 사라진다

    def orch_restart(self, c):
        """오케스트레이터가 재시작한다. 줄은 남는다. 열린 계획 묻기를 닫는다. 받던 글은 사라지고 보낸 글은 남는다."""
        c = c._replace(qno=(), ph="or", ors=c.ors + 1)
        r = self.orch(c, "link_down")
        return r[0] if r else c

    def nsend(self, c, msg, k):
        if c.ph == "up":
            q = c.qno + ((msg, k),)
            if len(q) > QMAX:
                self.overflow += 1
                return None
            return c._replace(qno=q)
        return c        # 끊긴 줄 모르고 보낸 것은 사라진다. 끊긴 줄 알면 보내지 않는다

    # ── 오케스트레이터 한 걸음 ──
    def orch(self, c, event, *, later=False, human=False, hole=None):
        r = request_step(self, c, event, later=later, human=human)
        if r is None:
            if hole:
                key = ("orch", event, c.st, c.bl)
                self.holes[key] = self.holes.get(key, 0) + 1
            return None
        row, todo = r
        c2 = c._replace(st=row["status"], bl=row["belief"], lk=row["lock"], tag=row["tag"])
        no_execution = event in {"send_failed", "declined", "ack_deadline"} or (
            event == "hello_missing" and c.bl != "b_running"
        )
        if no_execution and c.cur and c2.bl == "b_none" and c2.st in OPEN:
            c2 = c2._replace(unconfirmed=tuple(sorted(set(c2.unconfirmed) | {c.cur})))
        if c2.st in {"success", "rejected", "superseded"}:
            c2 = c2._replace(unconfirmed=())
        if c2.bl in ("b_none", "b_probe"):
            c2 = c2._replace(cur=0)
        if c2.bl != "b_probe":
            c2 = c2._replace(pk=0)
        return c2, todo

    def open_attempt(self, c, auto):
        """시도를 열고 허가를 보낸다. 보내는 순간 끊기는 갈래도 낸다."""
        k = c.opened + 1
        c = c._replace(cur=k, opened=k, pa=False,
                       au=c.au + (k,) if auto else c.au, ff=c.ff if auto else False)
        msg = "apermit" if auto else "permit"
        outs = list(self.osend(c, msg, k))
        if c.ph == "up" and outs:
            # 보내는 순간 끊긴다. 보낸 쪽은 오류로 안다
            d = self.drop(c, "o" if self.asym else "both")
            r = self.orch(d, "send_failed")
            if r:
                outs.append(r[0])
        return outs

    # ── 노드 한 걸음 ──
    def inst_get(self, c, k):
        for i in c.inst:
            if i[0] == k:
                return i
        return None

    def inst_put(self, c, k, ex, rp, rename=None):
        rest = [i for i in c.inst if i[0] != k]
        kk = rename if rename is not None else k
        if not (ex == "declined" and rp == "r_none") and rp != "r_acked":
            rest.append((kk, ex, rp))
        return c._replace(inst=tuple(sorted(rest)))

    def node(self, c, k, event, *, rename=None, hole=None, **facts):
        i = self.inst_get(c, k) or (k, "arrived", "r_none")
        r = node_step(NodeRun, {"exec_": i[1], "report": i[2]}, event, **facts)
        if r is None:
            if hole:
                key = ("node", event, i[1], i[2])
                self.holes[key] = self.holes.get(key, 0) + 1
            return None
        row, todo = r
        ex, rp = row["exec_"], row["report"]
        kk = rename if rename is not None else k
        if ex == "running" and i[1] != "running" and c.tgt:
            c = c._replace(dbl=True)
        if ex == "running" and i[1] != "running" and kk in c.au and c.ff:
            c = c._replace(v3=True)     # 약속 다: 실패한 뒤 사람 승인 없이 자동 허가로 다시 실행한다
        c = self.inst_put(c, k, ex, rp, rename)
        for t in todo:
            if t == "bundle":
                if kk == OWN:
                    msgs = ["own_ok"] if ex == "ok" else ["own_fail"]
                else:
                    msgs = ["res", "set"] if ex == "ok" else ["fail"]
            elif t == "auto_req":
                msgs = ["auto_req"]
            elif kk == OWN:
                msgs = []       # 스스로 한 배포는 받았다, 시작했다를 알리지 않는다
            else:
                msgs = [t]
            for m in msgs:
                c = self.nsend(c, m, kk)
                if c is None:
                    return None
        return c

    def give_slot(self, c):
        """실행 자리가 비면 줄 선 실행에 넘긴다 (순서기)."""
        if c.slot is not None:
            return c
        for i in c.inst:
            if i[1] == "queued":
                c2 = self.node(c, i[0], "slot_free")
                if c2 is not None:
                    return c2._replace(slot=i[0])
        return c

    def held(self, c):
        return {i[0] for i in c.inst if i[1] in ("queued", "running") or i[2] == "r_owed"}

    # ── 연결 ──
    def drop(self, c, ph):
        """ph로 끊긴다. 가던 것은 사라진다. 아는 쪽에 사건을 넣는다."""
        c = c._replace(qon=(), qno=(), ph=ph)
        if ph in ("o", "both"):
            r = self.orch(c, "link_down")
            if r:
                c = r[0]
        if ph in ("n", "both"):
            for i in list(c.inst):
                c2 = self.node(c, i[0], "link_down")
                if c2 is not None:
                    c = c2
        return c

    def reconnect(self, c):
        c = c._replace(ph="up")
        # hello: 노드가 쥔 시도 목록. 오케스트레이터가 지금 시도와 대조한다
        if "hello" not in self.without and c.cur and c.bl in ("b_unknown", "b_queued", "b_running"):
            r = self.orch(c, "hello_holds" if c.cur in self.held(c) else "hello_missing")
            if r:
                c = r[0]
        for i in list(c.inst):
            c2 = self.node(c, i[0], "link_up")
            if c2 is not None:
                c = c2
        return c

    # ── 받기 ──
    def node_recv(self, c, msg, k):
        if msg == "probe":
            if c.cut:
                return self.nsend(c, "plan_closed", k)
            return self.nsend(c, f"plan_exec_f:{c.jf}" if (self.journal and c.jf) else "plan_exec", k)
        if msg in ("permit", "apermit"):
            asking = self.inst_get(c, ASK)
            src = k
            unexpected = False
            if msg == "apermit":
                if asking and asking[1] == "asking":
                    src = ASK
                else:
                    unexpected = True       # 기다리던 이가 없다
            holds_other = any(
                i[0] not in (k, src) and (i[1] in ("queued", "running") or (i[1] == "ok" and i[2] == "r_owed"))
                for i in c.inst
            )
            free = c.slot is None
            if "refuse" in self.without:
                holds_other = False
            # P8-3: 같은 요청의 실패 묶음을 빚진 채 자동 허가가 오면 실행하지 않는다
            owes_fail = (msg == "apermit" and "p83" not in self.without and any(
                i[0] not in (k, src) and i[1] == "failed" and i[2] == "r_owed" for i in c.inst))
            holds_other = holds_other or owes_fail
            c2 = self.node(c, src, "permit", rename=k, hole="받은 글", free=free,
                           holds_other=holds_other, unexpected_auto=unexpected)
            if c2 is None:
                return c
            i = self.inst_get(c2, k)
            if i and i[1] == "running" and c2.slot is None:
                c2 = c2._replace(slot=k)
            if holds_other:
                # 같은 요청의 끝낸 묶음을 쥐고 있으면 다시 보낸다
                for j in list(c2.inst):
                    if j[2] == "r_owed" and (j[1] == "ok" or (owes_fail and j[1] == "failed")):
                        c3 = self.node(c2, j[0], "link_up")
                        if c3 is None:
                            return None
                        c2 = c3
            return c2
        if msg == "tack":
            c2 = self.node(c, k, "terminal_ack")
            return c2 if c2 is not None else c
        raise ValueError(msg)

    def orch_recv(self, c, msg, k):
        """받은 글을 줄의 사실(시도 번호, 계획 묻기 번호)과 견줘 사건으로 바꾼다. 결과 구성들을 돌려준다."""
        if msg == "auto_req":
            if c.opened >= MAXA or c.pcnt >= MAXP:
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
        if msg in ("plan_exec", "plan_closed") or msg.startswith("plan_exec_f:"):
            if c.bl != "b_probe" or c.pk != k:
                return [c]              # 닫힌 계획 묻기의 답
            auto = c.pa
            if msg == "plan_closed":
                event = "plan_closed"
            elif msg.startswith("plan_exec_f:") and auto:
                from .plan_rule import failed_journal_applies, plan_requires_inspection
                linked = int(msg.split(":", 1)[1])
                if plan_requires_inspection(True, c.lk == "l_on", 1):
                    event = "plan_rejected"
                else:
                    event = "plan_failed_journal" if failed_journal_applies(linked, c.unconfirmed) else "plan_execute"
            else:
                event = "plan_execute"
            r = self.orch(c, event, human=not auto, hole="받은 글")
            if not r:
                return [c]
            c2, todo = r
            if "send_permit" in todo:
                return self.open_attempt(c2, auto)
            return [c2]
        if msg in ("acked", "started", "declined"):
            if k != c.cur:
                if msg == "started":
                    return [c._replace(sm=tuple(sorted(set(c.sm) | {k})))]
                return [c]
            r = self.orch(c, msg, hole="받은 글")
            if not r:
                return [c]
            c2 = r[0]
            if msg == "started":
                c2 = c2._replace(sm=tuple(sorted(set(c2.sm) | {k})))
            return [c2]
        if msg == "own_ok":
            event = "node_succeeded" if c.st in OPEN else "success_evidence"
            r = self.orch(c, event, later=True, hole="받은 글")
            if not r:
                return [c]
            return self.osend(supersede_active_attempt(c, r[0]), "tack", OWN)
        if msg == "own_fail":
            r = self.orch(c, "node_failed", hole="받은 글")
            if not r:
                return [c]
            return self.osend(r[0], "tack", OWN)
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
                    later = not no_later_attempt(1, k, c2.opened, c2.sm)
                    if "startrule" in self.without:
                        later = c2.opened > k       # 「마지막으로 연 시도의 종결만 요청을 닫는다」로 바꿔 본다
                    r2 = self.orch(c2, "success_evidence" if ok else "late_failed", later=later, hole="받은 글")
                if r2:
                    after = r2[0]
                    c2 = supersede_active_attempt(c2, after) if ok and k != c2.cur else after
                return self.osend(c2, "tack", k)
            if "dup" in todo:
                return self.osend(c2, "tack", k)    # 이미 종결된 시도의 중복. 같은 답을 다시
            return [c2]                              # 하나만 왔다. 아직 답하지 않는다
        raise ValueError(msg)

    # ── 다음 구성들 ──
    def successors(self, c):
        verify_request_row(c.st, c.bl, c.lk, c.tag)
        out = []

        def add(label, kind, res):
            if res is None:
                return
            if isinstance(res, C):
                res = [res]
            for r in res:
                if r is not None and r != c:
                    out.append((label, kind, r))

        up_o = c.ph in ("up", "n")

        # 사람
        if c.opened < MAXA and c.pcnt < MAXP:
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
        if c.bl == "b_unknown":
            r = self.orch(c, "ack_deadline")
            add("닿았는지 모름의 마감이 지난다", "time", r and r[0])
        if c.bl == "b_probe":
            r = self.orch(c, "probe_deadline")
            add("계획 답의 마감이 지난다", "time", r and r[0])
        if c.bl == "b_queued" and not up_o and "grace" not in self.without:
            r = self.orch(c, "grace_deadline")
            add("끊긴 채 줄 서 있음의 유예 마감이 지난다", "time", r and r[0])
        if c.bl == "b_running":
            i = self.inst_get(c, c.cur)
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

        # 연결
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
            keep = tuple(i for i in c.inst if i[2] == "r_owed" and i[1] in ("ok", "failed"))
            n = c._replace(inst=keep, slot=None, boot=True, rs=c.rs + 1,
                           cut=c.cut or (was_running and self.restart == "manifest"))
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
        if (self.auto and all(i[1] == "failed" for i in c.inst) and not c.boot and not c.tgt
                and c.ph in ("up", "o", "or") and c.slot is None and c.asks < 2):
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
                    add("노드 배포가 끝까지 간다", "system",
                        self.give_slot(n._replace(slot=None, tgt=True, cut=False, jf=False)))
                n = self.node(c, i[0], "finish_fail")
                if n is not None:
                    jf = i[0] if self.journal and i[0] != OWN else 0
                    add("노드 배포가 실패해 되돌린다", "system",
                        self.give_slot(n._replace(slot=None, tgt=False, ff=True, jf=jf)))

        # 받기
        if c.qon and c.ph in ("up", "or"):
            msg, k = c.qon[0]
            add(f"노드가 받는다: {msg} {k}", "system", self.node_recv(c._replace(qon=c.qon[1:]), msg, k))
        if c.qno and c.ph == "up":
            msg, k = c.qno[0]
            add(f"오케스트레이터가 받는다: {msg} {k}", "system", self.orch_recv(c._replace(qno=c.qno[1:]), msg, k))
        return out
