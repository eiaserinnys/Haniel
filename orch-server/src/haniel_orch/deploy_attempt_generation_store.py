"""Generation-scoped probe terminalization transactions.

계획 묻기가 닫히면 그 요청의 믿음을 계획을 물음에서 도는 시도 없음으로 돌린다(link_down. 끊김과 연결 교체는
오케스트레이터가 끊김을 안 것이다, fable 146-152).
"""

from __future__ import annotations

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeployAttemptGenerationStore:
    """Store mixin keeping socket-generation cleanup behind one DB lock."""

    async def terminalize_generation(self, generation: str) -> list[str]:
        async with self._mutation_lock:
            try:
                cursor = await self._db.execute(
                    "SELECT probe_id FROM deploy_plan_probes WHERE connection_generation = ? "
                    "AND status IN ('active','proposed')",
                    (generation,),
                )
                changed: list[str] = []
                now = _now_iso()
                for (probe_id,) in await cursor.fetchall():
                    probe = await self._probe(probe_id)
                    if probe and await self._terminalize_probe_unlocked(
                        probe,
                        "preflight_disconnected",
                        "connection",
                        "node_disconnected",
                        "node disconnected before begin",
                    ):
                        changed.append(probe_id)
                        await self._settle_probe_belief_unlocked(probe["deploy_id"], "link_down", now)
                await self._db.commit()
                return changed
            except Exception:
                await self._db.rollback()
                raise

    async def terminalize_prior_generations(
        self, node_id: str, current_generation: str
    ) -> list[str]:
        """Atomically close every open probe owned by an older node socket."""
        async with self._mutation_lock:
            try:
                cursor = await self._db.execute(
                    "SELECT probe_id FROM deploy_plan_probes WHERE node_id = ? "
                    "AND connection_generation != ? AND status IN ('active','proposed')",
                    (node_id, current_generation),
                )
                changed: list[str] = []
                now = _now_iso()
                for (probe_id,) in await cursor.fetchall():
                    probe = await self._probe(probe_id)
                    if probe and await self._terminalize_probe_unlocked(
                        probe,
                        "preflight_stale",
                        "connection_registration",
                        "connection_generation_changed",
                        "a newer node connection generation replaced this probe",
                    ):
                        changed.append(probe_id)
                        await self._settle_probe_belief_unlocked(probe["deploy_id"], "link_down", now)
                await self._db.commit()
                return changed
            except Exception:
                await self._db.rollback()
                raise
