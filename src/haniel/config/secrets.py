"""Resolve explicitly supported secret references without serializing values."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from .model import HanielConfig

_REFERENCE = re.compile(r"\$\{secret:([^}]*)\}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SecretReferenceError(ValueError):
    """A value-free description of an invalid secret reference."""


def secret_fields(config: HanielConfig) -> Iterator[tuple[str, Any, str, bool]]:
    """Yield field path, owner, attribute and whether it is a Haniel secret."""
    for index, webhook in enumerate(config.webhooks or []):
        yield f"webhooks.{index}.url", webhook, "url", True
    if config.slack:
        for name in ("bot_token", "app_token"):
            yield f"slack.{name}", config.slack, name, True
    if config.orchestrator_client:
        yield "orchestrator_client.token", config.orchestrator_client, "token", True
    if config.dashboard:
        yield "dashboard.token", config.dashboard, "token", True
    if config.install:
        service = config.install.service
        if service and service.service_account:
            yield (
                "install.service.service_account.password",
                service.service_account,
                "password",
                True,
            )
        for name, item in (config.install.configs or {}).items():
            yield f"install.configs.{name}.content", item, "content", False
            for index, key in enumerate(item.keys or []):
                yield (
                    f"install.configs.{name}.keys.{index}.default",
                    key,
                    "default",
                    False,
                )


def resolve_secret(
    value: str | None,
    field_path: str,
    values: dict[str, str],
    source: str = "no secrets_file is set",
) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if not _NAME.fullmatch(name):
            raise SecretReferenceError(f"Invalid secret reference at {field_path}")
        if name not in values:
            raise SecretReferenceError(
                f"Unresolved secret at {field_path}: {name} ({source})"
            )
        return values[name]

    result = _REFERENCE.sub(replace, value)
    if "${secret:" in _REFERENCE.sub("", value):
        raise SecretReferenceError(f"Invalid secret reference at {field_path}")
    return result


def parse_config(data: Any, config_dir: Path) -> HanielConfig:
    from .model import HanielConfig

    config = HanielConfig.model_validate(data or {})
    fields = list(secret_fields(config))
    if config.secrets_file:
        # Reuse the service dotenv parser without importing its values into the process.
        from ..core.service_environment import read_service_environment_file

        secrets_path = (
            (config_dir / config.secrets_file).expanduser().resolve(strict=False)
        )
        try:
            config._secret_values = read_service_environment_file(secrets_path).values
            config._secret_source = f"looked in {secrets_path}"
        except (OSError, RuntimeError) as error:
            config._secret_source = f"cannot read {secrets_path}"
            for field_path, owner, attribute, _ in fields:
                value = getattr(owner, attribute)
                match = _REFERENCE.search(value or "")
                if match and _NAME.fullmatch(match.group(1)):
                    raise SecretReferenceError(
                        f"Unresolved secret at {field_path}: {match.group(1)} "
                        f"({config._secret_source})"
                    ) from None
            raise ValueError(f"Cannot read secrets_file: {secrets_path}") from error
    for field_path, owner, attribute, _ in fields:
        config.resolve_secret(getattr(owner, attribute), field_path)
    return config


def resolved_config(config: HanielConfig) -> HanielConfig:
    """Create a transient copy for equality comparison, leaving the model intact."""
    result = config.model_copy(deep=True)
    for field_path, owner, attribute, _ in secret_fields(result):
        setattr(
            owner,
            attribute,
            config.resolve_secret(getattr(owner, attribute), field_path),
        )
    return result


def plain_secret_errors(config: HanielConfig, raw: str) -> list[str]:
    errors = []
    for field_path, owner, attribute, own_secret in secret_fields(config):
        value = getattr(owner, attribute)
        if own_secret and value and not _REFERENCE.search(value):
            errors.append(f"Plain secret at {field_path}")
    for name, value in config._secret_values.items():
        if len(value) >= 8 and value in raw:
            errors.append(f"Secret value present in config: {name}")
    return errors
