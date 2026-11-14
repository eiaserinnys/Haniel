"""Compare false-success equivalence classes from the mixed-version experiment."""

from .explore import explore, path_to
from .mixed_system import MixedSystem

def behavioral_key(c):
    """옛 노드와 새 오케 비교 시, 이미 닫힌 시도에 사후 기록된 증거(ev, sm) 차이만 접은 동작 동치 키.
    현재 열린 시도 cur의 증거만 남기고 닫힌 시도의 ev와 sm, stale_gen/mlock 표지 차이를 정규화한다.
    """
    cur_ev = c.ev[c.cur - 1] if c.cur != 0 else "none"
    norm_bl = "b_active" if c.bl in ("b_unknown", "b_running") else c.bl
    return (
        c.st, norm_bl, c.cur, c.opened, c.pk, c.pcnt, c.pa, cur_ev,
        c.newer, c.inst, c.slot, c.tgt, c.cut, c.boot, c.dbl, c.rs, c.asks, c.qon, c.qno, c.ph,
    )



def false_success_classes(options, orch_ver):
    system = MixedSystem(orch_ver=orch_ver, node_ver="old", **options)
    parent, edges = explore(system)
    false = [state for state in parent if state.st == "success" and not state.tgt]
    keys = {}
    for state in false:
        key = behavioral_key(state)
        if key not in keys:
            keys[key] = (state, path_to(parent, state))
    assert not system.holes, system.holes
    assert system.overflow == 0
    print(f"{orch_ver} x old: 구성 {len(parent)}, 거짓 성공 {len(false)}, 동치 {len(keys)}", flush=True)
    return keys


def compare_false_success(name, options):
    old = false_success_classes(options, "old")
    new = false_success_classes(options, "new")
    assert_no_new_classes(name, old, new)


def assert_no_new_classes(name, old, new):
    extra = set(new) - set(old)
    print(f"섞인 판 {name}: 옛 동치 {len(old)}, 새 동치 {len(new)}, 새 동치 추가 {len(extra)}", flush=True)
    if extra:
        state, path = new[next(iter(extra))]
        print("새 거짓 성공 구성", state, "걸음", path, flush=True)
    assert not extra, (name, len(extra))


def check_new_pair(name, options):
    from .explore import AUTO, NOLINK, can_reach, path_to

    system = MixedSystem(orch_ver="new", node_ver="new", **options)
    parent, edges = explore(system)
    reach = list(parent)
    closed = [c for c in reach if c.st in ("success", "superseded")]
    way = can_reach(edges, closed, AUTO)
    may_fail = can_reach(edges, [c for c in reach if c not in way], AUTO)
    applied = [c for c in reach if c.tgt and c.st in ("pending", "again", "retry", "deploying")]
    not_deploying = [c for c in reach if c.st != "deploying"]
    leave = can_reach(edges, not_deploying, AUTO)
    may_stay = can_reach(edges, [c for c in reach if c not in leave], AUTO)
    leave_no_link = can_reach(edges, not_deploying, NOLINK)
    bad = {
        "가 길 없음": [c for c in applied if c not in way],
        "가 반드시 아님": [c for c in applied if c in may_fail],
        "나 길 없음": [c for c in reach if c.st == "deploying" and c not in leave],
        "나 반드시 아님": [c for c in reach if c.st == "deploying" and c in may_stay],
        "돌아오지 않으면 갇힘": [c for c in reach if c.st == "deploying" and c not in leave_no_link],
        "약속 다": [c for c in reach if c.viol_c],
    }
    print(f"새 둘 {name}: 구성 {len(reach)}, " + ", ".join(f"{key} {len(value)}" for key, value in bad.items()), flush=True)
    for key, states in bad.items():
        if states:
            state = min(states, key=lambda item: len(path_to(parent, item)))
            print(key, state, path_to(parent, state), flush=True)
    assert not system.holes, system.holes
    assert system.overflow == 0
    assert not any(bad.values())
