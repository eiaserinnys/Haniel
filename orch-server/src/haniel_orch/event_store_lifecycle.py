"""Deploy history/status and node registry methods for EventStore."""

from __future__ import annotations

from typing import Any

from . import event_store_mutations, event_store_nodes, node_deploy_store
from .event_store_rows import decode_deploy_row, now_iso, row_to_dict
from .protocol import DeployStatus


class EventStoreLifecycleMixin:
    """Keep EventStore's public lifecycle API behind one smaller module."""

    async def get_deploy_history(
        self,
        limit: int = 50,
        *,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Get deploy events newest first."""
        if include_superseded:
            sql = "SELECT * FROM deploy_events"
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT * FROM deploy_events "
                "WHERE NOT (status = ? AND reject_reason LIKE 'superseded by %') "
            )
            params = (DeployStatus.REJECTED.value,)
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            item = decode_deploy_row(cursor, row)
            item.update(
                terminal_kind=None,
                terminal_stage=None,
                terminal_reason=None,
                terminal_error=None,
            )
            results.append(item)
        if self.attempts is not None:
            success_metadata = await self.attempts.success_metadata()
            for item in results:
                metadata = success_metadata.get(item["deploy_id"])
                if (
                    metadata is not None
                    and item["status"] == DeployStatus.SUCCESS.value
                ):
                    item.update(metadata)
            results.extend(await self.attempts.history_rows(include_superseded))
        node_success_metadata = await node_deploy_store.success_metadata(self._db)
        for item in results:
            metadata = node_success_metadata.get(item.get("deploy_id"))
            if metadata is not None and item.get("status") == DeployStatus.SUCCESS.value:
                item.update(metadata)
        results.extend(await node_deploy_store.history_rows(self._db))
        results.sort(
            key=lambda item: (
                item.get("updated_at") or item.get("created_at") or "",
                item["deploy_id"],
            ),
            reverse=True,
        )
        return results[:limit]

    async def update_deploy_status(
        self,
        deploy_id: str,
        status: DeployStatus,
        approved_by: str | None = None,
        reject_reason: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Update event status and optional fields. Only non-None args are SET."""
        async with self._mutation_lock:
            try:
                await self._update_deploy_status_unlocked(
                    deploy_id, status, approved_by, reject_reason, error, duration_ms
                )
                if status in (DeployStatus.REJECTED, DeployStatus.SUCCESS):
                    await self._db.execute(
                        "DELETE FROM deploy_retry_requirements WHERE deploy_id = ?",
                        (deploy_id,),
                    )
                    await self._db.execute(
                        "DELETE FROM deploy_retry_source_attempts WHERE deploy_id = ?",
                        (deploy_id,),
                    )
                    await self._db.execute(
                        """UPDATE deploy_plan_probes
                           SET status = 'terminal', terminal_kind = 'preflight_stale',
                               terminal_stage = 'canonical_status_update',
                               terminal_reason = 'canonical_terminal',
                               terminal_error = ?, completed_at = ?
                           WHERE deploy_id = ? AND status IN ('active','proposed')""",
                        (
                            f"canonical became {status.value} during preflight",
                            now_iso(),
                            deploy_id,
                        ),
                    )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def _update_deploy_status_unlocked(
        self,
        deploy_id: str,
        status: DeployStatus,
        approved_by: str | None = None,
        reject_reason: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, now_iso()]

        if approved_by is not None:
            updates.append("approved_by = ?")
            params.append(approved_by)
        if reject_reason is not None:
            updates.append("reject_reason = ?")
            params.append(reject_reason)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if duration_ms is not None:
            updates.append("duration_ms = ?")
            params.append(duration_ms)

        params.append(deploy_id)
        await self._db.execute(
            f"UPDATE deploy_events SET {', '.join(updates)} WHERE deploy_id = ?",
            params,
        )

    async def get_deploying_events_for_node(self, node_id: str) -> list[dict[str, Any]]:
        """Get events in DEPLOYING state for diagnostics."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE node_id = ? AND status = ?",
            (node_id, DeployStatus.DEPLOYING.value),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

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
    ) -> None:
        """Register or update a node and its durable connection generation."""
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
            "last_seen, created_at FROM nodes ORDER BY last_seen DESC"
        )
        rows = await cursor.fetchall()
        return [row_to_dict(cursor, row) for row in rows]

    async def get_node_ids_by_hostname(self, hostname: str) -> list[str]:
        """Return node IDs that have reported the given hostname."""
        cursor = await self._db.execute(
            "SELECT node_id FROM nodes WHERE hostname = ?",
            (hostname,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]
