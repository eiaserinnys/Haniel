"""허브의 서비스 명령 추적(진행 중 명령, 시간 초과, 끊긴 노드의 명령 정리). 2b-2에서 허브 모듈의 크기를 지키려 옮겼다. 내용은 그대로다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .protocol import ServiceCommandResult


@dataclass
class PendingCommand:
    """In-flight service command tracked by the hub."""

    node_id: str
    service_name: str
    action: str
    timeout_task: asyncio.Task[None]


class HubServiceCommands:
    """Mixin: 진행 중 서비스 명령의 추적과 시간 초과."""

    async def _handle_service_command_result(self, msg: ServiceCommandResult) -> None:
        """Process a ServiceCommandResult: cancel timeout task, broadcast to dashboards."""
        # Race-safe ownership transfer: pop ensures the timeout task can no longer broadcast.
        pending = self._pending_commands.pop(msg.command_id, None)
        if pending is not None:
            pending.timeout_task.cancel()
        await self.broadcast_to_dashboards(
            {
                "type": "service_command_result",
                "command_id": msg.command_id,
                "node_id": msg.node_id,
                "service_name": msg.service_name,
                "action": msg.action,
                "success": msg.success,
                "error": msg.error,
                "result": msg.result,
            }
        )

    async def register_pending_command(
        self,
        command_id: str,
        node_id: str,
        service_name: str,
        action: str,
    ) -> None:
        """Track an in-flight service command and schedule a timeout broadcast.

        On timeout (after `command_timeout_sec`), broadcasts a service_command_result
        with success=False, error='timeout'. Cancelled when a real ServiceCommandResult
        arrives or the target node disconnects.
        """
        timeout_task = asyncio.create_task(self._command_timeout(command_id))
        self._pending_commands[command_id] = PendingCommand(
            node_id=node_id,
            service_name=service_name,
            action=action,
            timeout_task=timeout_task,
        )

    async def _command_timeout(self, command_id: str) -> None:
        """Wait `command_timeout_sec`; broadcast timeout if command is still pending."""
        try:
            await asyncio.sleep(self._command_timeout_sec)
        except asyncio.CancelledError:
            return
        # Race-safe: if the result already arrived, pop returns None and we no-op.
        pending = self._pending_commands.pop(command_id, None)
        if pending is None:
            return
        await self.broadcast_to_dashboards(
            {
                "type": "service_command_result",
                "command_id": command_id,
                "node_id": pending.node_id,
                "service_name": pending.service_name,
                "action": pending.action,
                "success": False,
                "error": "timeout",
            }
        )

    async def _cleanup_orphan_commands(self, node_id: str, *, error: str) -> None:
        """Cancel & broadcast for any in-flight commands targeted at the given node.

        Single source of truth for both ws-disconnect (handle_node_ws.finally) and
        heartbeat-timeout (_check_loop) paths so that the dashboard releases the
        matching button rather than waiting for the per-command timeout.
        """
        orphans = [
            cid
            for cid, pending in self._pending_commands.items()
            if pending.node_id == node_id
        ]
        for cid in orphans:
            pending = self._pending_commands.pop(cid)
            pending.timeout_task.cancel()
            await self.broadcast_to_dashboards(
                {
                    "type": "service_command_result",
                    "command_id": cid,
                    "node_id": node_id,
                    "service_name": pending.service_name,
                    "action": pending.action,
                    "success": False,
                    "error": error,
                }
            )
