"""그림과 표에 쓰는 한글 표시 이름 사전.

상태, 사건, 조건마다 읽는 사람이 겪는 일의 말을 단다. 이름이 없는 상태나
전이가 있으면 생성기(`docs`)가 실패한다. 사건 코드 이름은 그림에 쓰지 않는다.
"""

from __future__ import annotations

STATE_LABELS: dict[str, dict[str, str]] = {
    "OrchRequest": {
        "req": "배포 요청 한 건",
        "status": "요청 상태",
        "open_": "열림",
        "closed": "닫힘",
        "pending": "처음 대기",
        "retry": "표시 줄 있는 대기",
        "deploying": "배포 중",
        "success": "성공",
        "rejected": "거절",
        "superseded": "새 커밋에 밀림",
        "lock": "점검 필요",
        "l_off": "점검 필요 없음",
        "l_on": "점검 필요",
    },
    "AttemptEvidence": {
        "ev": "종결 증거",
        "open_": "열림",
        "closed": "닫힘",
        "ev_none": "증거 아직 없음",
        "ev_result": "성공 결과만 옴",
        "ev_settled": "체크아웃 대조만 옴",
        "ev_both": "성공으로 종결",
        "ev_failed": "실패로 종결",
        "ev_superseded": "노드의 배포 성공에 밀려 종결",
    },
}

EVENT_LABELS: dict[str, dict[str, str]] = {
    "OrchRequest": {
        "begin_attempt": "승인해서 시도를 시작한다",
        "attempt_succeeded": "시도가 성공으로 끝난다",
        "attempt_failed": "시도가 실패로 끝난다",
        "send_failed": "허가가 노드에 닿지 못해 시도를 닫는다",
        "reject": "거절한다",
        "newer_commit": "더 새 커밋이 들어온다",
        "node_succeeded": "노드가 자기 배포의 성공을 알린다",
        "node_failed": "노드가 자기 배포의 실패를 알린다",
        "observed": "목표 커밋에 와 있다고 관측한다",
        "observed_self_update": "더 새 관측이 자기 갱신을 닫는다",
        "reopen": "보류 시한이 지나 되살린다",
    },
    "AttemptEvidence": {
        "result_ok": "성공 결과가 온다",
        "settled_ok": "목표와 같은 대조가 온다",
        "settled_noted": "대조가 온다",
        "fail": "실패로 닫는다",
        "recovered": "증거 회수가 검증을 통과한다",
        "superseded_by_node": "노드의 배포 성공이 밀어낸다",
    },
}

COND_LABELS: dict[str, dict[str, str]] = {
    "OrchRequest": {
        "newer": "더 새 커밋이 있음",
        "not_newer": "더 새 커밋이 없음",
        "manual_single": "승인 원천이 단건 승인",
        "may_open": "점검 필요가 없거나 승인 원천이 단건 승인",
        "open_request": "요청이 열려 있음",
        "locked": "점검 필요 켜짐",
        "unlocked": "점검 필요 꺼짐",
        "heads_equal": "머리 셋이 같음",
        "no_active_attempt": "도는 시도 없음",
        "no_failed_node_attempt": "실패한 노드 보고 없음",
        "self_update": "자기 갱신",
        "postponed_due": "보류 시한 지남",
        "no_other_open": "다른 열린 줄 없음",
    },
    "AttemptEvidence": {
        "execute_mode": "execute 방식",
        "not_execute_mode": "execute 방식 아님",
        "recovery_mode": "증거 회수 방식",
    },
}

# 전이와 함께 같은 트랜잭션에 쓰는 것(표의 「함께 쓰는 것」 칸).
# 키는 (사건, 도착 잎, 출발 잎), (사건, 도착 잎), (사건, None) 순으로 찾는다.
EFFECTS: dict[str, dict[tuple, str]] = {
    "OrchRequest": {
        ("begin_attempt", "l_off"): "점검 필요를 내린다(바깥 칸: 표시 줄의 inspection_required를 0으로). 내릴 수 있는 원천은 단건 승인 하나다",
        ("begin_attempt", None): "approved_by를 적는다. 시도 줄은 같은 걸음에서 만든다",
        ("attempt_succeeded", None): "error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다",
        ("attempt_failed", "superseded"): "reject_reason을 `superseded by newer canonical`로 적는다(바깥 status는 rejected가 된다). 표시 줄과 계보 줄을 지운다",
        ("attempt_failed", "retry"): "approved_by, error, duration_ms를 지운다. 표시 줄을 세우고(source를 이 시도로 옮긴다) 계보 줄을 더한다",
        ("attempt_failed", "l_on"): "점검 필요를 켠다(표시 줄이 있으면 inspection_required를 1로). 요청이 이미 닫혀 있어도 켜고, 그때 요청 줄의 다른 칸과 표시 줄은 쓰지 않는다",
        ("send_failed", "superseded"): "reject_reason을 `superseded by newer canonical`로 적는다(바깥 status는 rejected가 된다). 표시 줄과 계보 줄을 지운다",
        ("send_failed", "retry"): "approved_by, error, duration_ms를 지운다. 표시 줄을 세우고 계보 줄을 더한다. 실행하지 않았음이 확실하므로 source는 옮기지 않는다(첫 시도면 빈 칸)",
        ("send_failed", "l_on"): "점검 필요를 켠다(표시 줄이 있으면 inspection_required를 1로). 옛 노드 규칙이라 보내기 실패도 켠다",
        ("reject", None): "reject_reason을 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다",
        ("newer_commit", None): "reject_reason을 `superseded by <끝 줄>`로 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다",
        ("node_succeeded", None): "approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다",
        ("node_failed", "retry"): "배포 중이었으면 approved_by, error, duration_ms를 지운다. 표시 줄이 없으면 두 시도 번호 칸이 빈 표시 줄을 만든다",
        ("node_failed", "l_on"): "점검 필요를 켠다(표시 줄이 없으면 두 시도 번호 칸이 빈 표시 줄을 만든다). 도는 시도가 있는 배포 중이면 칸만 켜고 요청 상태와 시도는 그대로 둔다",
        ("observed", None): "status와 잎만 바꾼다",
        ("observed_self_update", None): "표시 줄과 계보 줄을 지운다",
        ("reopen", "retry"): "reject_reason, postponed_until, approved_by, error를 지운다. 두 시도 번호 칸이 빈 표시 줄을 만든다(점검 필요는 켜진 채 그대로)",
        ("reopen", None): "reject_reason, postponed_until, approved_by, error를 지운다",
    },
    "AttemptEvidence": {
        ("result_ok", None): "result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다",
        ("settled_ok", None): "settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다",
        ("settled_noted", None): "settled 세 칸만 적는다",
        ("fail", None): "outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다",
        ("recovered", None): "outcome을 evidence_recovered_success로 닫는다",
        ("superseded_by_node", None): "outcome을 superseded로 닫고 terminal 네 칸을 적는다",
    },
}


def effect_for(name: str, event: str, target: str, source: str) -> str | None:
    """전이에 딸린 쓰기의 설명. 없으면 None."""
    effects = EFFECTS[name]
    for key in ((event, target, source), (event, target), (event, None)):
        if key in effects:
            return effects[key]
    return None
