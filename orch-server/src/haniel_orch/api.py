"""REST API routes for the Orchestrator dashboard."""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .event_store import EventStore
from .hub import WebSocketHub
from .protocol import DeployApproval, DeployReject, DeployStatus

logger = logging.getLogger(__name__)


def create_api_routes(hub: WebSocketHub, store: EventStore) -> list[Route]:
    """Create REST API routes bound to the given hub and store."""

    async def get_pending(request: Request) -> JSONResponse:
        """GET /api/orch/pending — list all pending deploy events."""
        pending = await store.get_pending_deploys()
        return JSONResponse({"deploys": pending})

    async def get_nodes(request: Request) -> JSONResponse:
        """GET /api/orch/nodes — list all registered nodes."""
        nodes = await store.get_nodes()
        for node in nodes:
            connected_node = hub.registry.get_node(node["node_id"])
            if connected_node and connected_node.services:
                node["services"] = connected_node.services
        return JSONResponse({"nodes": nodes})

    async def get_history(request: Request) -> JSONResponse:
        """GET /api/orch/history — list deploy events, newest first."""
        limit = int(request.query_params.get("limit", "50"))
        history = await store.get_deploy_history(limit=limit)
        return JSONResponse({"deploys": history})

    async def approve_deploy(request: Request) -> JSONResponse:
        """POST /api/orch/approve — approve a pending deploy.

        Flow: get event → validate status is PENDING → set APPROVED →
              send DeployApproval to node → set DEPLOYING.
        """
        body = await request.json()
        deploy_id = body.get("deploy_id")
        if not deploy_id:
            return JSONResponse(
                {"error": "deploy_id is required"}, status_code=400
            )

        event = await store.get_deploy_event(deploy_id)
        if event is None:
            return JSONResponse(
                {"error": f"deploy {deploy_id} not found"}, status_code=404
            )

        if event["status"] != DeployStatus.PENDING.value:
            return JSONResponse(
                {"error": f"deploy is '{event['status']}', not pending"},
                status_code=409,
            )

        # Mark as approved
        approved_by = body.get("approved_by", "dashboard")
        await store.update_deploy_status(
            deploy_id, DeployStatus.APPROVED, approved_by=approved_by
        )

        # Send approval to node
        msg = DeployApproval(deploy_id=deploy_id, approved_by=approved_by)
        sent = await hub.send_to_node(event["node_id"], msg)

        if sent:
            # Node received — mark as deploying
            await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)
            await hub.broadcast_to_dashboards({
                "type": "status_change",
                "deploy_id": deploy_id,
                "status": DeployStatus.DEPLOYING.value,
                "node_id": event["node_id"],
            })
            return JSONResponse({"deploy_id": deploy_id, "status": "deploying"})
        else:
            # Node not connected — leave as approved (will deploy when reconnects)
            return JSONResponse({
                "deploy_id": deploy_id,
                "status": "approved",
                "warning": "node not connected, will deploy on reconnect",
            })

    async def reject_deploy(request: Request) -> JSONResponse:
        """POST /api/orch/reject — reject a pending deploy."""
        body = await request.json()
        deploy_id = body.get("deploy_id")
        if not deploy_id:
            return JSONResponse(
                {"error": "deploy_id is required"}, status_code=400
            )

        event = await store.get_deploy_event(deploy_id)
        if event is None:
            return JSONResponse(
                {"error": f"deploy {deploy_id} not found"}, status_code=404
            )

        if event["status"] != DeployStatus.PENDING.value:
            return JSONResponse(
                {"error": f"deploy is '{event['status']}', not pending"},
                status_code=409,
            )

        reason = body.get("reason", "rejected by dashboard")
        await store.update_deploy_status(
            deploy_id, DeployStatus.REJECTED, reject_reason=reason
        )

        # Send rejection to node
        msg = DeployReject(deploy_id=deploy_id, reason=reason)
        await hub.send_to_node(event["node_id"], msg)

        await hub.broadcast_to_dashboards({
            "type": "status_change",
            "deploy_id": deploy_id,
            "status": DeployStatus.REJECTED.value,
            "node_id": event["node_id"],
        })

        return JSONResponse({"deploy_id": deploy_id, "status": "rejected"})

    async def approve_all(request: Request) -> JSONResponse:
        """POST /api/orch/approve-all — approve all pending deploys.

        If no pending deploys: returns {approved: [], failed: [], message: "no pending deploys"}.
        """
        pending = await store.get_pending_deploys()

        if not pending:
            return JSONResponse({
                "approved": [],
                "failed": [],
                "message": "no pending deploys",
            })

        approved: list[str] = []
        failed: list[dict[str, Any]] = []

        for event in pending:
            deploy_id = event["deploy_id"]
            node_id = event["node_id"]

            await store.update_deploy_status(
                deploy_id, DeployStatus.APPROVED, approved_by="dashboard"
            )

            msg = DeployApproval(deploy_id=deploy_id, approved_by="dashboard")
            sent = await hub.send_to_node(node_id, msg)

            if sent:
                await store.update_deploy_status(
                    deploy_id, DeployStatus.DEPLOYING
                )
                await hub.broadcast_to_dashboards({
                    "type": "status_change",
                    "deploy_id": deploy_id,
                    "status": DeployStatus.DEPLOYING.value,
                    "node_id": node_id,
                })
                approved.append(deploy_id)
            else:
                failed.append({
                    "deploy_id": deploy_id,
                    "reason": "node not connected",
                })

        return JSONResponse({"approved": approved, "failed": failed})

    return [
        Route("/api/orch/pending", get_pending, methods=["GET"]),
        Route("/api/orch/nodes", get_nodes, methods=["GET"]),
        Route("/api/orch/history", get_history, methods=["GET"]),
        Route("/api/orch/approve", approve_deploy, methods=["POST"]),
        Route("/api/orch/reject", reject_deploy, methods=["POST"]),
        Route("/api/orch/approve-all", approve_all, methods=["POST"]),
    ]
