"""
Finalizer - Phase 3.

Handles the final installation steps:
- Generate config files from collected values
- Register system service (WinSW on Windows)

haniel doesn't care what the configs contain - it just writes files.
"""

import json
import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

from ..config import HanielConfig
from ..config.model import ServiceDefinitionConfig
from .state import InstallState
from .utils import detect_tool_paths, find_winsw

logger = logging.getLogger(__name__)


class Finalizer:
    """Handles finalization of installation."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
        config_filename: str = "haniel.yaml",
    ):
        """Initialize the finalizer.

        Args:
            config: Haniel configuration
            config_dir: Directory containing the config file
            state: Installation state
            config_filename: Name of the config file (e.g. "haniel.yaml")
        """
        self.config = config
        self.config_dir = config_dir
        self.state = state
        self.config_filename = config_filename

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to config_dir.

        Args:
            path: Relative path from config

        Returns:
            Absolute path
        """
        p = Path(path)
        if p.is_absolute():
            return p
        return (self.config_dir / path).resolve()

    def check_all_configs_filled(self) -> bool:
        """Check if all required config values are filled.

        Returns:
            True if all required configs are filled
        """
        if not self.config.install or not self.config.install.configs:
            return True

        for name, cfg in self.config.install.configs.items():
            if cfg.keys:
                for key_cfg in cfg.keys:
                    # Skip keys with defaults
                    if key_cfg.default:
                        continue

                    # Check if value is set
                    if key_cfg.key not in self.state.config_values.get(name, {}):
                        logger.warning(f"Missing required config: {name}.{key_cfg.key}")
                        return False

        return True

    def generate_config_files(self) -> None:
        """Generate config files from collected values."""
        if not self.config.install or not self.config.install.configs:
            return

        for name, cfg in self.config.install.configs.items():
            if cfg.keys:
                self._generate_config_file(name, cfg)

    def _generate_config_file(self, name: str, cfg) -> None:
        """Generate a single config file.

        Args:
            name: Config name
            cfg: ConfigFileConfig
        """
        config_path = self._resolve_path(cfg.path)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Gather all values (from state and defaults)
        values: dict[str, str] = {}

        if cfg.keys:
            for key_cfg in cfg.keys:
                # Get from state first
                value = self.state.config_values.get(name, {}).get(key_cfg.key)

                # Fall back to default
                if value is None and key_cfg.default:
                    value = key_cfg.default
                    # Substitute {root} placeholder
                    value = value.replace("{root}", str(self.config_dir))

                if value is not None:
                    values[key_cfg.key] = value

        # Generate file based on extension
        suffix = config_path.suffix.lower()

        if suffix == ".env":
            self._write_env_file(config_path, values)
        elif suffix == ".json":
            self._write_json_file(config_path, values)
        else:
            # Default to env format
            self._write_env_file(config_path, values)

        logger.info(f"Generated config file: {config_path}")

    def _write_env_file(self, path: Path, values: dict[str, str]) -> None:
        """Write values to a .env file.

        Args:
            path: Path to .env file
            values: Key-value pairs
        """
        lines: list[str] = []

        for key, value in sorted(values.items()):
            # Escape quotes in value
            escaped_value = value.replace('"', '\\"')
            # Quote values that contain spaces or special chars
            if any(c in value for c in [" ", "=", "#", "\n"]):
                lines.append(f'{key}="{escaped_value}"')
            else:
                lines.append(f"{key}={value}")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_json_file(self, path: Path, values: dict[str, str]) -> None:
        """Write values to a JSON file.

        Args:
            path: Path to JSON file
            values: Key-value pairs
        """
        # If file exists, merge with existing
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        # Update with new values
        existing.update(values)

        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def register_service(self) -> None:
        """Register the system service.

        On Windows, uses WinSW. On other platforms, logs instructions.
        """
        if not self.config.install or not self.config.install.service:
            logger.info("No service configuration, skipping registration")
            return

        service_cfg = self.config.install.service

        if platform.system() == "Windows":
            self._register_winsw_service(service_cfg)
        else:
            self._log_service_instructions(service_cfg)

    def _generate_winsw_xml(
        self, service_cfg: ServiceDefinitionConfig, working_dir: str
    ) -> str:
        """Generate WinSW XML configuration.

        When self-update is configured, the service runs haniel-runner.ps1
        (outer loop) instead of haniel directly. See ADR-0002.

        Args:
            service_cfg: Service configuration
            working_dir: Resolved working directory path

        Returns:
            XML configuration string
        """
        use_wrapper = self.config.self_update is not None

        if use_wrapper:
            # Wrapper mode: WinSW → powershell → {self-repo}/haniel-runner.ps1
            # The script lives in the haniel repo (e.g. .self/) but WinSW runs
            # from root. See ADR-0003 for directory structure.
            powershell_path = shutil.which("powershell")
            if not powershell_path:
                raise RuntimeError("PowerShell not found in PATH")

            # Resolve wrapper script path from self repo
            repo_key = self.config.self_update.repo
            repo_config = self.config.repos.get(repo_key)
            repo_path = repo_config.path if repo_config else ".self"
            runner_script = f"{repo_path}/haniel-runner.ps1"

            lines = [
                '<?xml version="1.0" encoding="utf-8"?>',
                "<service>",
                f"  <id>{xml_escape(service_cfg.name)}</id>",
                f"  <executable>{xml_escape(powershell_path)}</executable>",
                f"  <arguments>-ExecutionPolicy Bypass -File {xml_escape(runner_script)}</arguments>",
            ]
        else:
            # Direct mode: WinSW → python → haniel
            python_path = shutil.which("python")
            if not python_path:
                raise RuntimeError("Python not found in PATH")

            lines = [
                '<?xml version="1.0" encoding="utf-8"?>',
                "<service>",
                f"  <id>{xml_escape(service_cfg.name)}</id>",
                f"  <executable>{xml_escape(python_path)}</executable>",
                f"  <arguments>-m haniel.cli run {xml_escape(self.config_filename)}</arguments>",
            ]

        if service_cfg.display:
            lines.append(f"  <name>{xml_escape(service_cfg.display)}</name>")

        lines.append(
            f"  <description>{xml_escape(service_cfg.display or service_cfg.name)}</description>"
        )

        lines.append(
            f"  <workingdirectory>{xml_escape(working_dir)}</workingdirectory>"
        )

        # Environment variables
        has_explicit_path = False
        if service_cfg.environment:
            for k, v in service_cfg.environment.items():
                if k.upper() == "PATH":
                    has_explicit_path = True
                resolved = v.replace("{root}", str(self.config_dir))
                lines.append(
                    f"  <env name={xml_quoteattr(k)} value={xml_quoteattr(resolved)}/>"
                )

        # Auto-detect Node.js/pnpm paths if PATH not explicitly set
        if not has_explicit_path:
            node_paths = detect_tool_paths(["node", "pnpm", "npx"])
            if node_paths:
                path_value = "%PATH%;" + ";".join(node_paths)
                lines.append(
                    f"  <env name=\"PATH\" value={xml_quoteattr(path_value)}/>"
                )
                logger.info(f"Auto-detected Node.js paths for service: {node_paths}")

        # Logging with roll-by-size
        lines.extend(
            [
                '  <log mode="roll">',
                "    <sizeThreshold>10240</sizeThreshold>",
                "    <keepFiles>8</keepFiles>",
                "    <logpath>%BASE%\\logs</logpath>",
                "  </log>",
            ]
        )

        # Service account — run as user instead of LocalSystem
        if service_cfg.service_account:
            sa = service_cfg.service_account
            raw_username = sa.username
            # Parse domain\user: ".\\LG" -> (".", "LG"), "DOMAIN\\user" -> ("DOMAIN", "user")
            if "\\" in raw_username:
                domain, username = raw_username.split("\\", 1)
            else:
                domain, username = ".", raw_username
            lines.append("  <serviceaccount>")
            lines.append(f"    <domain>{xml_escape(domain)}</domain>")
            lines.append(f"    <user>{xml_escape(username)}</user>")
            if sa.password is not None:
                lines.append(f"    <password>{xml_escape(sa.password)}</password>")
            if sa.allow_service_logon:
                lines.append("    <allowservicelogon>true</allowservicelogon>")
            lines.append("  </serviceaccount>")

        # Graceful shutdown timeout and failure recovery
        lines.extend(
            [
                "  <stoptimeout>15 sec</stoptimeout>",
                '  <onfailure action="restart" delay="10 sec"/>',
                '  <onfailure action="restart" delay="30 sec"/>',
                '  <onfailure action="none"/>',
                "  <startmode>Automatic</startmode>",
                "</service>",
            ]
        )

        return "\n".join(lines) + "\n"

    def _register_winsw_service(self, service_cfg: ServiceDefinitionConfig) -> None:
        """Register service using WinSW on Windows.

        Creates a WinSW XML config and service executable, then registers
        the service with Windows Service Control Manager.

        Args:
            service_cfg: Service configuration
        """
        winsw_path = find_winsw(self.config_dir)
        if not winsw_path:
            raise RuntimeError(
                "WinSW not found. Expected in bin/winsw.exe (walking up from "
                f"{self.config_dir}) or in PATH"
            )

        service_name = service_cfg.name

        # Resolve working directory
        working_dir = service_cfg.working_directory.replace(
            "{root}", str(self.config_dir)
        )

        try:
            # Copy winsw.exe as {service_name}.exe (WinSW naming convention)
            service_exe = self.config_dir / f"{service_name}.exe"
            shutil.copy2(winsw_path, service_exe)

            # Generate XML configuration
            xml_path = self.config_dir / f"{service_name}.xml"
            xml_content = self._generate_winsw_xml(service_cfg, working_dir)
            xml_path.write_text(xml_content, encoding="utf-8")

            # Generate wrapper config if self-update is enabled
            if self.config.self_update is not None:
                self._generate_runner_conf()

            # Create log directory
            log_dir = self.config_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            # Remove existing service if present (ignore errors)
            subprocess.run(
                [str(service_exe), "stop", "--no-elevate"],
                capture_output=True,
            )
            subprocess.run(
                [str(service_exe), "uninstall", "--no-elevate"],
                capture_output=True,
            )

            # Register the service
            result = subprocess.run(
                [str(service_exe), "install", "--no-elevate"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"WinSW install failed: {result.stderr}")

            logger.info(f"Registered Windows service: {service_name}")
            logger.info(f"  Start with: sc start {service_name}")
            logger.info(f"  Stop with: sc stop {service_name}")
            logger.info(f"  Status: sc query {service_name}")

        except Exception as e:
            logger.error(f"Failed to register WinSW service: {e}")
            raise

    def _generate_runner_conf(self) -> None:
        """Generate haniel-runner.conf for the wrapper script.

        This file provides the wrapper script with minimal configuration
        without requiring it to parse YAML. See ADR-0002.
        """
        assert self.config.self_update is not None

        repo_key = self.config.self_update.repo
        repo_config = self.config.repos.get(repo_key)
        repo_path = repo_config.path if repo_config else f"./.projects/{repo_key}"

        # Webhook URL (first configured webhook, if any)
        webhook_url = ""
        if self.config.webhooks:
            webhook_url = self.config.webhooks[0].url

        lines = [
            "# haniel-runner.conf - Generated by haniel install",
            "# Configuration for haniel-runner.ps1 wrapper script",
            f"WEBHOOK_URL={webhook_url}",
            f"HANIEL_REPO={repo_path}",
            f"CONFIG={self.config_filename}",
            "MAX_GIT_FAILURES=3",
        ]

        conf_path = self.config_dir / "haniel-runner.conf"
        conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Generated wrapper configuration: {conf_path}")

    def _log_service_instructions(self, service_cfg) -> None:
        """Log instructions for setting up service on non-Windows.

        Args:
            service_cfg: Service configuration
        """
        service_name = service_cfg.name
        working_dir = service_cfg.working_directory.replace(
            "{root}", str(self.config_dir)
        )

        logger.info("=== Service Setup Instructions ===")
        logger.info(f"Service name: {service_name}")
        logger.info(f"Working directory: {working_dir}")
        logger.info("")
        logger.info("To run manually:")
        logger.info(f"  cd {working_dir}")
        logger.info("  haniel run haniel.yaml")
        logger.info("")
        # Resolve environment variables
        env_lines = ""
        if service_cfg.environment:
            resolved_env = {
                k: v.replace("{root}", str(self.config_dir))
                for k, v in service_cfg.environment.items()
            }
            env_lines = "\n".join(
                f"Environment={k}={v}" for k, v in resolved_env.items()
            )
            env_lines = "\n" + env_lines

        logger.info("For systemd, create /etc/systemd/system/{service_name}.service:")
        logger.info(
            f"""
[Unit]
Description={service_cfg.display or service_name}
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart=/usr/bin/python -m haniel.cli run haniel.yaml{env_lines}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        )
        logger.info("")
        logger.info("Then run:")
        logger.info("  sudo systemctl daemon-reload")
        logger.info(f"  sudo systemctl enable {service_name}")
        logger.info(f"  sudo systemctl start {service_name}")

    def get_completion_summary(self) -> dict[str, Any]:
        """Get a summary of the completed installation.

        Returns:
            Summary dict
        """
        summary: dict[str, Any] = {
            "status": "complete",
            "config_dir": str(self.config_dir),
            "generated_files": [],
            "service": None,
        }

        # List generated config files
        if self.config.install and self.config.install.configs:
            for name, cfg in self.config.install.configs.items():
                config_path = self._resolve_path(cfg.path)
                if config_path.exists():
                    summary["generated_files"].append(
                        {
                            "name": name,
                            "path": str(config_path),
                        }
                    )

        # Service info
        if self.config.install and self.config.install.service:
            summary["service"] = {
                "name": self.config.install.service.name,
                "display": self.config.install.service.display,
            }

        return summary
