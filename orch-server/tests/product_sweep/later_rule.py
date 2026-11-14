"""Use the production query to determine whether a later attempt is relevant."""

import asyncio
from functools import lru_cache

import aiosqlite

from haniel_orch.deploy_attempt_late_evidence import DeployAttemptLateEvidence


@lru_cache(maxsize=None)
def no_later_attempt(protocol, attempt, opened, started):
    async def evaluate():
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(
                "CREATE TABLE deploy_attempts (deploy_id TEXT, attempt_seq INTEGER, execution_started_at TEXT)"
            )
            await db.executemany(
                "INSERT INTO deploy_attempts VALUES (?, ?, ?)",
                [("request", seq, "started" if seq in started else None) for seq in range(1, opened + 1)],
            )
            context = DeployAttemptLateEvidence()
            context._db = db
            return await context._no_later_attempt(
                {"deploy_id": "request", "attempt_seq": attempt, "node_protocol": protocol}
            )

    return asyncio.run(evaluate())
