"""시험이 요청 줄을 원하는 상태에 세울 때 쓰는 사건 도우미.

지운 일반 쓰기 API(`update_deploy_status`, `transition_deploy_status`)를 대신해
같은 뜻의 사건을 한 걸음으로 넣는다. 정의에 없는 옛 값(approved, failed)은
옛 DB에서 온 줄처럼 잎 없이 직접 적는다.
"""

from __future__ import annotations

from typing import Any

from haniel_orch.event_store import EventStore
from haniel_orch.statechart import steps


async def fire(
    store: EventStore,
    deploy_id: str,
    event: str,
    *,
    facts: dict[str, bool] | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """저장소 변경 잠금 안에서 요청 사건 하나를 넣고 커밋한다."""
    async with store._mutation_lock:
        try:
            await steps.step_request(store._db, deploy_id, event, facts=facts, data=data)
            await store._db.commit()
        except Exception:
            await store._db.rollback()
            raise


async def to_deploying(
    store: EventStore,
    deploy_id: str,
    by: str = "dashboard",
    *,
    manual_single: bool = False,
) -> None:
    """승인해서 배포 중으로(시도 줄 없이 요청 줄만). 옛 `update_deploy_status(DEPLOYING)`와 같은 뜻.

    점검 필요가 켜진 줄을 열려면 manual_single=True(원천이 단건 승인)여야 한다.
    """
    await fire(
        store,
        deploy_id,
        "begin_attempt",
        facts={"manual_single": manual_single, "via_probe": False},
        data={"approved_by": by},
    )


async def to_pending_again(store: EventStore, deploy_id: str) -> None:
    """배포 중에서 도는 시도 없이 대기로. 노드 실패 보고처럼 점검 필요가 켜진 표시 줄 있는 대기가 된다."""
    await fire(store, deploy_id, "node_failed", facts={"no_active_attempt": True})


async def to_success(
    store: EventStore, deploy_id: str, duration_ms: int | None = None
) -> None:
    """노드가 스스로 한 배포의 성공으로 닫는다. 옛 `update_deploy_status(SUCCESS)`와 같은 뜻."""
    await fire(store, deploy_id, "node_succeeded", data={"duration_ms": duration_ms})


async def set_legacy_status(
    store: EventStore, deploy_id: str, status: str, **columns: Any
) -> None:
    """정의에 잎이 없는 옛 값(approved, failed)이 든 줄을 옛 DB에서 온 것처럼 만든다."""
    assignments = ", ".join(["status = ?", "sm_status = NULL", *[f"{k} = ?" for k in columns]])
    await store._db.execute(
        f"UPDATE deploy_events SET {assignments} WHERE deploy_id = ?",
        (status, *columns.values(), deploy_id),
    )
    await store._db.commit()
