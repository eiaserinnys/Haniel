"""Immutable identities for the coordinator-owned node connection lifecycle."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionLease:
    """Identity captured by one accepted node WebSocket handler."""

    generation: str
    connection_token: str


@dataclass(frozen=True)
class ActiveConnection(ConnectionLease):
    """Connection that may create probes, begin attempts, and receive permits."""


@dataclass(frozen=True)
class DisconnectingConnection(ConnectionLease):
    """Revoked connection completing conditional registry and DB cleanup."""

    disconnect_token: str


ConnectionState = ActiveConnection | DisconnectingConnection
