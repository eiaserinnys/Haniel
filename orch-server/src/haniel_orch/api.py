"""REST API routes for the Orchestrator dashboard."""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .event_store import EventStore
from .deploy_attempt_coordinator import PlanRejected
from .hub import WebSocketHub
from .protocol import DeployReject, DeployStatus, ServiceCommand

logger = logging.getLogger(__name__)

SERVICE_SCOPED_COMMANDS = {"start", "restart", "stop", "reload"}
CONFIG_COMMANDS = {"reload-config", "register-service", "register-repo"}
SERVICE_COMMAND_ACTIONS = SERVICE_SCOPED_COMMANDS | CONFIG_COMMANDS


def create_api_routes(hub: WebSocketHub, store: EventStore) -> list[Route]:
    """Create REST API routes bound to the given hub and store."""

    async def _approve_one(event: dict[str, Any], approved_by: str, source: str) -> str:
        return await hub.deploy_coordinator.approve_manual(
            event, approved_by=approved_by, source=source
        )

    async def get_pending(request: Request) -> JSONResponse:
        """GET /api/orch/pending — list active deploys (pending + deploying).

        PendingView shows both states so that a deploy stays visible after
        approval (DEPLOYING) until the node reports a terminal result.
        """
        deploys = await store.get_active_deploys()
        return JSONResponse({"deploys": deploys})

    async def get_nodes(request: Request) -> JSONResponse:
        """GET /api/orch/nodes — list all registered nodes.

        services field semantics:
          - omitted (key absent): node hasn't reported services yet (pre-handshake
            or stale client without get_services_info callable wired)
          - [] (empty list): node has no services configured
          - [...]: list of service info dicts
        Distinguishing None vs [] lets the dashboard show "no services configured"
        for the empty case without conflating it with "information missing".
        """
        nodes = await store.get_nodes()
        for node in nodes:
            connected_node = hub.registry.get_node(node["node_id"])
            node["connected"] = 1 if connected_node is not None else 0
            if connected_node is not None and connected_node.services is not None:
                node["services"] = connected_node.services
        return JSONResponse({"nodes": nodes})

    async def get_history(request: Request) -> JSONResponse:
        """GET /api/orch/history — list deploy events, newest first.

        Default excludes auto-superseded entries (reject_reason starting with
        'superseded by ') so the dashboard history isn't drowned out by them.
        Pass ?include_superseded=1 to expose them for audit chains.
        """
        limit = int(request.query_params.get("limit", "50"))
        include_superseded = (
            request.query_params.get("include_superseded") == "1"
        )
        history = await store.get_deploy_history(
            limit=limit, include_superseded=include_superseded
        )
        return JSONResponse({"deploys": history})

    async def approve_deploy(request: Request) -> JSONResponse:
        """POST /api/orch/approve — approve a pending deploy.

        The coordinator either begins a normal attempt transaction or runs a
        retry preflight before beginning and sending the immutable approval.
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

        approved_by = body.get("approved_by", "dashboard")
        try:
            orchestrator_attempt_id = await _approve_one(
                event, approved_by, "manual_single"
            )
            await hub.supersede_pending(
                event["node_id"], event["repo"], event["branch"]
            )
            return JSONResponse(
                {
                    "deploy_id": deploy_id,
                    "orchestrator_attempt_id": orchestrator_attempt_id,
                    "status": "deploying",
                }
            )
        except PlanRejected as exc:
            return JSONResponse(
                {"error": str(exc), "deploy_id": deploy_id},
                status_code=exc.status_code,
            )

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
        await hub.deploy_coordinator.resolve_terminal_canonicals(
            {deploy_id}, message="deploy was rejected during preflight"
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

        Within each (node, repo, branch) group, only the latest canonical
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

        # Group by (node, repo, branch). `pending` is ordered by canonical
        # detected_at, then insertion time, so the first occurrence is latest. Older
        # entries in the same group are auto-superseded via the single
        # source of truth (hub.supersede_pending) — keeps the reject_reason
        # format ("superseded by <id>") and broadcast payload identical to
        # the generation-time supersede path. See atom 260519.01.
        seen_groups: set[tuple[str, str, str]] = set()
        to_approve: list[dict[str, Any]] = []
        for ev in pending:
            key = (ev["node_id"], ev["repo"], ev["branch"])
            if key in seen_groups:
                # Older deploy in the same branch — handled below by
                # supersede_pending chooses the transaction-time latest row.
                continue
            seen_groups.add(key)
            to_approve.append(ev)

        # Supersede older PENDINGs in each group through the canonical hub
        # method. Defensive: by this point generation-time supersede should
        # have already covered most cases, but a stale row that slipped in
        # before the upgrade still gets cleaned up.
        auto_superseded: list[str] = []
        for ev in to_approve:
            superseded = await hub.supersede_pending(
                ev["node_id"], ev["repo"], ev["branch"]
            )
            auto_superseded.extend(superseded)

        approved: list[str] = []
        failed: list[dict[str, Any]] = []

        for event in to_approve:
            deploy_id = event["deploy_id"]

            try:
                await _approve_one(event, "dashboard", "manual_batch")
                approved.append(deploy_id)
            except (PlanRejected, ValueError) as exc:
                failed.append({
                    "deploy_id": deploy_id,
                    "reason": str(exc),
                })

        response: dict[str, Any] = {"approved": approved, "failed": failed}
        if auto_superseded:
            response["superseded"] = auto_superseded
        return JSONResponse(response)

    async def service_command(request: Request) -> JSONResponse:
        """POST /api/orch/service-command — send lifecycle command to a node."""
        body = await request.json()
        node_id = body.get("node_id")
        service_name = body.get("service_name") or ""
        action = body.get("action")
        payload = body.get("payload")

        if not all([node_id, action]):
            return JSONResponse(
                {"error": "node_id and action required"}, status_code=400
            )
        if action not in SERVICE_COMMAND_ACTIONS:
            return JSONResponse(
                {
                    "error": (
                        "action must be one of "
                        f"{', '.join(sorted(SERVICE_COMMAND_ACTIONS))}"
                    )
                },
                status_code=400,
            )
        if payload is not None and not isinstance(payload, dict):
            return JSONResponse(
                {"error": "payload must be an object"}, status_code=400
            )

        payload = payload or {}
        command_target = service_name

        if action in SERVICE_SCOPED_COMMANDS and not service_name:
            return JSONResponse(
                {"error": f"service_name is required for action '{action}'"},
                status_code=400,
            )
        if action == "reload-config":
            command_target = service_name or "config"
            service_name = command_target
        elif action == "register-service":
            command_target = service_name or payload.get("name", "")
            if not command_target:
                return JSONResponse(
                    {"error": "service_name or payload.name is required"},
                    status_code=400,
                )
            if payload.get("service_config", payload.get("config")) is None:
                return JSONResponse(
                    {"error": "payload.service_config is required"},
                    status_code=400,
                )
            service_name = command_target
        elif action == "register-repo":
            command_target = service_name or payload.get("name", "")
            if not command_target:
                return JSONResponse(
                    {"error": "service_name or payload.name is required"},
                    status_code=400,
                )
            if payload.get("repo_config", payload.get("config")) is None:
                return JSONResponse(
                    {"error": "payload.repo_config is required"},
                    status_code=400,
                )
            service_name = command_target

        command_id = f"{node_id}:{command_target}:{action}:{int(time.time())}"
        msg = ServiceCommand(
            command_id=command_id,
            service_name=service_name,
            action=action,
            payload=payload or None,
        )
        sent = await hub.send_to_node(node_id, msg)

        if not sent:
            return JSONResponse(
                {"error": "node not connected"}, status_code=503
            )

        # Track in-flight command for timeout/disconnect handling.
        await hub.register_pending_command(
            command_id, node_id, command_target, action
        )

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
