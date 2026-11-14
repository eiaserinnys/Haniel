"""잎 칸과 옛 칸 사이의 대응(순수 함수).

잎 칸(`deploy_events.sm_status`, `deploy_attempts.sm_evidence`)이 정본이고, 옛 칸은
한 걸음이 같은 트랜잭션에 함께 쓴다. 기동 때 잎이 비었거나 바깥 칸과 어긋난 줄만
옛 칸에서 다시 계산한다. 되돌렸다 다시 올린 경우도 이것으로 맞춰진다.
"""

from __future__ import annotations

from .definitions import REQUEST_OUTER_STATUS

SUPERSEDED_PREFIX = "superseded by "

# 잎 칸은 저장소 안쪽의 표현이다. REST, 방송 글, 저장소 반환값의 모양은 바뀌지 않는다.
LEAF_COLUMNS = frozenset({"sm_status", "sm_lock", "sm_evidence"})

# 영역 이름 = 칸 이름의 접미를 뗀 이름
REQUEST_REGION = "status"
LOCK_REGION = "lock"
ATTEMPT_REGION = "ev"

LOCK_ON = "l_on"
LOCK_OFF = "l_off"


def request_leaf_from_columns(
    status: str, reject_reason: str | None, has_marker: bool
) -> str | None:
    """옛 칸(status, reject_reason 접두, 표시 줄 유무)에서 요청 잎을 계산한다.

    정의에 잎이 없는 값(옛 `approved`, `failed`)은 None이다.
    """
    if status == "pending":
        return "retry" if has_marker else "pending"
    if status == "deploying":
        return "deploying"
    if status == "success":
        return "success"
    if status == "rejected":
        if reject_reason is not None and reject_reason.startswith(SUPERSEDED_PREFIX):
            return "superseded"
        return "rejected"
    return None


def request_leaf_is_consistent(
    leaf: str | None, status: str, has_marker: bool
) -> bool:
    """잎이 바깥 칸과 표시 줄 유무에 맞는가. 비었거나 어긋나면 다시 계산한다."""
    if leaf is None or leaf not in REQUEST_OUTER_STATUS:
        return False
    if REQUEST_OUTER_STATUS[leaf] != status:
        return False
    if leaf == "pending" and has_marker:
        return False
    return not (leaf == "retry" and not has_marker)


def lock_leaf_from_columns(
    status: str, has_marker: bool, inspection_required: bool | None
) -> str:
    """옛 칸에서 점검 필요 잎을 계산한다(sm_lock이 비어 있는 줄에만 쓴다).

    표시 줄이 있으면 켜짐이다(닫힌 줄에 남은 표시 줄 포함). 다만 배포 중인 줄은 표시 줄의
    inspection_required를 그대로 읽는다. 표시 줄이 없으면 꺼짐이다.
    """
    if not has_marker:
        return LOCK_OFF
    if status == "deploying":
        return LOCK_ON if inspection_required else LOCK_OFF
    return LOCK_ON


def attempt_leaf_from_columns(
    outcome: str, result_status: str | None, settled_at: str | None
) -> str | None:
    """옛 칸(outcome, result_status, settled_at)에서 시도 증거 잎을 계산한다."""
    if outcome == "active":
        if result_status == "success":
            return "ev_result"
        if settled_at is not None:
            return "ev_settled"
        return "ev_none"
    if outcome in ("success", "evidence_recovered_success"):
        return "ev_both"
    if outcome == "failed":
        return "ev_failed"
    if outcome == "superseded":
        return "ev_superseded"
    return None


# 잎이 가리키는 outcome 칸 값. ev_both는 두 값이 가능하다.
ATTEMPT_OUTCOMES = {
    "ev_none": ("active",),
    "ev_result": ("active",),
    "ev_settled": ("active",),
    "ev_both": ("success", "evidence_recovered_success"),
    "ev_failed": ("failed",),
    "ev_superseded": ("superseded",),
}


def attempt_leaf_is_consistent(leaf: str | None, outcome: str) -> bool:
    return leaf in ATTEMPT_OUTCOMES and outcome in ATTEMPT_OUTCOMES[leaf]
