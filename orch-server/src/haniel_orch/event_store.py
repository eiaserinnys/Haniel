"""SQLite-backed persistence for deploy events and node registry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

from . import event_store_mutations, event_store_nodes, node_deploy_store
from .deploy_attempt_schema import initialize_attempt_schema
from .deploy_attempt_store import DeployAttemptStore
from .deploy_last_failure import last_failure
from .event_store_lifecycle import EventStoreLifecycleMixin
from .event_store_rows import decode_deploy_row, now_iso
from .protocol import DeployStatus, NodeDeployReport
from .statechart import steps
from .statechart.columns import LOCK_ON
from .statechart.restore import REASON_NO_TRANSITION, NoTransition

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS deploy_events (
    deploy_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    commits_json TEXT NOT NULL,
    affected_services_json TEXT NOT NULL,
    diff_stat TEXT,
    detected_at TEXT NOT NULL,
    approved_by TEXT,
    reject_reason TEXT,
    error TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_self_update INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    os TEXT NOT NULL,
    arch TEXT NOT NULL,
    haniel_version TEXT NOT NULL,
    connected INTEGER NOT NULL DEFAULT 1,
    connection_generation TEXT,
    connection_token TEXT,
    last_seen TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_tokens (
    token_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(node_id)
);
"""


class EventStore(EventStoreLifecycleMixin):
    """Async SQLite store for deploy events and nodes."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._mutation_lock = asyncio.Lock()
        self.attempts: DeployAttemptStore | None = None

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        async with self._mutation_lock:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.executescript(_CREATE_TABLES)
            await event_store_nodes.initialize_node_schema(self._db)
            await initialize_attempt_schema(self._db)
            self.attempts = DeployAttemptStore(self._db, self._mutation_lock)
            await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def create_deploy_event(
        self,
        deploy_id: str,
        node_id: str,
        repo: str,
        branch: str,
        commits: list[str],
        affected_services: list[str],
        diff_stat: str | None,
        detected_at: str,
        target_head: str | None = None,
        deployment_kind: str = "legacy",
        expected_manifest_identity: str | None = None,
        expected_manifest_digest: str | None = None,
        is_self_update: bool = False,
    ) -> bool:
        """Create a deploy event.

        Returns True when a new row was inserted. Duplicate deploy_id is
        silently ignored and returns False.
        """
        async with self._mutation_lock:
            now = now_iso()
            try:
                inserted = await steps.create_request(
                    self._db,
                    deploy_id=deploy_id,
                    node_id=node_id,
                    repo=repo,
                    branch=branch,
                    commits_json=json.dumps(commits),
                    affected_services_json=json.dumps(affected_services),
                    diff_stat=diff_stat,
                    detected_at=detected_at,
                    target_head=target_head or deploy_id.rsplit(":", 1)[-1],
                    deployment_kind=deployment_kind,
                    expected_manifest_identity=expected_manifest_identity,
                    expected_manifest_digest=expected_manifest_digest,
                    is_self_update=is_self_update,
                    now=now,
                )
                if inserted:
                    latest_cursor = await self._db.execute(
                        "SELECT deploy_id FROM deploy_events WHERE node_id = ? "
                        "AND repo = ? AND branch = ? AND deploy_id NOT LIKE 'attempt:%' "
                        "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
                        (node_id, repo, branch),
                    )
                    latest_row = await latest_cursor.fetchone()
                    if latest_row is None:
                        raise RuntimeError("inserted canonical disappeared")
                    latest_id = latest_row[0]
                    pending_cursor = await self._db.execute(
                        "SELECT deploy_id FROM deploy_events WHERE node_id = ? AND repo = ? "
                        "AND branch = ? AND status = ? AND deploy_id != ?",
                        (
                            node_id,
                            repo,
                            branch,
                            DeployStatus.PENDING.value,
                            latest_id,
                        ),
                    )
                    for (old_id,) in await pending_cursor.fetchall():
                        await steps.step_request(
                            self._db,
                            old_id,
                            "newer_commit",
                            data={"reason": f"superseded by {latest_id}"},
                            now=now,
                        )
                        await self._db.execute(
                            """UPDATE deploy_plan_probes
                               SET status = 'terminal', terminal_kind = 'preflight_stale',
                                   terminal_stage = 'canonical_create',
                                   terminal_reason = 'newer_canonical',
                                   terminal_error = ?, completed_at = ?
                               WHERE deploy_id = ? AND status IN ('active','proposed')""",
                            (
                                f"canonical superseded by {latest_id}",
                                now,
                                old_id,
                            ),
                        )
                await self._db.commit()
                return inserted
            except Exception:
                await self._db.rollback()
                raise

    async def record_node_deploy_report(
        self, report: NodeDeployReport
    ) -> dict[str, Any]:
        """Reconcile one node-owned deployment report transactionally."""
        async with self._mutation_lock:
            try:
                result = await node_deploy_store.record(self._db, report)
                await self._db.commit()
                return result
            except Exception:
                await self._db.rollback()
                raise

    async def resolve_observed_pending(
        self,
        *,
        deploy_id: str,
        node_id: str,
        repo: str,
        branch: str,
        local_head: str,
        remote_head: str,
    ) -> str | bool:
        """Resolve one stale row from observed Git truth.

        Returns the deploy_id of the row that was closed, or False when no
        row matched. The exact non-retry path is unchanged. A timed-out
        self-update keeps a deploy_id built from an older remote head, so it
        closes on a newer observation whose local_head is its target_head.
        """
        async with self._mutation_lock:
            try:
                now = now_iso()
                closed_id: str | None = None
                cursor = await self._db.execute(
                    """SELECT deploy_id, target_head FROM deploy_events
                       WHERE deploy_id = ? AND node_id = ? AND repo = ? AND branch = ?
                         AND status = ?""",
                    (deploy_id, node_id, repo, branch, DeployStatus.PENDING.value),
                )
                row = await cursor.fetchone()
                if row is not None:
                    try:
                        await steps.step_request(
                            self._db,
                            deploy_id,
                            "observed",
                            facts={
                                "heads_equal": row[1] == local_head == remote_head,
                                "no_active_attempt": not await self._has_active_attempt(
                                    deploy_id
                                ),
                                "no_failed_node_attempt": not await self._has_failed_node_attempt(
                                    deploy_id
                                ),
                            },
                            now=now,
                        )
                        closed_id = deploy_id
                    except NoTransition as exc:
                        exc.warn("observed")
                        closed_id = None
                if closed_id is None:
                    closed_id = await self._close_observed_self_update(
                        node_id=node_id,
                        repo=repo,
                        branch=branch,
                        local_head=local_head,
                        now=now,
                    )
                await self._db.commit()
                return closed_id or False
            except Exception:
                await self._db.rollback()
                raise

    async def _has_active_attempt(self, deploy_id: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM deploy_attempts WHERE deploy_id = ? AND outcome = 'active'",
            (deploy_id,),
        )
        return await cursor.fetchone() is not None

    async def _has_failed_node_attempt(self, deploy_id: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM node_deploy_attempts "
            "WHERE deploy_id = ? AND outcome = 'failed'",
            (deploy_id,),
        )
        return await cursor.fetchone() is not None

    async def _close_observed_self_update(
        self,
        *,
        node_id: str,
        repo: str,
        branch: str,
        local_head: str,
        now: str,
    ) -> str | None:
        """Close a timed-out self-update that a newer observation proves done.

        Retry markers and failed node reports do not block this path: the
        dashboard-timeout row is the same deployment the node finished. An
        active orchestrator attempt still does.
        """
        cursor = await self._db.execute(
            """SELECT deploy_id, is_self_update, target_head FROM deploy_events
               WHERE node_id = ? AND repo = ? AND branch = ? AND status = ?
               ORDER BY detected_at DESC, created_at DESC, deploy_id DESC""",
            (node_id, repo, branch, DeployStatus.PENDING.value),
        )
        # 가장 새 줄부터 보고, 조건을 처음 맞춘 줄을 닫는다(조건은 정의가 판단한다)
        for candidate_id, is_self_update, target_head in await cursor.fetchall():
            try:
                await steps.step_request(
                    self._db,
                    candidate_id,
                    "observed_self_update",
                    facts={
                        "self_update": bool(is_self_update),
                        "heads_equal": target_head == local_head,
                        "no_active_attempt": not await self._has_active_attempt(
                            candidate_id
                        ),
                    },
                    now=now,
                )
            except NoTransition as exc:
                if exc.reason != REASON_NO_TRANSITION:
                    exc.warn("observed_self_update")
                continue
            return candidate_id
        return None

    async def reopen_postponed_self_update(self, deploy_id: str) -> bool:
        """Return one due postponed self-update to pending.

        The row stays rejected while another pending or deploying deploy
        exists for the same (node, repo, branch).
        """
        async with self._mutation_lock:
            try:
                now = now_iso()
                cursor = await self._db.execute(
                    "SELECT node_id, repo, branch, is_self_update, postponed_until "
                    "FROM deploy_events WHERE deploy_id = ?",
                    (deploy_id,),
                )
                row = await cursor.fetchone()
                reopened = False
                if row is not None:
                    node_id, repo, branch, is_self_update, postponed_until = row
                    other_cursor = await self._db.execute(
                        "SELECT 1 FROM deploy_events WHERE node_id = ? AND repo = ? "
                        "AND branch = ? AND deploy_id != ? AND status IN (?, ?)",
                        (
                            node_id,
                            repo,
                            branch,
                            deploy_id,
                            DeployStatus.PENDING.value,
                            DeployStatus.DEPLOYING.value,
                        ),
                    )
                    try:
                        await steps.step_request(
                            self._db,
                            deploy_id,
                            "reopen",
                            facts={
                                "self_update": bool(is_self_update),
                                "postponed_due": postponed_until is not None
                                and postponed_until <= now,
                                "no_other_open": await other_cursor.fetchone() is None,
                            },
                            now=now,
                        )
                        reopened = True
                    except NoTransition as exc:
                        exc.warn("reopen")
                        reopened = False
                await self._db.commit()
                return reopened
            except Exception:
                await self._db.rollback()
                raise

    async def get_deploy_event(self, deploy_id: str) -> dict[str, Any] | None:
        """Get a single deploy event by ID. Returns None if not found."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE deploy_id = ?", (deploy_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return decode_deploy_row(cursor, row)

    async def get_pending_deploys(self) -> list[dict[str, Any]]:
        """Get all events with status='pending'.

        Used by approve_all (PENDING-only semantics — DEPLOYING shouldn't be re-approved).
        For dashboard's PendingView (which shows pending+deploying), use
        ``get_active_deploys`` instead.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
            (DeployStatus.PENDING.value,),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

    async def get_active_deploys(self) -> list[dict[str, Any]]:
        """Get pending + deploying events (newest first).

        Used by /api/orch/pending so that PendingView keeps showing deploys
        that have advanced to DEPLOYING after approval. APPROVED is transient
        and not included — the hub flips APPROVED → DEPLOYING immediately
        on send_to_node.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status IN (?, ?) "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC",
            (DeployStatus.PENDING.value, DeployStatus.DEPLOYING.value),
        )
        names = [column[0] for column in cursor.description]
        deploys = []
        for row in await cursor.fetchall():
            item = decode_deploy_row(cursor, row)
            # 점검 필요(잎 칸은 decode가 거른다)와 직전 실패 요약. 기존 칸과 이름은 그대로다
            item["inspection_required"] = row[names.index("sm_lock")] == LOCK_ON
            item["last_failure"] = await last_failure(self._db, item["deploy_id"])
            deploys.append(item)
        return deploys

    async def get_active_self_update_for_node(
        self, node_id: str
    ) -> dict[str, Any] | None:
        """Return one active self-update that blocks other deploys on a node."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events "
            "WHERE node_id = ? AND is_self_update = 1 AND status IN (?, ?) "
            "ORDER BY detected_at DESC, created_at DESC, deploy_id DESC LIMIT 1",
            (
                node_id,
                DeployStatus.PENDING.value,
                DeployStatus.DEPLOYING.value,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else decode_deploy_row(cursor, row)

    async def supersede_stale_pending_deploys(self) -> list[str]:
        """Reject older PENDING deploys per (node, repo, branch).

        This startup repair handles rows that existed before generation-time
        supersede was added, or rows inserted while an old orch-server was
        running. Read APIs never invoke it. DEPLOYING rows are excluded.
        """
        async with self._mutation_lock:
            try:
                superseded = (
                    await event_store_mutations.supersede_stale_pending_deploys(
                        self._db
                    )
                )
                await self._db.commit()
                return superseded
            except Exception:
                await self._db.rollback()
                raise

    async def get_pending_deploys_for_branch(
        self, node_id: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        """Get PENDING deploys for the same (node, repo, branch).

        Used by hub.supersede_pending to mark older PENDING deploys as
        REJECTED (with reject_reason='superseded by ${kept}') when a newer
        deploy is approved.
        """
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events "
            "WHERE node_id = ? AND repo = ? AND branch = ? AND status = ? "
            "ORDER BY created_at DESC",
            (node_id, repo, branch, DeployStatus.PENDING.value),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]

    async def supersede_pending_for_branch(
        self, node_id: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        async with self._mutation_lock:
            try:
                rejected = await event_store_mutations.supersede_pending_for_branch(
                    self._db, node_id, repo, branch
                )
                await self._db.commit()
                return rejected
            except Exception:
                await self._db.rollback()
                raise

    async def get_deploys_superseded_by(self, deploy_id: str) -> list[dict[str, Any]]:
        """Read canonical rows atomically superseded when ``deploy_id`` was created."""
        cursor = await self._db.execute(
            "SELECT * FROM deploy_events WHERE status = ? AND reject_reason = ?",
            (DeployStatus.REJECTED.value, f"superseded by {deploy_id}"),
        )
        rows = await cursor.fetchall()
        return [decode_deploy_row(cursor, row) for row in rows]
