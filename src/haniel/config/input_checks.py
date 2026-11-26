"""
Checks for what a haniel.yaml author typed: unknown keys and bad values.

The models read a config leniently: a key they do not know is dropped and a
value they cannot use is kept as written. These checks report both. They are
plain functions rather than model validators because each caller reacts
differently: ``haniel validate`` fails, a config save through MCP or REST
rejects the entry being written, and ``haniel run`` only logs a warning.
"""

import difflib
import logging
import math
import signal
import types
import typing
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .model import (
    HanielConfig,
    RepoConfig,
    ServiceConfig,
    ServiceShutdownConfig,
    ShutdownConfig,
)
from .validators import ValidationError

logger = logging.getLogger(__name__)

READY_FORMATS = "port:<1-65535>, delay:<seconds>, log:<pattern>, or http:<url>"
_READY_PREFIXES = ("port", "delay", "log", "http")


def _error(message: str) -> ValidationError:
    return ValidationError(message=message, severity="error")


def _did_you_mean(name: str, known: typing.Iterable[str]) -> str:
    matches = difflib.get_close_matches(name, list(known), n=1, cutoff=0.6)
    return f" (did you mean {matches[0]}?)" if matches else ""


def _nested_models(annotation: Any) -> list[tuple[str, type[BaseModel]]]:
    """Find the models inside an annotation as (container kind, model) pairs.

    The kind is "one" for a plain model, "list" for list[Model] and "map" for
    dict[str, Model].
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        found: list[tuple[str, type[BaseModel]]] = []
        for arg in args:
            found.extend(_nested_models(arg))
        return found
    if origin is list and args:
        return [
            ("list", model) for kind, model in _nested_models(args[0]) if kind == "one"
        ]
    if origin is dict and len(args) == 2:
        return [
            ("map", model) for kind, model in _nested_models(args[1]) if kind == "one"
        ]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [("one", annotation)]
    return []


def find_unknown_keys(
    data: Any, model: type[BaseModel], path: str = ""
) -> list[ValidationError]:
    """List the keys in raw YAML data that ``model`` does not read.

    ``path`` is the dotted location of ``data``; it prefixes every reported key.
    """
    if not isinstance(data, dict):
        return []

    fields = {(info.alias or name): info for name, info in model.model_fields.items()}
    errors: list[ValidationError] = []
    for raw_key, value in data.items():
        key = str(raw_key)
        key_path = f"{path}.{key}" if path else key
        info = fields.get(key)
        if info is None:
            errors.append(_error(f"unknown key {key_path}{_did_you_mean(key, fields)}"))
            continue
        for kind, nested in _nested_models(info.annotation):
            if kind == "one":
                errors.extend(find_unknown_keys(value, nested, key_path))
            elif kind == "list" and isinstance(value, list):
                for index, item in enumerate(value):
                    errors.extend(
                        find_unknown_keys(item, nested, f"{key_path}.{index}")
                    )
            elif kind == "map" and isinstance(value, dict):
                for item_key, item in value.items():
                    errors.extend(
                        find_unknown_keys(item, nested, f"{key_path}.{item_key}")
                    )
    return errors


def ready_format_error(ready: str) -> str | None:
    """Explain why a ``ready`` value cannot be used, or None when it is fine."""
    prefix, separator, value = ready.partition(":")
    if not separator or prefix not in _READY_PREFIXES:
        close = difflib.get_close_matches(prefix, _READY_PREFIXES, n=1, cutoff=0.6)
        hint = f" (did you mean {close[0]}:?)" if separator and close else ""
        return f"unknown ready format {ready!r}{hint}; expected {READY_FORMATS}"
    if prefix == "port":
        try:
            port = int(value)
        except ValueError:
            return f"ready port must be an integer from 1 to 65535: {ready!r}"
        if not 1 <= port <= 65535:
            return f"ready port must be an integer from 1 to 65535: {ready!r}"
    elif prefix == "delay":
        try:
            seconds = float(value)
        except ValueError:
            return f"ready delay must be a number of seconds, 0 or more: {ready!r}"
        if not math.isfinite(seconds) or seconds < 0:
            return f"ready delay must be a number of seconds, 0 or more: {ready!r}"
    elif not value.strip():
        return f"ready {prefix} condition is empty: {ready!r}"
    return None


def _signal_error(name: str) -> str | None:
    if name in signal.Signals.__members__:
        return None
    upper = name.upper()
    if upper in signal.Signals.__members__:
        return f"unknown signal {name!r} (did you mean {upper}?)"
    return f"unknown signal {name!r}{_did_you_mean(name, signal.Signals.__members__)}"


def _shutdown_errors(
    path: str, shutdown: ShutdownConfig | ServiceShutdownConfig | None
) -> list[ValidationError]:
    if shutdown is None:
        return []
    message = _signal_error(shutdown.signal)
    return [_error(f"{path}.signal: {message}")] if message else []


def service_value_errors(name: str, service: ServiceConfig) -> list[ValidationError]:
    path = f"services.{name}"
    errors: list[ValidationError] = []
    if service.ready:
        message = ready_format_error(service.ready)
        if message:
            errors.append(_error(f"{path}.ready: {message}"))
    if service.restart_delay is not None and service.restart_delay < 0:
        errors.append(
            _error(f"{path}.restart_delay must be 0 or more: {service.restart_delay}")
        )
    errors.extend(_shutdown_errors(f"{path}.shutdown", service.shutdown))
    return errors


def repo_value_errors(name: str, repo: RepoConfig) -> list[ValidationError]:
    if not repo.url.strip():
        return [_error(f"repos.{name}.url must not be empty")]
    return []


def _global_value_errors(config: HanielConfig) -> list[ValidationError]:
    errors = _shutdown_errors("shutdown", config.shutdown)
    if config.backoff:
        for field in ("base_delay", "max_delay"):
            delay = getattr(config.backoff, field)
            if delay < 0:
                errors.append(_error(f"backoff.{field} must be 0 or more: {delay}"))
    if config.orchestrator_client:
        for field in ("url", "token"):
            if not getattr(config.orchestrator_client, field).strip():
                errors.append(_error(f"orchestrator_client.{field} must not be empty"))
    return errors


def config_input_issues(config: HanielConfig, raw: Any) -> list[ValidationError]:
    """Every unknown key and bad value in one config, plus the no-service warning.

    ``raw`` is the YAML data the config was parsed from.
    """
    issues = find_unknown_keys(raw, HanielConfig)
    issues.extend(_global_value_errors(config))
    for name, repo in config.repos.items():
        issues.extend(repo_value_errors(name, repo))
    for name, service in config.services.items():
        issues.extend(service_value_errors(name, service))
    if not config.services:
        issues.append(
            ValidationError(
                message="no services are defined",
                severity="warning",
                location="services",
            )
        )
    return issues


def entry_input_errors(
    path: str, raw: Any, model: type[BaseModel], entry: ServiceConfig | RepoConfig
) -> list[ValidationError]:
    """Unknown keys and bad values in one service or repo entry about to be saved."""
    errors = find_unknown_keys(raw, model, path)
    name = path.split(".", 1)[1]
    if isinstance(entry, ServiceConfig):
        errors.extend(service_value_errors(name, entry))
    else:
        errors.extend(repo_value_errors(name, entry))
    return errors


def read_raw_config(path: Path) -> Any:
    with open(path, encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def input_issues(
    config_path: Path, config: HanielConfig | None
) -> list[ValidationError]:
    """Unknown keys and bad values in a config file.

    A config that failed to load still gets its unknown keys reported, since a
    misspelled key is often why it failed.
    """
    try:
        raw = read_raw_config(config_path)
    except (OSError, yaml.YAMLError):
        return []
    if config is None:
        return find_unknown_keys(raw, HanielConfig)
    return config_input_issues(config, raw)


def log_config_issues(config_path: Path, config: HanielConfig) -> None:
    """Log what ``haniel validate`` would report, without stopping anything."""
    try:
        raw = read_raw_config(config_path)
    except (OSError, yaml.YAMLError):
        return
    for issue in config_input_issues(config, raw):
        logger.warning("Config issue in %s: %s", config_path, issue.message)
