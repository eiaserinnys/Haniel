"""Reachability and contract metrics for the bounded product."""

from collections import deque

from .system import OPEN, System

def explore(sysm):
    start = sysm.initial()
    parent = {start: None}
    edges: dict = {}
    queue = deque([start])
    while queue:
        c = queue.popleft()
        succ = sysm.successors(c)
        edges[c] = succ
        for label, kind, nx in succ:
            if nx not in parent:
                parent[nx] = (c, label)
                queue.append(nx)
    return parent, edges


def path_to(parent, c):
    steps = []
    while parent[c] is not None:
        c, label = parent[c]
        steps.append(label)
    return steps[::-1]


AUTO = {"system", "time", "link", "relink"}
NOLINK = {"system", "time", "link"}      # 노드가 돌아오지 않는다고 볼 때


def can_reach(edges, targets, kinds):
    """targets에 kinds의 걸음만으로 닿을 수 있는 구성들."""
    rev: dict = {}
    for c, succ in edges.items():
        for _, kind, nx in succ:
            if kind in kinds:
                rev.setdefault(nx, []).append(c)
    seen = set(targets)
    queue = deque(targets)
    while queue:
        c = queue.popleft()
        for p in rev.get(c, ()):
            if p not in seen:
                seen.add(p)
                queue.append(p)
    return seen


def run(name, show_paths=False, **kw):
    sysm = System(**kw)
    parent, edges = explore(sysm)
    reach = list(parent)

    success = [c for c in reach if c.st in ("success", "superseded")]
    way = can_reach(edges, success, AUTO)                       # 사람 없이 닫힐 길이 있다 (성공, 또는 새 커밋에 밀림)
    no_way = [c for c in reach if c not in way]
    may_fail = can_reach(edges, no_way, AUTO)                   # 사람 없이 가다가 길이 없는 구성에 빠질 수 있다

    applied_open = [c for c in reach if c.tgt and c.st in OPEN]
    a_way = [c for c in applied_open if c not in way]            # 약속 가 (길이 있다)
    a_must = [c for c in applied_open if c in may_fail]          # 약속 가 (반드시 닫힌다)

    not_dep = [c for c in reach if c.st != "deploying"]
    leave = can_reach(edges, not_dep, AUTO)
    b_way = [c for c in reach if c.st == "deploying" and c not in leave]            # 약속 나 (길이 있다)
    may_stay = can_reach(edges, [x for x in reach if x not in leave], AUTO)
    b_must = [c for c in reach if c.st == "deploying" and c in may_stay]            # 약속 나 (반드시)

    leave_nolink = can_reach(edges, not_dep, NOLINK)
    b_gone = [c for c in reach if c.st == "deploying" and c not in leave_nolink]     # 노드가 돌아오지 않으면 배포 중에 갇힌다
    false_ok = [c for c in reach if c.st == "success" and not c.tgt]                 # 성공인데 체크아웃이 목표가 아니다
    double = [c for c in reach if c.dbl]                                             # 목표에 있는데 또 실행했다
    no_debt = [c for c in reach if not any(i[2] == "r_owed" for i in c.inst)]
    clears = can_reach(edges, no_debt, AUTO)
    debt = [c for c in reach if c not in clears]                                     # 사람 없이는 남는 빚
    tagged = [c for c in reach if c.st in ("rejected", "superseded") and c.tgt]      # 닫힌 뒤 적용됨 (드러남)
    stuck13 = [c for c in reach if c.st in ("retry", "again") and c.cut and c not in way]   # 계획이 닫힘으로만 답한다
    again_n = [c for c in reach if c.st == "again"]
    again_way = can_reach(edges, [c for c in reach if c.st != "again"], AUTO)
    again_stay = [c for c in again_n if c not in again_way]                          # 사람 없이는 다시 대기에 남는다
    stuck_again = [c for c in stuck13 if c.st == "again"]
    stuck_retry = [c for c in stuck13 if c.st == "retry"]
    stuck_again_stay = [c for c in stuck_again if c not in again_way]                # 그 가운데 사람 없이는 다시 대기를 벗어나지 못하는 것
    promise3 = [c for c in reach if c.v3]                                            # 약속 다: 실패 뒤 사람 승인 없이 자동 허가로 다시 실행
    lock_bad = [c for c in reach if (c.st == "retry") != (c.lk == "l_on") and c.st in ("pending", "again", "retry")]
    failed_open = [c for c in reach if c.ff and c.st in ("pending", "again") and c.bl == "b_none"
                   and not c.qno and not any(i[2] == "r_owed" for i in c.inst)]      # 실패했는데 잠기지 않은 채 가라앉은 대기

    row = [("구성", len(reach)), ("가 길 없음", len(a_way)), ("가 반드시 아님", len(a_must)),
           ("나 길 없음", len(b_way)), ("나 반드시 아님", len(b_must)),
           ("돌아오지 않으면 갇힘", len(b_gone)), ("거짓 성공", len(false_ok)),
           ("두 번 실행", len(double)), ("남는 빚", len(debt)), ("빈칸", len(sysm.holes)),
           ("넘침", sysm.overflow), ("닫힌 뒤 적용", len(tagged)), ("닫힌 계획에 갇힘", len(stuck13)),
           ("갇힘 중 다시 대기", len(stuck_again)), ("갇힘 중 실패 뒤 대기", len(stuck_retry)),
           ("갇힘 중 다시 대기에 남음", len(stuck_again_stay)),
           ("다시 대기", len(again_n)), ("다시 대기에 남음", len(again_stay)),
           ("약속 다", len(promise3)), ("잠금 불일치", len(lock_bad)), ("실패했는데 안 잠긴 대기", len(failed_open))]
    print(f"{name}: " + " | ".join(f"{k} {v}" for k, v in row))
    for key, cnt in sorted(sysm.holes.items(), key=lambda kv: -kv[1])[:8]:
        print(f"      빈칸: {key} ({cnt}번)")
    if show_paths:
        for nm, bad in (("가 길 없음", a_way), ("가 반드시 아님", a_must), ("나 길 없음", b_way),
                        ("나 반드시 아님", b_must), ("돌아오지 않으면 갇힘", b_gone), ("거짓 성공", false_ok), ("두 번 실행", double),
                        ("남는 빚", debt),
                        ("약속 다", promise3), ("잠금 불일치", lock_bad), ("실패했는데 안 잠긴 대기", failed_open)):
            if bad:
                c = min(bad, key=lambda x: len(path_to(parent, x)))
                print(f"      {nm}: " + " → ".join(path_to(parent, c)))
                print(f"         {c}")
    return dict(row)
