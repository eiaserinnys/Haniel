"""받기 함수(순수): 노드 글, 마감, 끊김, hello를 요청 정의의 (사건, 사실)로 바꾼다(카드 #1259 PR 2b-2, 13항).

저장소를 모른다. 코디네이터와 저장소가 줄에서 읽은 값(시도의 node_protocol, 요청의 믿음 잎, 마감 종류, 더 새 커밋 여부)을
넘기고, 2b-3의 곱 훑기가 같은 함수를 부른다. 글의 신원 확인(시도 번호, 요청 번호, 노드)은 호출부가 먼저 한다.

판 없는 시도(node_protocol이 None)는 2a와 2b-1 규칙 그대로다: 마감 하나(실행 임대)가 attempt_failed로 들어가고,
「실행하지 않음」 답도 attempt_failed이며, 받았다와 hello 대조는 없다(wire-v1 4절).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 시도 줄의 closure 칸 값. 닫힘 일곱을 세 종류로 가른다(design.md:153-162). 「아니오」 둘은 점검 필요를 세우지 않는다
CLOSURE_NOT_EXECUTED = "not_executed"  # 실행하지 않음: 보내기 실패, 노드의 「실행하지 않음」 답
CLOSURE_EXECUTION_UNKNOWN = "execution_unknown"  # 실행 여부 불명: 짧은 마감, hello 목록에 없는 닿았는지 모름과 줄 서 있음
CLOSURE_FAILED = "failed"  # 실패: 노드 실패 보고, 실행 임대 마감, 유예 마감, hello 목록에 없는 시작했다
CLOSURE_NO = frozenset({CLOSURE_NOT_EXECUTED, CLOSURE_EXECUTION_UNKNOWN})

# 시도 줄의 deadline_kind 칸 값
DEADLINE_LEASE = "lease"  # 실행 임대(판 없는 시도의 유일한 마감, 판 1의 시작했다)
DEADLINE_ACK = "ack"  # 닿았는지 모름의 짧은 마감(판 1)
DEADLINE_GRACE = "grace"  # 줄 서 있음이고 끊긴 동안의 유예 마감(판 1)
DEADLINE_NONE = "none"  # 줄 서 있음이고 붙어 있는 동안: 세지 않는다(deadline_at은 마지막 값 그대로, 읽지 않는다)

BELIEF_UNKNOWN = "b_unknown"
BELIEF_QUEUED = "b_queued"
BELIEF_RUNNING = "b_running"
HELD = frozenset({BELIEF_UNKNOWN, BELIEF_QUEUED, BELIEF_RUNNING})


@dataclass(frozen=True)
class Intake:
    """요청 정의에 넣을 사건 하나와 그 사실."""

    event: str
    facts: dict[str, bool] = field(default_factory=dict)


def is_legacy(node_protocol: int | None) -> bool:
    """판 없는 시도인가(wire-v1 1절: hello에 deploy_protocol이 없거나 None이면 옛 노드)."""
    return node_protocol is None or node_protocol < 1


def intake_received(*, node_protocol: int | None, active: bool) -> Intake | None:
    """받았다(deploy_attempt_received). 판 1의 도는 시도에만 믿음을 옮긴다. 닫힌 시도의 것은 버린다."""
    if is_legacy(node_protocol) or not active:
        return None
    return Intake("acked")


def intake_progress(*, active: bool) -> Intake | None:
    """진행 알림은 「시작했다」다(wire-v1 2절). 닫힌 시도의 것은 믿음을 옮기지 않는다."""
    return Intake("started") if active else None


def intake_terminal(*, node_protocol: int | None, belief: str, newer: bool) -> Intake:
    """노드의 deploy_attempt_terminal. 판 1 시도가 시작하기 전(닿았는지 모름, 줄 서 있음)이면 「실행하지 않음」 닫힘이고,
    시작했다 뒤에 오면 실제 실행으로 보아 실패 닫힘이다(4e). 판 없는 시도는 지금처럼 실패 닫힘이다."""
    if not is_legacy(node_protocol) and belief in (BELIEF_UNKNOWN, BELIEF_QUEUED):
        return Intake("declined", {"newer": newer})
    return Intake("attempt_failed", {"newer": newer})


def intake_send_failed(*, node_protocol: int | None, newer: bool) -> Intake:
    """허가 보내기 실패. 판을 사실로 넘겨 정의가 점검 필요 규칙을 가른다."""
    return Intake("send_failed", {"newer": newer, "legacy": is_legacy(node_protocol)})


def intake_deadline(
    *, node_protocol: int | None, deadline_kind: str | None, belief: str, newer: bool
) -> Intake | None:
    """마감 타이머. 마감 종류와 믿음이 맞을 때만 사건이고, 맞지 않으면 낡은 타이머(None)다(8항).

    판 없는 시도는 마감 하나(실행 임대)가 믿음과 상관없이 attempt_failed다.
    """
    if is_legacy(node_protocol):
        if deadline_kind in (None, DEADLINE_LEASE):
            return Intake("attempt_failed", {"newer": newer})
        return None
    if deadline_kind == DEADLINE_ACK and belief == BELIEF_UNKNOWN:
        return Intake("ack_deadline", {"newer": newer})
    if deadline_kind == DEADLINE_GRACE and belief == BELIEF_QUEUED:
        return Intake("grace_deadline", {"newer": newer})
    if deadline_kind == DEADLINE_LEASE and belief == BELIEF_RUNNING:
        return Intake("lease_deadline", {"newer": newer})
    return None


def intake_link_down() -> Intake:
    """오케스트레이터가 끊김을 안다. 계획 묻기를 닫고, 줄 서 있음은 호출부가 유예 마감을 건다."""
    return Intake("link_down")


def intake_hello(
    *,
    node_protocol: int | None,
    belief: str,
    held: set[str] | None,
    attempt_id: str,
    newer: bool,
) -> Intake | None:
    """다시 붙은 노드의 hello 목록과 지금 시도를 견준다(design.md:141-145).

    목록이 None(옛 노드)이거나 판 없는 시도이거나 믿음이 쥔 것이 아니면 대조하지 않는다.
    """
    if held is None or is_legacy(node_protocol) or belief not in HELD:
        return None
    if attempt_id in held:
        return Intake("hello_holds")
    return Intake("hello_missing", {"newer": newer})


def closure_of(event: str, belief: str) -> str:
    """닫힘 사건과 그때의 믿음에서 시도 줄의 closure 칸 값을 정한다."""
    if event in ("send_failed", "declined"):
        return CLOSURE_NOT_EXECUTED
    if event == "ack_deadline":
        return CLOSURE_EXECUTION_UNKNOWN
    if event == "hello_missing":
        return CLOSURE_FAILED if belief == BELIEF_RUNNING else CLOSURE_EXECUTION_UNKNOWN
    return CLOSURE_FAILED
