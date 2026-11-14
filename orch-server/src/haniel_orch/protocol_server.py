"""서버에서 노드로 가는 글의 모델. `protocol`이 재노출한다.

2b-2에서 protocol 모듈의 크기를 지키려 옮겼다. 내용은 그대로다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

from .protocol_base import StrictMessage


class DeployApproval(StrictMessage):
    """Server instructs node to proceed with deployment."""

    type: Literal["deploy_approval"] = "deploy_approval"
    deploy_id: str
    orchestrator_attempt_id: str
    execution_mode: Literal["execute", "evidence_recovery"]
    probe_id: str | None = None
    connection_generation: str
    preflight_fingerprint: str | None = None
    approved_by: str = "dashboard"


class DeployPlanProbe(StrictMessage):
    """Read-only, generation-bound planning request; it grants no execution."""

    type: Literal["deploy_plan_probe"] = "deploy_plan_probe"
    probe_id: str
    connection_generation: str
    deploy_id: str
    node_id: str
    repo: str
    branch: str
    target_head: str
    source_orchestrator_attempt_id: str | None = None
    retry_lineage: list[str] = Field(default_factory=list)
    expected_manifest_identity: str | None = None
    expected_manifest_digest: str | None = None
    deadline_at: str
    requested_orchestrator_attempt_id: str | None = None


class AcceptedDeployAttemptAck(StrictMessage):
    type: Literal["deploy_attempt_ack"] = "deploy_attempt_ack"
    accepted: Literal[True]
    requested_orchestrator_attempt_id: str
    begun_orchestrator_attempt_id: str
    deploy_id: str
    connection_generation: str
    probe_id: str
    execution_mode: Literal["execute", "evidence_recovery"]
    preflight_fingerprint: str


class RejectedDeployAttemptAck(StrictMessage):
    type: Literal["deploy_attempt_ack"] = "deploy_attempt_ack"
    accepted: Literal[False]
    requested_orchestrator_attempt_id: str
    deploy_id: str
    connection_generation: str
    error: Literal["retry_requires_manual_approval", "self_update_pending"]


DeployAttemptAck = Annotated[
    Union[AcceptedDeployAttemptAck, RejectedDeployAttemptAck],
    Field(discriminator="accepted"),
]


class DeployReject(BaseModel):
    """Server instructs node to skip deployment."""

    type: str = "deploy_reject"
    deploy_id: str
    reason: str


class DeployTerminalReceived(StrictMessage):
    """이 시도의 종결 묶음을 적었다(wire-v1 3절). 종결 증거가 종결된 뒤에만, 판 1 연결에만 보낸다.

    outcome은 이 글이 요청이나 시도를 닫았으면 closed_now, 이미 닫혀 있었으면 already_closed다.
    """

    type: Literal["deploy_terminal_received"] = "deploy_terminal_received"
    deploy_id: str | None = None
    orchestrator_attempt_id: str | None = None
    node_attempt_id: str | None = None
    outcome: Literal["closed_now", "already_closed"]

    @model_validator(mode="after")
    def validate_exactly_one_id(self) -> "DeployTerminalReceived":
        if (self.orchestrator_attempt_id is None) == (self.node_attempt_id is None):
            raise ValueError("deploy_terminal_received needs exactly one of orchestrator_attempt_id, node_attempt_id")
        return self


class DeployReportAck(StrictMessage):
    """Tell the current node connection why a late report was ignored."""

    type: Literal["deploy_report_ack"] = "deploy_report_ack"
    deploy_id: str
    orchestrator_attempt_id: str
    report_type: Literal["deploy_result", "repo_reconciliation"]
    status: Literal["ignored"] = "ignored"
    reason: Literal["unknown_attempt", "terminal_attempt", "identity_mismatch"]
    attempt_outcome: str | None = None


class ServiceCommand(BaseModel):
    """Server instructs node to run a service/config lifecycle command."""

    type: str = "service_command"
    command_id: str  # unique ID for tracking
    service_name: str
    action: str
    payload: dict[str, Any] | None = None
