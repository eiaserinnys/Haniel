"""점검 필요 알림의 본문(카드 #1260).

받는 쪽(소울스트림 세션)에 그대로 보이는 한국어 글이다. 맨 앞의 지시문은 코드 상수로 두고, 그 뒤에
요청 한 줄마다 구조 필드를 붙인다. 값이 없으면 「미확인」이고, reason과 error는 비밀값을 가린다.
이 모듈은 DB나 설정을 모른다(순수 함수만 둔다).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

UNKNOWN = "미확인"
ERROR_LIMIT = 1500

INSTRUCTION_TEXT = "\n".join(
    (
        "하니엘 배포가 실패해 점검 필요 잠금이 걸렸다. 🪽 하니엘 폴더에 서소영(카드 담당) 점검 카드를 열어 맡긴다.",
        "점검 담당은 원인을 찾고, 무엇이 바뀌어 다음 시도가 다르게 끝날지를 카드에 적은 뒤에만 "
        "haniel_approve.py --inspected-card로 하나씩 다시 승인한다.",
        "노드의 로컬 작업을 지워야 할 때, 원인을 찾지 못했을 때, 점검 뒤 다시 승인했는데 또 실패했을 때는 "
        "디렉터께 카드 질문으로 올린다.",
        "요청이 이미 닫혔거나 더 새 요청이 있으면 현 상태부터 확인한다.",
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


def compose_notice_message(texts: list[str]) -> str:
    """맨 앞 지시문 + 요청 블록들. 같은 묶음은 언제 보내도 같은 본문이 된다."""
    return "\n\n".join((INSTRUCTION_TEXT, *texts))
