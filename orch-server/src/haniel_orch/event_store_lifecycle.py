"""Deploy history/status and node registry methods for EventStore."""

from __future__ import annotations

from typing import Any

from . import event_store_mutations, event_store_nodes, node_deploy_store
from .event_store_rows import (
    batches,
    close_probes,
    decode_deploy_row,
    history_order,
    now_iso,
    row_to_dict,
)
from .protocol import DeployStatus
from .statechart import steps
from .statechart.restore import NoTransition


class EventStoreLifecycleMixin:
    """Keep EventStore's public lifecycle API behind one smaller module."""

    async def get_deploy_history(
        self,
        limit: int = 50,
        *,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Get deploy events newest first.

        Each source is sorted and cut to ``limit`` rows in SQL, then the merged
        rows are sorted and cut again here.
        """
        if include_superseded:
            where = ""
            params: tuple[Any, ...] = (limit,)
        else:
            where = "WHERE NOT (status = ? AND reject_reason LIKE 'superseded by %') "
            params = (DeployStatus.REJECTED.value, limit)
        # 큰 칸(commits_json, diff_stat)을 정렬 칸에 싣지 않으려고 번호만 정렬해 자른 뒤 그 줄만 읽는다
        key_cursor = await self._db.execute(
            "SELECT deploy_id FROM deploy_events "
            + where
            + history_order("updated_at", "created_at", "deploy_id"),
            params,
        )
        deploy_ids = [row[0] for row in await key_cursor.fetchall()]
        results = []
        for batch in batches(deploy_ids):
            placeholders = ", ".join("?" for _ in batch)
            cursor = await self._db.execute(
                f"SELECT * FROM deploy_events WHERE deploy_id IN ({placeholders})",
                tuple(batch),
            )
            for row in await cursor.fetchall():
                item = decode_deploy_row(cursor, row)
                item.update(
                    terminal_kind=None,
                    terminal_stage=None,
                    terminal_reason=None,
                    terminal_error=None,
                )
                results.append(item)
        success_ids = [
            item["deploy_id"]
            for item in results
            if item["status"] == DeployStatus.SUCCESS.value
        ]
        if self.attempts is not None:
            success_metadata = await self.attempts.success_metadata(success_ids)
            for item in results:
                metadata = success_metadata.get(item["deploy_id"])
                if (
                    metadata is not None
                    and item["status"] == DeployStatus.SUCCESS.value
                ):
                    item.update(metadata)
        node_success_metadata = await node_deploy_store.success_metadata(
            self._db, success_ids
        )
        for item in results:
            metadata = node_success_metadata.get(item["deploy_id"])
            if (
                metadata is not None
                and item["status"] == DeployStatus.SUCCESS.value
            ):
                item.update(metadata)
        if self.attempts is not None:
            results.extend(await self.attempts.history_rows(limit))
        results.extend(await node_deploy_store.history_rows(self._db, limit))
        results.sort(
            key=lambda item: (
                item.get("updated_at") or item.get("created_at") or "",
                item["deploy_id"],
            ),
            reverse=True,
        )
        return results[:limit]

    async def reject_deploy(self, deploy_id: str, reason: str | None = None) -> bool:
        """Reject one request through the reject step.

        Only a pending or retry request can be rejected (어긋남 8 is gone).
        Returns False when the definition has no reject transition from the
        current state (deploying or closed), the row is unknown, or it has no leaf.
        """
        async with self._mutation_lock:
            try:
                try:
                    await steps.step_request(
                        self._db, deploy_id, "reject", data={"reason": reason}
                    )
                except NoTransition as exc:
                    exc.warn("reject")
                    await self._db.commit()
                    return False
                await close_probes(
                    self._db,
                    [deploy_id],
                    stage="canonical_status_update",
                    reason="canonical_terminal",
                    error=f"canonical became {DeployStatus.REJECTED.value} during preflight",
                )
                await self._db.commit()
                return True
            except Exception:
                await self._db.rollback()
                raise

    async def set_deploy_postponed_until(
        self, deploy_id: str, postponed_until: str
    ) -> None:
        """Record when a postponed self-update may return to pending."""
        async with self._mutation_lock:
            try:
                await steps.set_postponed_until(
                    self._db, deploy_id, postponed_until, now_iso()
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def set_apply_policy(self, deploy_id: str, apply_policy: str) -> None:
        """이미 있는 대기 줄에 노드가 다시 알린 적용 방식을 반영한다(옛 노드가 만든 줄은 NULL로 시작한다)."""
        async with self._mutation_lock:
            try:
                if await steps.set_apply_policy(self._db, deploy_id, apply_policy):
                    await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def reject_pending_deploys_for_nodes(
        self, node_ids: list[str], reject_reason: str
    ) -> list[dict[str, Any]]:
        """Reject all PENDING deploys for the given nodes and return them."""
        if not node_ids:
            return []
        async with self._mutation_lock:
            try:
                rejected = await event_store_mutations.reject_pending_deploys_for_nodes(
                    self._db, node_ids, reject_reason
                )
                await self._db.commit()
                return rejected
            except Exception:
                await self._db.rollback()
                raise

    async def close_absent_node_requests(
        self, *, absent_before: str, connected_node_ids: set[str]
    ) -> list[dict[str, Any]]:
        """기한 넘게 연결되지 않은 노드의 대기 요청을 닫는다(어긋남 7). 닫은 요청을 돌려준다."""
        async with self._mutation_lock:
            try:
                closed = await event_store_mutations.close_absent_node_requests(
                    self._db,
                    absent_before=absent_before,
                    connected_node_ids=connected_node_ids,
                    now=now_iso(),
                )
                await self._db.commit()
                return closed
            except Exception:
                await self._db.rollback()
                raise

    async def upsert_node(
        self,
        node_id: str,
        hostname: str,
        os: str,
        arch: str,
        haniel_version: str,
        connected: bool = True,
        connection_generation: str | None = None,
        connection_token: str | None = None,
        deploy_protocol: int | None = None,
    ) -> None:
        """Register or update a node, its durable connection generation, and the deploy protocol it declared."""
        async with self._mutation_lock:
            try:
                await event_store_nodes.upsert_node(
                    self._db,
                    node_id,
                    hostname,
                    os,
                    arch,
                    haniel_version,
                    connected,
                    connection_generation,
                    connection_token,
                    deploy_protocol,
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def update_node_heartbeat(
        self,
        node_id: str,
        expected_generation: str | None = None,
        expected_connection_token: str | None = None,
    ) -> bool:
        """Update last_seen timestamp for a node."""
        async with self._mutation_lock:
            try:
                updated = await event_store_nodes.update_node_heartbeat(
                    self._db,
                    node_id,
                    expected_generation,
                    expected_connection_token,
                )
                await self._db.commit()
                return updated
            except Exception:
                await self._db.rollback()
                raise

    async def mark_node_disconnected(
        self,
        node_id: str,
        expected_generation: str,
        expected_connection_token: str,
    ) -> bool:
        """Mark only the expected live generation disconnected."""
        async with self._mutation_lock:
            try:
                updated = await event_store_nodes.mark_node_disconnected(
                    self._db,
                    node_id,
                    expected_generation,
                    expected_connection_token,
                )
                await self._db.commit()
                return updated
            except Exception:
                await self._db.rollback()
                raise

    async def get_node_connection_generation(self, node_id: str) -> str | None:
        """Return the durable connection generation for race assertions."""
        cursor = await self._db.execute(
            "SELECT connection_generation FROM nodes WHERE node_id = ?", (node_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else row[0]

    async def get_node_connection_identity(
        self, node_id: str
    ) -> tuple[str | None, str | None] | None:
        """Return the durable generation and token used by disconnect CAS."""
        cursor = await self._db.execute(
            "SELECT connection_generation, connection_token FROM nodes WHERE node_id = ?",
            (node_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else (row[0], row[1])

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes (connected and disconnected)."""
        cursor = await self._db.execute(
            "SELECT node_id, hostname, os, arch, haniel_version, connected, "
            "last_seen, created_at, deploy_protocol FROM nodes ORDER BY last_seen DESC"
        )
        rows = await cursor.fetchall()
        return [row_to_dict(cursor, row) for row in rows]

    async def get_served_commits(self) -> dict[str, list[dict[str, Any]]]:
        """노드별, 저장소별로 마지막 성공 배포의 target head와 그 시각. 이 허브가 아는 배포 기준이다.

        SQLite는 MAX가 든 집계에서 같은 줄의 나머지 칸을 돌려준다.
        """
        cursor = await self._db.execute(
            "SELECT node_id, repo, branch, target_head, deploy_id, "
            "MAX(COALESCE(NULLIF(updated_at, ''), created_at)) AS deployed_at "
            "FROM deploy_events WHERE status = ? GROUP BY node_id, repo "
            "ORDER BY node_id, repo",
            (DeployStatus.SUCCESS.value,),
        )
        served: dict[str, list[dict[str, Any]]] = {}
        for (
            node_id,
            repo,
            branch,
            target_head,
            deploy_id,
            deployed_at,
        ) in await cursor.fetchall():
            served.setdefault(node_id, []).append(
                {
                    "repo": repo,
                    "branch": branch,
                    "target_head": target_head or deploy_id.rsplit(":", 1)[-1],
                    "deployed_at": deployed_at,
                    "deploy_id": deploy_id,
                }
            )
        return served

    async def get_deploying_self_updates(
        self, node_id: str | None = None
    ) -> dict[str, str]:
        """지금 배포 중(deploying)인 자기 갱신의 deploy_id를 노드별로 돌려준다. 노드마다 가장 최근 줄 하나."""
        query = (
            "SELECT node_id, deploy_id FROM deploy_events "
            "WHERE is_self_update = 1 AND status = ?"
        )
        params: list[Any] = [DeployStatus.DEPLOYING.value]
        if node_id is not None:
            query += " AND node_id = ?"
            params.append(node_id)
        cursor = await self._db.execute(
            query + " ORDER BY detected_at ASC, created_at ASC, deploy_id ASC", params
        )
        # 오래된 줄부터 읽어 덮으므로 노드마다 가장 최근 줄이 남는다
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def get_node_ids_by_hostname(self, hostname: str) -> list[str]:
        """Return node IDs that have reported the given hostname."""
        cursor = await self._db.execute(
            "SELECT node_id FROM nodes WHERE hostname = ?",
            (hostname,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]
