"""Shared fixtures for orch-server tests."""

import pytest

from haniel_orch.event_store import EventStore


@pytest.fixture
async def store():
    """In-memory SQLite EventStore for tests."""
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()
