"""Evaluate the production plan intake against the model's journal and lineage."""

import asyncio
from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import patch

from haniel_orch.deploy_attempt_plan_rules import DeployAttemptPlanRules
from haniel_orch.deploy_attempt_validation import DeployAttemptValidation


class PlanContext(DeployAttemptPlanRules):
    def __init__(self, unconfirmed):
        self.unconfirmed = unconfirmed
        self._db = None

    async def _retry_unlocked(self, deploy_id):
        return {"lineage": [str(item) for item in self.unconfirmed], "created_at": "now"}

    async def _attempt(self, linked):
        return {"closure": "execution_unknown"}

    async def _terminalize_probe_unlocked(self, *args):
        pass


@lru_cache(maxsize=None)
def failed_journal_applies(linked, unconfirmed):
    async def evaluate():
        async def step_request(db, deploy_id, event, **kwargs):
            assert event == "plan_failed_journal"
            return SimpleNamespace(todo=("auto_reject",))

        probe = {"requested_orchestrator_attempt_id": "auto", "deploy_id": "request"}
        proposal = SimpleNamespace(linked_orchestrator_attempt_id=str(linked),
                                   journal_status="failed", journal_attempt_id="journal")
        with patch("haniel_orch.deploy_attempt_plan_rules.steps.step_request", step_request):
            return await PlanContext(unconfirmed)._plan_failed_journal_unlocked(probe, proposal, "auto")

    return asyncio.run(evaluate()) is not None


class ValidationContext(DeployAttemptValidation):
    def __init__(self, locked, protocol):
        self.locked = locked
        self.protocol = protocol

    async def _event(self, deploy_id):
        return {"status": "pending"}

    async def _is_latest(self, event):
        return True

    async def _lock_on(self, deploy_id):
        return self.locked

    async def _retry_unlocked(self, deploy_id):
        return None

    async def _request_protocol(self, deploy_id):
        return self.protocol


@lru_cache(maxsize=None)
def plan_requires_inspection(auto, locked, protocol):
    async def evaluate():
        fields = {"deploy_id": "request", "node_id": "node", "repo": "repo",
                  "branch": "branch", "target_head": "head", "connection_generation": "generation"}
        probe = {**fields, "deadline_at": "2999-01-01T00:00:00+00:00",
                 "requested_orchestrator_attempt_id": "auto" if auto else None,
                 "source_orchestrator_attempt_id": None, "retry_lineage_json": "[]"}
        proposal = SimpleNamespace(**fields)
        source = "auto" if auto else "manual_single"
        return await ValidationContext(locked, protocol)._proposal_stale_reason(probe, proposal, source)

    result = asyncio.run(evaluate())
    assert result is None or result[0] == "retry_requires_manual_approval", result
    return result is not None
