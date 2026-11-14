"""Mixed-version product model based on the design experiment."""

from collections import namedtuple
from types import SimpleNamespace

from .node_model import NodeRun, step as model_step
from .old_models import OldEvidence, OldNodeRun, OldOrchReq
from .product_steps import evidence_step, request_step, verify_request_row

OWN = 0
ASK = 9
QMAX = 10
OPEN_STATES = {"pending", "again", "retry", "deploying"}


def fast_orch_step(cls, st, bl, event, *, up, newer, later, locked, mode):
    return model_step(cls, {"status": st, "belief": bl}, event, up=up, newer=newer)


def fast_node_step(cls, ex, rp, event, f_items):
    return model_step(cls, {"exec_": ex, "report": rp}, event, **dict(f_items))


def old_evidence_step(ev, msg):
    return model_step(OldEvidence, {"ev": ev}, {"res": "res", "set": "settled", "fail": "fail"}[msg])

#   mlock: 오케스트레이터 줄에 '사람 승인 필요'가 서 있는가(P8-1, P8-2)
#   unconf: '사람 승인 필요 아니오'(again)로 닫힌 시도 번호 집합(P8-1 탐침 실패 저널 판정용)
#   fjournal: manifest 저장소에서 마지막으로 실패 저널을 남긴 시도 번호(0이면 없음)
#   unack_fail: 노드에서 실제 실행 실패가 난 뒤 아직 사람 승인이 새 시도를 열기 전인가(약속 다 판정용)
#   viol_c: 약속 다 위반(unack_fail이 참인 동안 자동 허가로 실행을 시작했는가)
MC4 = namedtuple(
    "MC4",
    "st bl cur opened pk pcnt pa ev sm newer inst slot tgt cut boot dbl rs asks qon qno ph ors "
    "stale_gen mlock unconf fjournal unack_fail viol_c tag",
)


class MixedBase:
    def __init__(
        self,
        *,
        orch_ver: str,
        node_ver: str,
        auto: bool = False,
        restart: str | None = None,
        manifest: bool = False,
        asym: bool = False,
        newer: bool = False,
        orestart: bool = False,
        max_attempts: int = 2,
        max_probes: int = 3,
        max_asks: int = 2,
    ):
        assert orch_ver in ("old", "new")
        assert node_ver in ("old", "new")
        self.orch_ver = orch_ver
        self.node_ver = node_ver
        self.orch_cls = None if orch_ver == "new" else OldOrchReq
        self.node_cls = NodeRun if node_ver == "new" else OldNodeRun
        self.protocol = 1 if node_ver == "new" else None
        self.auto = auto
        self.restart = restart
        self.manifest = manifest or (restart == "manifest")
        self.asym = asym
        self.newer = newer
        self.orestart = orestart
        self.max_attempts = max_attempts
        self.max_probes = max_probes
        self.max_asks = max_asks
        self.without = set()
        self.holes: dict = {}
        self.overflow = 0

    @property
    def peer_has_cap(self) -> bool:
        return self.orch_ver == "new" and self.node_ver == "new"

    def initial(self) -> MC4:
        return MC4(
            "pending", "b_none", 0, 0, 0, 0, False,
            ("ev_none",) * self.max_attempts, (), False, (), None,
            False, False, False, False, 0, 0, (), (), "up", 0,
            False, False, (), 0, False, False, "t_off",
        )

    def osend(self, c: MC4, msg: str, k: int) -> list[MC4]:
        if c.ph == "up":
            q = c.qon + ((msg, k),)
            if len(q) > QMAX:
                self.overflow += 1
                return []
            outs = [c._replace(qon=q)]
            if self.orestart and c.ors < 1:
                outs.append(self.orch_restart(c))
            return outs
        return [c]

    def orch_restart(self, c: MC4) -> MC4:
        c = c._replace(qno=(), ph="or", ors=c.ors + 1)
        r = self.orch(c, "link_down")
        return r[0] if r else c

    def nsend(self, c: MC4, msg: str, k: int) -> MC4 | None:
        if c.ph == "up":
            q = c.qno + ((msg, k),)
            if len(q) > QMAX:
                self.overflow += 1
                return None
            return c._replace(qno=q)
        return c

    def orch(self, c: MC4, event: str, *, later: bool = False, hole: str | None = None):
        if self.orch_ver == "new":
            view = SimpleNamespace(st=c.st, bl=c.bl, lk="l_on" if c.mlock else "l_off",
                                   tag=c.tag, ph=c.ph, newer=c.newer, cur=c.cur)
            r = request_step(self, view, event, later=later, human=not c.pa)
        else:
            r = fast_orch_step(self.orch_cls, c.st, c.bl, event, up=c.ph in ("up", "n"),
                               newer=c.newer, later=later, locked=False, mode="old")
        if r is None:
            if hole:
                key = ("orch", self.orch_ver, event, c.st, c.bl)
                self.holes[key] = self.holes.get(key, 0) + 1
            return None
        row, todo = r
        prev_cur = c.cur
        if self.orch_ver == "new":
            c2 = c._replace(st=row["status"], bl=row["belief"],
                            mlock=row["lock"] == "l_on", tag=row["tag"])
        else:
            c2 = c._replace(st=row["status"], bl=row["belief"])
            if c2.st == "retry":
                c2 = c2._replace(mlock=True)
            elif c2.st in ("success", "rejected", "superseded"):
                c2 = c2._replace(mlock=False)
        no_execution = event in {"send_failed", "declined", "ack_deadline"} or (
            event == "hello_missing" and c.bl != "b_running"
        )
        if prev_cur and no_execution and c2.bl == "b_none" and c2.st in OPEN_STATES:
            c2 = c2._replace(unconf=tuple(sorted(set(c2.unconf) | {prev_cur})))
        if c2.st in {"success", "rejected", "superseded"}:
            c2 = c2._replace(unconf=())
        if c2.bl in ("b_none", "b_probe"):
            c2 = c2._replace(cur=0, stale_gen=False)
        if c2.bl != "b_probe":
            c2 = c2._replace(pk=0)
        return c2, todo

    def open_attempt(self, c: MC4, auto: bool) -> list[MC4]:
        k = c.opened + 1
        # P8-2: 사람 승인이 새 시도를 열 때만 '사람 승인 필요'와 약속 다의 미승인 실패 플래그를 내린다
        if not auto:
            c = c._replace(cur=k, opened=k, pa=False, stale_gen=False, unack_fail=False)
        else:
            c = c._replace(cur=k, opened=k, pa=False, stale_gen=False)
        msg = "apermit" if auto else "permit"
        outs = list(self.osend(c, msg, k))
        if c.ph == "up" and outs:
            d = self.drop(c, "o" if self.asym else "both")
            if self.orch_ver == "new":
                r = self.orch(d, "send_failed")
                if r:
                    outs.append(r[0])
            else:
                outs.append(d)
        return outs

    def inst_get(self, c: MC4, k: int):
        for i in c.inst:
            if i[0] == k:
                return i
        return None

    def inst_put(self, c: MC4, k: int, ex: str, rp: str, rename: int | None = None, is_auto: bool = False) -> MC4:
        rest = [i for i in c.inst if i[0] != k]
        kk = rename if rename is not None else k
        if self.node_ver == "new":
            if not (ex == "declined" and rp == "r_none") and rp != "r_acked":
                rest.append((kk, ex, rp, is_auto))
        else:
            if kk != ASK and not (ex == "declined" and k == kk and self.inst_get(c, kk) is None):
                rest.append((kk, ex, rp, is_auto))
        return c._replace(inst=tuple(sorted(rest)))

    def node(
        self,
        c: MC4,
        k: int,
        event: str,
        *,
        rename: int | None = None,
        hole: str | None = None,
        auto_permit: bool = False,
        **facts,
    ) -> MC4 | None:
        cur_i = self.inst_get(c, k)
        i = cur_i or (k, "arrived", "r_none", auto_permit)
        f_items = tuple(sorted(facts.items())) if facts else ()
        r = fast_node_step(self.node_cls, i[1], i[2], event, f_items)
        if r is None:
            if hole:
                key = ("node", self.node_ver, event, i[1], i[2])
                self.holes[key] = self.holes.get(key, 0) + 1
            return None
        row, todo = r
        ex, rp = row["exec_"], row["report"]
        kk = rename if rename is not None else k
        is_auto = (i[3] or auto_permit) if (rename is not None or cur_i is not None) else auto_permit

        # 실행 시작 진입 판정: 두 번 실행(dbl)과 약속 다 위반(viol_c) 기록
        if ex == "running" and i[1] != "running":
            if c.tgt:
                c = c._replace(dbl=True)
            if is_auto and c.unack_fail:
                c = c._replace(viol_c=True)

        c = self.inst_put(c, k, ex, rp, rename, is_auto=is_auto)
        if kk == OWN and self.node_ver == "old" and ex in ("ok", "failed"):
            c = c._replace(inst=tuple(x for x in c.inst if x[0] != OWN))

        for t in todo:
            if t == "bundle":
                if kk == OWN:
                    if ex == "ok":
                        msgs = ["own_ok"]
                    elif self.node_ver == "new":
                        msgs = ["own_fail"]
                    else:
                        msgs = []
                else:
                    msgs = ["res", "set"] if ex == "ok" else ["fail"]
            elif t == "reval_fail":
                msgs = ["fail"]
            elif t == "auto_req":
                msgs = ["auto_req"]
            elif kk == OWN:
                msgs = []
            else:
                msgs = [t]
            for m in msgs:
                c = self.nsend(c, m, kk)
                if c is None:
                    return None
        return c

    def give_slot(self, c: MC4) -> MC4:
        if c.slot is not None:
            return c
        for i in c.inst:
            if i[1] == "queued":
                c2 = self.node(c, i[0], "slot_free")
                if c2 is not None:
                    return c2._replace(slot=i[0])
        return c

    def held(self, c: MC4) -> set[int]:
        return {i[0] for i in c.inst if i[1] in ("queued", "running") or i[2] == "r_owed"}

    def drop(self, c: MC4, ph: str) -> MC4:
        stale = c.stale_gen or (c.cur != 0 and ph in ("o", "both"))
        c = c._replace(qon=(), qno=(), ph=ph, stale_gen=stale)
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

    def reconnect(self, c: MC4) -> MC4:
        stale = c.stale_gen or (c.cur != 0)
        c = c._replace(ph="up", stale_gen=stale)
        if self.peer_has_cap and c.cur and c.bl in ("b_unknown", "b_queued", "b_running"):
            r = self.orch(c, "hello_holds" if c.cur in self.held(c) else "hello_missing")
            if r:
                c = r[0]
        for i in list(c.inst):
            c2 = self.node(c, i[0], "link_up")
            if c2 is not None:
                c = c2
        return c

    def node_recv(self, c: MC4, msg: str, k: int) -> MC4 | None:
        if msg == "probe":
            if c.cut:
                return self.nsend(c, "plan_closed", k)
            if self.manifest and c.fjournal != 0:
                return self.nsend(c, f"plan_exec_fj:{c.fjournal}", k)
            return self.nsend(c, "plan_exec", k)
        if msg in ("permit", "apermit"):
            is_auto = msg == "apermit"
            asking = self.inst_get(c, ASK)
            src = k
            unexpected = False
            if is_auto:
                if asking and asking[1] == "asking":
                    src = ASK
                else:
                    unexpected = True
            free = c.slot is None
            if self.node_ver == "new":
                allow_fail_guard = is_auto
                owed_kinds = ("ok", "failed") if allow_fail_guard else ("ok",)
                holds_other = any(
                    i[0] not in (k, src)
                    and (i[1] in ("queued", "running") or (i[1] in owed_kinds and i[2] == "r_owed"))
                    for i in c.inst
                )
                c2 = self.node(
                    c, src, "permit", rename=k, hole="받은 글",
                    auto_permit=is_auto, free=free, holds_other=holds_other, unexpected_auto=unexpected,
                )
                if c2 is None:
                    return c
                i = self.inst_get(c2, k)
                if i and i[1] == "running" and c2.slot is None:
                    c2 = c2._replace(slot=k)
                if holds_other or (is_auto and unexpected and allow_fail_guard):
                    for j in list(c2.inst):
                        if j[1] in owed_kinds and j[2] == "r_owed":
                            c3 = self.node(c2, j[0], "link_up")
                            if c3 is None:
                                return None
                            c2 = c3
                return c2
            else:
                if self.inst_get(c, k) is not None:
                    src = k
                    unexpected = False
                c2 = self.node(
                    c, src, "permit", rename=k, hole="받은 글",
                    auto_permit=is_auto, free=free, unexpected_auto=unexpected,
                )
                if c2 is None:
                    return c
                i = self.inst_get(c2, k)
                if i and i[1] == "running" and c2.slot is None:
                    c2 = c2._replace(slot=k)
                return c2
        if msg == "tack":
            c2 = self.node(c, k, "terminal_ack")
            return c2 if c2 is not None else c
        raise ValueError(msg)

    def _maybe_send_tack(self, c: MC4, k: int) -> list[MC4]:
        if self.peer_has_cap:
            return self.osend(c, "tack", k)
        return [c]
