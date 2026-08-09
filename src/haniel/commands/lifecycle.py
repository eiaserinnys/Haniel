"""Public resident lifecycle control CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from haniel.core.one_shot_handover import stop


@click.group("lifecycle")
def lifecycle_group() -> None:
    """Control a resident Haniel instance by config identity."""


@lifecycle_group.command("stop")
@click.argument("config", type=click.Path(path_type=Path))
@click.option("--instance-id", required=True)
@click.option("--wait", "wait_timeout", type=float, default=60.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def stop_command(
    config: Path, instance_id: str, wait_timeout: float, as_json: bool
) -> None:
    """Stop only the resident instance whose identity was observed by the caller."""
    result = stop(
        config,
        expected_instance=instance_id,
        wait_timeout=wait_timeout,
    )
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        click.echo("stopped" if result.get("ok") else "stop failed")
    if not result.get("ok"):
        error = result.get("error") or {}
        raise click.ClickException(str(error.get("message", "stop failed")))
