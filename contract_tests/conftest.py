"""Fixtures: one in-process orchestrator on a loopback port, a dashboard socket,
and real node clients. Nothing here touches a production node or database."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from haniel.config.model import OrchestratorClientConfig
from haniel.core.repo_reconciliation import RepoReconciliationSnapshot
from haniel.integrations.orchestrator_client import OrchestratorClient
from haniel_orch.server import OrchestratorConfig, OrchestratorServer

NODE_TOKEN = "contract-node-token"
NODE_ID = "contract-node"
REPO = "appA"
BRANCH = "main"
TARGET = "c0ffee" + "0" * 34


@dataclass
class Orchestrator:
    server: OrchestratorServer
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def node_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/node"

    @property
    def dashboard_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/dashboard"

    @property
    def hub(self):
        return self.server.hub


@pytest.fixture
async def orchestrator(tmp_path: Path):
    config = OrchestratorConfig(
        token=NODE_TOKEN,
        db_path=str(tmp_path / "orchestrator.db"),
        host="127.0.0.1",
        port=0,
        heartbeat_timeout=600.0,
        command_timeout_sec=3.0,
        deploy_timeout_sec=60.0,
    )
    server = OrchestratorServer(config)
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            server.build_app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    serving = asyncio.create_task(uvicorn_server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not uvicorn_server.started:
        if serving.done():
            serving.result()
            raise AssertionError("uvicorn exited before starting")
        assert loop.time() < deadline, "uvicorn did not start"
        await asyncio.sleep(0.01)
    port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
    yield Orchestrator(server=server, port=port)
    uvicorn_server.should_exit = True
    await asyncio.wait_for(serving, 10)


class Dashboard:
    """Dashboard WebSocket consumer: await the first event matching a predicate."""

    def __init__(self, socket) -> None:
        self._socket = socket
        self._unmatched: list[dict] = []

    async def wait_for(
        self, predicate: Callable[[dict], bool], *, timeout: float = 10.0
    ) -> dict:
        for index, event in enumerate(self._unmatched):
            if predicate(event):
                return self._unmatched.pop(index)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            assert remaining > 0, (
                f"no matching dashboard event; saw {self._unmatched!r}"
            )
            raw = await asyncio.wait_for(self._socket.recv(), remaining)
            event = json.loads(raw)
            if predicate(event):
                return event
            self._unmatched.append(event)


@pytest.fixture
async def dashboard(orchestrator: Orchestrator):
    async with websockets.connect(orchestrator.dashboard_ws_url) as socket:
        yield Dashboard(socket)


@pytest.fixture
async def http(orchestrator: Orchestrator):
    async with httpx.AsyncClient(base_url=orchestrator.base_url, timeout=10) as client:
        yield client


class HeldDeployment:
    """Approval handler that parks the deployment on an event until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.approvals: list[dict] = []

    def handle(self, approval: dict, _progress) -> None:
        self.approvals.append(approval)
        self.entered.set()
        if not self.release.wait(timeout=60):
            raise TimeoutError("held deployment was never released")
        return None

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 15), "deploy never started"


def settled_snapshot(
    repo: str, branch: str, deploy_id: str
) -> RepoReconciliationSnapshot:
    target = deploy_id.rsplit(":", 1)[-1]
    return RepoReconciliationSnapshot(
        node_id=NODE_ID,
        repo=repo,
        branch=branch,
        local_head=target,
        remote_head=target,
        deploy_id=deploy_id,
    )


@dataclass
class NodeFactory:
    orchestrator: Orchestrator
    clients: list[OrchestratorClient] = field(default_factory=list)

    def start(
        self,
        *,
        approval_handler,
        service_command_handler=None,
        reconnect_base: float = 0.05,
    ) -> OrchestratorClient:
        config = OrchestratorClientConfig(
            url=self.orchestrator.node_ws_url,
            token=NODE_TOKEN,
            node_id=NODE_ID,
            reconnect_base=reconnect_base,
            reconnect_max=max(reconnect_base, 0.2),
            ping_interval=5.0,
            ping_timeout=5.0,
        )
        client = OrchestratorClient(
            config,
            haniel_version="contract",
            get_services_info=lambda: [],
            service_command_handler=service_command_handler,
            deploy_approval_handler=approval_handler,
            repo_snapshot_handler=settled_snapshot,
        )
        self.clients.append(client)
        client.start()
        return client


@pytest.fixture
async def nodes(orchestrator: Orchestrator):
    factory = NodeFactory(orchestrator)
    yield factory
    for client in factory.clients:
        await asyncio.to_thread(client.stop)


async def approve_held_deployment(
    client: OrchestratorClient,
    dashboard: Dashboard,
    http: httpx.AsyncClient,
    held: HeldDeployment,
) -> str:
    """Connect, announce one change, approve it, and wait until the node holds it."""
    await dashboard.wait_for(
        lambda e: e["type"] == "node_connected" and e["node_id"] == NODE_ID
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not client._connected:
        assert loop.time() < deadline, "node client never marked itself connected"
        await asyncio.sleep(0.01)
    client.notify_change(
        REPO,
        BRANCH,
        commits=[f"{TARGET} contract change"],
        affected_services=["web"],
        target_head=TARGET,
        wait=True,
    )
    pending = await dashboard.wait_for(lambda e: e["type"] == "new_pending")
    deploy_id = pending["deploy_id"]
    assert deploy_id == f"{NODE_ID}:{REPO}:{BRANCH}:{TARGET}"
    response = await http.post(
        "/api/orch/approve", json={"deploy_id": deploy_id, "approved_by": "contract"}
    )
    assert response.status_code == 200, response.text
    await held.wait_entered()
    return deploy_id
