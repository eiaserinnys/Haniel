"""설계(design.md와 fable_r4_product.py)와 다른 전이의 표시. 그림과 표가 읽는다.

`definitions`가 재노출한다(2b-2에서 정의 모듈의 크기를 지키려 옮겼다).
"""

from __future__ import annotations

# 설계(design.md와 fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이의 표시.
# 키는 (정의 이름, 출발 잎, 사건, 도착 잎) 또는 조건까지 가르는 (정의 이름, 출발 잎, 사건, 도착 잎, 조건 튜플).
# 같은 출발과 도착에 설계대로인 가지와 다른 가지가 함께 있으면 조건 튜플로 가른다(보내기 실패의 판 없는 가지).
# 값은 (종류, 그림에 쓰는 글).
# BREAKS는 설계가 어긋남으로 적은 것(번호, PR 2a에서 어긋남 8이 없어져 지금은 없다), CHANGES는 설계 글이 옛 노드 규칙이나
# PR 2에서 바꾼다고 적은 것(근거 줄을 DESIGN_LINES에 둔다), ABSENT는 설계에 없는 전이다. 근거 줄이 없으면 CHANGES를 달지 않는다.
# 증거 정의의 실행 방식 조건, 증거 회수, 노드 성공이 시도를 밀어냄, dup 제자리 전이는 설계 글이 받치므로 표시가 없다.
BREAKS = "breaks"
CHANGES = "changes"
ABSENT = "absent"
_PR2 = "PR 2에서 바뀜"
_ABSENT = "설계에 없음"
DIVERGENCE: dict[tuple, tuple[str, str]] = {
    # 보내기 실패(어긋남 9)의 판 없는 가지: 설계는 점검 필요를 세우지 않고 다시 대기로 보낸다. 옛 노드 규칙은 모든 닫힘이
    # 점검 필요를 세운다. 판 1 가지(다시 대기, 서 있는 lock에서 실패 뒤 대기)는 설계대로다
    ("OrchRequest", "deploying", "send_failed", "retry", ("is_unknown", "not_newer", "legacy")): (CHANGES, _PR2),
    ("OrchRequest", "l_off", "send_failed", "l_on"): (CHANGES, _PR2),
    # 도는 시도 없는 노드 실패 보고(W17): design.md 125줄이 점검 필요를 세워 실패 뒤 대기로 보낸다고 바꾼다
    ("OrchRequest", "deploying", "node_failed", "retry"): (CHANGES, _PR2),
    # 닫힌 시도의 늦은 성공: 판 1은 뒤 시도의 실행 시작으로(design.md:123), 판 없음은 뒤 시도를 연 이력으로 가른다(완화 A, 431줄)
    ("OrchRequest", "pending", "success_evidence", "success"): (CHANGES, _PR2),
    ("OrchRequest", "again", "success_evidence", "success"): (CHANGES, _PR2),
    ("OrchRequest", "retry", "success_evidence", "success"): (CHANGES, _PR2),
    ("OrchRequest", "deploying", "success_evidence", "success"): (CHANGES, _PR2),
    # 자기 갱신 관측이 고른 다른 줄을 닫음(W4): design.md 382줄은 처음 대기이고 점검 필요가 없을 때만 닫는다
    ("OrchRequest", "pending", "observed_self_update", "success"): (CHANGES, _PR2),
    ("OrchRequest", "again", "observed_self_update", "success"): (CHANGES, _PR2),
    ("OrchRequest", "retry", "observed_self_update", "success"): (CHANGES, _PR2),
    # 보류 자기 갱신 되살림(W5): 닫힘에서 열림으로 돌아가는 전이는 설계에 없다. 자기 갱신 영역은 둘째 걸음(design.md 489줄)
    ("OrchRequest", "rejected", "reopen", "pending"): (ABSENT, _ABSENT),
    ("OrchRequest", "superseded", "reopen", "pending"): (ABSENT, _ABSENT),
    ("OrchRequest", "rejected", "reopen", "retry"): (ABSENT, _ABSENT),
    ("OrchRequest", "superseded", "reopen", "retry"): (ABSENT, _ABSENT),
}

# 바뀐다는 표시(CHANGES)와 설계에 없다는 표시(ABSENT)의 근거: design.md 줄 번호
DESIGN_LINES: dict[tuple, str] = {
    ("OrchRequest", "deploying", "send_failed", "retry", ("is_unknown", "not_newer", "legacy")): "design.md:429-430",
    ("OrchRequest", "l_off", "send_failed", "l_on"): "design.md:429-430",
    ("OrchRequest", "deploying", "node_failed", "retry"): "design.md:125",
    ("OrchRequest", "pending", "success_evidence", "success"): "design.md:123, 431",
    ("OrchRequest", "again", "success_evidence", "success"): "design.md:123, 431",
    ("OrchRequest", "retry", "success_evidence", "success"): "design.md:123, 431",
    ("OrchRequest", "deploying", "success_evidence", "success"): "design.md:123, 431",
    ("OrchRequest", "pending", "observed_self_update", "success"): "design.md:382, 489",
    ("OrchRequest", "again", "observed_self_update", "success"): "design.md:382, 489",
    ("OrchRequest", "retry", "observed_self_update", "success"): "design.md:382, 489",
    ("OrchRequest", "rejected", "reopen", "pending"): "design.md:489",
    ("OrchRequest", "superseded", "reopen", "pending"): "design.md:489",
    ("OrchRequest", "rejected", "reopen", "retry"): "design.md:489",
    ("OrchRequest", "superseded", "reopen", "retry"): "design.md:489",
}


def mark_for(definition: str, source: str, event: str, target: str, conds: tuple[str, ...]) -> tuple[str, str] | None:
    """전이의 표시. 조건까지 가른 키를 먼저, 그다음 네 칸 키를 본다."""
    mark = DIVERGENCE.get((definition, source, event, target, conds))
    if mark is None:
        mark = DIVERGENCE.get((definition, source, event, target))
    return mark


def design_line_for(definition: str, source: str, event: str, target: str, conds: tuple[str, ...]) -> str | None:
    line = DESIGN_LINES.get((definition, source, event, target, conds))
    if line is None:
        line = DESIGN_LINES.get((definition, source, event, target))
    return line
