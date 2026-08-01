"""Connection-state transitions shared by deploy attempt coordination."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .connection_lifecycle import (
    ActiveConnection,
    ConnectionLease,
    ConnectionState,
    DisconnectingConnection,
)

ConnectionActivation = Callable[[ConnectionLease], Awaitable[None]]
ConnectionDeactivation = Callable[[], Awaitable[None]]


class DeployAttemptConnectionLifecycle:
    """Mixin that owns active/disconnecting/absent node connection states."""

    _coordination_lock: asyncio.Lock
    _connections: dict[str, ConnectionState]
    attempts: Any

    async def register_connection(
        self, node_id: str, activate: ConnectionActivation
    ) -> ConnectionLease:
        """Install the current socket generation and stale older probes atomically."""
        async with self._coordination_lock:
            lease = ConnectionLease(
                generation=uuid4().hex,
                connection_token=uuid4().hex,
            )
            await activate(lease)
            self._connections[node_id] = ActiveConnection(
                generation=lease.generation,
                connection_token=lease.connection_token,
            )
            stale = await self.attempts.terminalize_prior_generations(
                node_id, lease.generation
            )
            for probe_id in stale:
                self._resolve_probe(
                    probe_id,
                    self._plan_rejected("connection generation changed", 409),
                )
            return lease

    def current_generation(self, node_id: str) -> str | None:
        state = self._connections.get(node_id)
        return state.generation if isinstance(state, ActiveConnection) else None

    def current_connection(self, node_id: str) -> ConnectionLease | None:
        state = self._connections.get(node_id)
        if not isinstance(state, ActiveConnection):
            return None
        return ConnectionLease(state.generation, state.connection_token)

    async def begin_disconnect(
        self,
        node_id: str,
        expected_generation: str,
        expected_connection_token: str,
    ) -> DisconnectingConnection | None:
        """Revoke one active connection before any external cleanup await."""
        async with self._coordination_lock:
            state = self._connections.get(node_id)
            if isinstance(state, DisconnectingConnection):
                if (
                    state.generation == expected_generation
                    and state.connection_token == expected_connection_token
                ):
                    return state
                return None
            if not (
                isinstance(state, ActiveConnection)
                and state.generation == expected_generation
                and state.connection_token == expected_connection_token
            ):
                return None
            disconnecting = DisconnectingConnection(
                generation=state.generation,
                connection_token=state.connection_token,
                disconnect_token=uuid4().hex,
            )
            self._connections[node_id] = disconnecting
            return disconnecting

    async def finalize_disconnect(
        self,
        node_id: str,
        lease: DisconnectingConnection,
        on_current_disconnect: ConnectionDeactivation | None = None,
    ) -> bool:
        """Finish only the unchanged disconnecting state exactly once."""
        async with self._coordination_lock:
            state = self._connections.get(node_id)
            if state != lease:
                return False
            self._connections.pop(node_id, None)
            for probe_id in await self.attempts.terminalize_generation(
                lease.generation
            ):
                self._resolve_probe(
                    probe_id,
                    self._plan_rejected("node disconnected", 503),
                )
            if on_current_disconnect is not None:
                await on_current_disconnect()
            return True
