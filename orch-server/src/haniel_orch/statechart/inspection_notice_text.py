"""점검 필요 알림의 본문(카드 #1260).

받는 쪽(소울스트림 세션)에 그대로 보이는 한국어 글이다. 맨 첫 줄은 고정 말머리와 묶음 요약이고, 그
다음 줄부터 지시문은 코드 상수로 두며, 그 뒤에 요청 한 줄마다 구조 필드를 붙인다. 값이 없으면
「미확인」이고, reason과 error는 비밀값을 가린다. 이 모듈은 DB나 설정을 모른다(순수 함수만 둔다).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

UNKNOWN = "미확인"
ERROR_LIMIT = 1500
HEADER = "[하니엘 점검 필요]"

INSTRUCTION_TEXT = "\n".join(
    (
        "하니엘 배포가 실패해 이 대상의 자동 배포와 일괄 승인(approve-all)이 멈췄다. "
        "개별 승인(POST /api/orch/approve, haniel_approve.py)은 잠금 없이 통과한다.",
        "이 배포를 맡은 카드가 있으면 그 담당이 원인을 고친 뒤 개별 승인으로 다시 돌린다. "
        "점검 카드를 따로 열 필요는 없다.",
        "노드 설정에서 이미 빠진 저장소이거나 더 배포하지 않을 대상이면 "
        "거절(POST /api/orch/reject, haniel_reject.py)로 닫는다.",
    )
)

# 가리는 순서: 설정된 값(리터럴) → Authorization → URL 자격 증명 → 이름=값 → 토큰 모양.
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_URL_CREDENTIALS = re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@")
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:token|key|password|secret))\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)
_GITHUB_TOKEN = re.compile(r"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]+")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+")


def redact_secrets(text: str, extra: tuple[str, ...] = ()) -> str:
    """비밀값을 `***`로 바꾼다. extra는 설정에서 온 값처럼 모양으로 알아볼 수 없는 것을 넘긴다."""
    redacted = text
    for value in extra:
        if value:
            redacted = redacted.replace(value, "***")
    redacted = _BEARER.sub("Bearer ***", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1***@", redacted)
    redacted = _ASSIGNMENT.sub(r"\1=***", redacted)
    redacted = _GITHUB_TOKEN.sub("***", redacted)
    redacted = _SK_TOKEN.sub("***", redacted)
    return redacted


def _shown(value: Any) -> str:
    """값이 없으면 미확인. 숫자 0은 값이다."""
    if value is None:
        return UNKNOWN
    text = str(value)
    if not text.strip():
        return UNKNOWN
    return text


def _shown_redacted(value: Any, *, limit: int | None = None) -> str:
    if value is None:
        return UNKNOWN
    text = redact_secrets(str(value))
    if limit is not None:
        text = text[:limit]
    if not text.strip():
        return UNKNOWN
    return text


def render_notice_text(
    *,
    node_id: str | None,
    repo: str | None,
    branch: str | None,
    target_head: str | None,
    deploy_id: str,
    notice_id: str,
    sm_status: str | None,
    event: str | None,
    failure: Mapping[str, Any] | None,
    prior_notice_count: int,
) -> str:
    """알림 한 줄의 본문. 맨 앞 지시문은 보내는 쪽이 붙이므로 여기에는 구조 필드만 둔다."""
    failure = failure or {}
    return "\n".join(
        (
            f"node_id: {_shown(node_id)}",
            f"repo: {_shown(repo)}",
            f"branch: {_shown(branch)}",
            f"target commit: {_shown(target_head)}",
            f"deploy_id: {_shown(deploy_id)}",
            f"알림 id: {_shown(notice_id)}",
            f"sm_status: {_shown(sm_status)}",
            f"잠금을 켠 사건 이름: {_shown(event)}",
            f"last_failure kind: {_shown(failure.get('kind'))}",
            f"attempt_id: {_shown(failure.get('attempt_id'))}",
            f"reason: {_shown_redacted(failure.get('reason'))}",
            f"error: {_shown_redacted(failure.get('error'), limit=ERROR_LIMIT)}",
            f"at: {_shown(failure.get('at'))}",
            f"같은 요청이 전에 알림을 받은 횟수: {prior_notice_count}",
        )
    )


def _bundle_summary(notices: list[Mapping[str, Any]]) -> str:
    """묶음의 줄들만으로 정해지는 첫 줄. 시각이나 시도 수처럼 바뀌는 값은 넣지 않는다."""
    first = notices[0]
    count = len(notices)
    rest = f" 외 {count - 1}" if count > 1 else ""
    return f"{HEADER} {_shown(first.get('node_id'))} {count}건: {_shown(first.get('repo'))}{rest}"


def compose_notice_message(notices: list[Mapping[str, Any]]) -> str:
    """첫 줄 묶음 요약 + 지시문 + 요청 블록들. 같은 묶음은 언제 보내도 같은 본문이 된다."""
    return "\n\n".join(
        (_bundle_summary(notices), INSTRUCTION_TEXT, *(notice["text"] for notice in notices))
    )
