"""Public manifest-aware one-shot handover CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from haniel.core.one_shot_handover import execute_manifest_handover_once
from haniel.core.handover_result import handover_error_code
from haniel.core.safety_redaction import bounded_redact_text


@click.command("handover")
@click.argument("config", type=click.Path(path_type=Path, exists=True))
@click.argument("repo")
@click.option("--target-ref", required=True)
@click.option(
    "--expected-operation",
    type=click.Choice(["fresh_install", "upgrade"]),
    required=True,
)
@click.option("--request-id", required=True)
@click.option("--start-owner", is_flag=True)
@click.option("--wait", "wait_timeout", type=float, default=300.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def handover_command(
    config: Path,
    repo: str,
    target_ref: str,
    expected_operation: str,
    request_id: str,
    start_owner: bool,
    wait_timeout: float,
    as_json: bool,
) -> None:
    """Execute one immutable release handover through the resident owner."""
    try:
        result = execute_manifest_handover_once(
            config,
            repo,
            target_ref,
            expected_operation,
            request_id,
            start_owner,
            wait_timeout,
        )
    except Exception as error:
        safe_message = bounded_redact_text(str(error))
        if not as_json:
            raise click.ClickException(safe_message) from error
        payload = {
            "schema_version": "haniel.handover.result.v1",
            "ok": False,
            "request_id": request_id,
            "release_id": None,
            "operation": expected_operation,
            "phase": "failed",
            "previous_head": None,
            "target_head": None,
            "journal_path": None,
            "backup_path": None,
            "recovered": False,
            "retryable": True,
            "error": {
                "code": handover_error_code(error),
                "message": safe_message,
            },
        }
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        raise click.exceptions.Exit(1) from error

    payload = result.to_dict()
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        click.echo(f"{result.phase}: {repo} {result.target_head or 'unresolved'}")
    if not result.ok:
        if as_json:
            raise click.exceptions.Exit(1)
        raise click.ClickException(
            result.error["message"] if result.error else "handover failed"
        )
