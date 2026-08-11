"""Pure validation for deployment quiescence receipts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def validate_quiescence_receipt(
    receipt: dict[str, Any] | None,
    *,
    request_id: str | None,
    repo_name: str,
    target_head: str,
    owner_instance: str | None,
    quiescence_nonce: str | None,
    config_digest: str | None,
    writer_services: Sequence[str],
) -> None:
    """Require one identity-bound receipt covering every writer service."""

    if not isinstance(receipt, dict):
        raise RuntimeError(
            "QUIESCENCE_REQUIRED: resident owner did not acknowledge stop"
        )
    expected_fields = {
        "request_id": request_id,
        "repo": repo_name,
        "target_head": target_head,
        "owner_instance": owner_instance,
        "quiescence_nonce": quiescence_nonce,
    }
    if config_digest is not None:
        expected_fields["config_digest"] = config_digest
    mismatched = [
        key
        for key, expected in expected_fields.items()
        if not isinstance(expected, str) or not expected or receipt.get(key) != expected
    ]
    writers = set(writer_services)
    stopped = receipt.get("stopped_services")
    already_stopped = receipt.get("already_stopped_services")
    quiesced = receipt.get("quiesced_services")
    if not all(
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        for value in (stopped, already_stopped, quiesced)
    ):
        mismatched.append("service_sets")
    else:
        stopped_set = set(stopped)
        already_stopped_set = set(already_stopped)
        quiesced_set = set(quiesced)
        if (
            not writers
            or stopped_set & already_stopped_set
            or stopped_set | already_stopped_set != writers
            or quiesced_set != writers
            or len(stopped) != len(stopped_set)
            or len(already_stopped) != len(already_stopped_set)
            or len(quiesced) != len(quiesced_set)
        ):
            mismatched.append("service_sets")
    if mismatched:
        raise RuntimeError(
            "QUIESCENCE_REQUIRED: receipt does not match current deployment intent"
        )
