"""Node and server deploy-plan reason inventories must remain symmetric."""

from __future__ import annotations

from pathlib import Path
import sys

from haniel.integrations.orchestrator_client import DEPLOY_PLAN_REASON_INVENTORY


def test_node_and_server_deploy_plan_reason_inventories_match() -> None:
    server_src = Path(__file__).resolve().parents[1] / "orch-server" / "src"
    sys.path.insert(0, str(server_src))
    try:
        from haniel_orch.protocol import SERVER_DEPLOY_PLAN_REASON_INVENTORY
    finally:
        sys.path.remove(str(server_src))

    assert DEPLOY_PLAN_REASON_INVENTORY == SERVER_DEPLOY_PLAN_REASON_INVENTORY
