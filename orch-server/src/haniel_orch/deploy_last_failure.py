"""대기 목록에 싣는 「직전 실패 요약」(카드 #1259 PR 2a).

점검하는 사람이 무엇을 볼지 알 수 있도록, 요청마다 가장 나중에 실패로 끝난 시도 하나를 요약한다.
오케스트레이터 시도(deploy_attempts)와 노드가 스스로 한 배포(node_deploy_attempts)를 함께 본다.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

KIND_ORCHESTRATOR_ATTEMPT = "orchestrator_attempt"
KIND_NODE_DEPLOY = "node_deploy"


async def last_failure(db: aiosqlite.Connection, deploy_id: str) -> dict[str, Any] | None:
    """직전 실패 요약: 종류, 시도 번호, 사유, 오류, 시각. 실패한 시도가 없으면 None."""
    cursor = await db.execute(
        "SELECT orchestrator_attempt_id, terminal_reason, terminal_error, completed_at "
        "FROM deploy_attempts WHERE deploy_id = ? AND outcome = 'failed' "
        "ORDER BY completed_at DESC LIMIT 1",
        (deploy_id,),
    )
    attempt = await cursor.fetchone()
    cursor = await db.execute(
        "SELECT node_attempt_id, error, completed_at FROM node_deploy_attempts "
        "WHERE deploy_id = ? AND outcome = 'failed' "
        "ORDER BY completed_at DESC LIMIT 1",
        (deploy_id,),
    )
    node = await cursor.fetchone()
    candidates: list[dict[str, Any]] = []
    if attempt is not None:
        candidates.append(
            {
                "kind": KIND_ORCHESTRATOR_ATTEMPT,
                "attempt_id": attempt[0],
                "reason": attempt[1],
                "error": attempt[2],
                "at": attempt[3],
            }
        )
    if node is not None:
        candidates.append(
            {
                "kind": KIND_NODE_DEPLOY,
                "attempt_id": node[0],
                "reason": "out_of_band_failure",
                "error": node[1],
                "at": node[2],
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["at"] or "")
