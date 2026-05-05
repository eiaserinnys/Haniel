"""REST API routes for the Orchestrator dashboard."""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .event_store import EventStore
from .hub import WebSocketHub
from .protocol import DeployApproval, DeployReject, DeployStatus, ServiceCommand

logger = logging.getLogger(__name__)


def create_api_routes(hub: WebSocketHub, store: EventStore) -> list[Route]:
    """Create REST API routes bound to the given hub and store."""

    async def get_pending(request: Request) -> JSONResponse:
        """GET /api/orch/pending — list active deploys (pending + deploying).

        PendingView shows both states so that a deploy stays visible after
        approval (DEPLOYING) until the node reports a terminal result.
        """
        deploys = await store.get_active_deploys()
        return JSONResponse({"deploys": deploys})

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
            # Track for timeout/disconnect handling FIRST. A fast node may
            # send DeployResult before our await chain completes; if that
            # arrives before register, _handle_deploy_result.pop() returns
            # None and we'd leak a timeout task that fires false-positive
            # status='failed' minutes later.
            await hub.register_pending_deploy(
                deploy_id, event["node_id"], event["repo"], event["branch"]
            )
            # Node received — mark as deploying
            await store.update_deploy_status(deploy_id, DeployStatus.DEPLOYING)
            await hub.broadcast_to_dashboards({
                "type": "status_change",
                "deploy_id": deploy_id,
                "status": DeployStatus.DEPLOYING.value,
                "node_id": event["node_id"],
            })
            # Supersede any other PENDING deploys in the same (node, repo, branch).
            # `kept_deploy_id` is the one we just approved.
            await hub.supersede_pending(
                event["node_id"], event["repo"], event["branch"], deploy_id
            )
            return JSONResponse({"deploy_id": deploy_id, "status": "deploying"})
        else:
            # Node not connected — leave as approved (will deploy when reconnects).
            # No supersede / no register_pending_deploy: deploy is not in flight yet.
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

        Within each (node, repo, branch) group, only the latest (by created_at)
        is approved; the others are auto-superseded so that a stale older
        deploy does not run after a newer one. Response includes ``superseded``
        list when any auto-supersede occurred.

        If no pending deploys: returns
        {approved: [], failed: [], message: "no pending deploys"}.
        """
        pending = await store.get_pending_deploys()

        if not pending:
            return JSONResponse({
                "approved": [],
                "failed": [],
                "message": "no pending deploys",
            })

        # Group by (node, repo, branch). `pending` is ordered created_at DESC,
        # so the first occurrence per group is the latest commit. Older
        # entries in the same group are auto-superseded right away.
        seen_groups: set[tuple[str, str, str]] = set()
        to_approve: list[dict[str, Any]] = []
        auto_superseded: list[str] = []
        for ev in pending:
            key = (ev["node_id"], ev["repo"], ev["branch"])
            if key in seen_groups:
                # Older deploy in the same branch → supersede now.
                kept = next(
                    t for t in to_approve
                    if (t["node_id"], t["repo"], t["branch"]) == key
                )
                reason = f"superseded by {kept['deploy_id']}"
                await store.update_deploy_status(
                    ev["deploy_id"], DeployStatus.REJECTED, reject_reason=reason
                )
                await hub.broadcast_to_dashboards({
                    "type": "status_change",
                    "deploy_id": ev["deploy_id"],
                    "status": DeployStatus.REJECTED.value,
                    "node_id": ev["node_id"],
                    "reject_reason": reason,
                })
                auto_superseded.append(ev["deploy_id"])
                continue
            seen_groups.add(key)
            to_approve.append(ev)

        approved: list[str] = []
        failed: list[dict[str, Any]] = []

        for event in to_approve:
            deploy_id = event["deploy_id"]
            node_id = event["node_id"]

            await store.update_deploy_status(
                deploy_id, DeployStatus.APPROVED, approved_by="dashboard"
            )

            msg = DeployApproval(deploy_id=deploy_id, approved_by="dashboard")
            sent = await hub.send_to_node(node_id, msg)

            if sent:
                # Race-safe: register before any further await so a fast
                # DeployResult finds the entry. See approve_deploy comment.
                await hub.register_pending_deploy(
                    deploy_id, node_id, event["repo"], event["branch"]
                )
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

        response: dict[str, Any] = {"approved": approved, "failed": failed}
        if auto_superseded:
            response["superseded"] = auto_superseded
        return JSONResponse(response)

    async def service_command(request: Request) -> JSONResponse:
        """POST /api/orch/service-command — send restart/stop to a node's service."""
        body = await request.json()
        node_id = body.get("node_id")
        service_name = body.get("service_name")
        action = body.get("action")

        if not all([node_id, service_name, action]):
            return JSONResponse(
                {"error": "node_id, service_name, action required"}, status_code=400
            )
        if action not in ("restart", "stop"):
            return JSONResponse(
                {"error": "action must be 'restart' or 'stop'"}, status_code=400
            )

        command_id = f"{node_id}:{service_name}:{action}:{int(time.time())}"
        msg = ServiceCommand(
            command_id=command_id, service_name=service_name, action=action
        )
        sent = await hub.send_to_node(node_id, msg)

        if not sent:
            return JSONResponse(
                {"error": "node not connected"}, status_code=503
            )

        # Track in-flight command for timeout/disconnect handling.
        await hub.register_pending_command(command_id, node_id, service_name, action)

        return JSONResponse({"command_id": command_id, "status": "sent"})

    return [
        Route("/api/orch/pending", get_pending, methods=["GET"]),
        Route("/api/orch/nodes", get_nodes, methods=["GET"]),
        Route("/api/orch/history", get_history, methods=["GET"]),
        Route("/api/orch/approve", approve_deploy, methods=["POST"]),
        Route("/api/orch/reject", reject_deploy, methods=["POST"]),
        Route("/api/orch/approve-all", approve_all, methods=["POST"]),
        Route("/api/orch/service-command", service_command, methods=["POST"]),
    ]
