"""
Finalizer - Phase 3.

Handles the final installation steps:
- Generate config files from collected values
- Register system service (NSSM on Windows)

haniel doesn't care what the configs contain - it just writes files.
"""

import json
import logging
import platform
import subprocess
from pathlib import Path
from typing import Any

from ..config import HanielConfig
from .state import InstallState

logger = logging.getLogger(__name__)


class Finalizer:
    """Handles finalization of installation."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
    ):
        """Initialize the finalizer.

        Args:
            config: Haniel configuration
            config_dir: Directory containing haniel.yaml
            state: Installation state
        """
        self.config = config
        self.config_dir = config_dir
        self.state = state

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
                        logger.warning(
                            f"Missing required config: {name}.{key_cfg.key}"
                        )
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

        On Windows, uses NSSM. On other platforms, logs instructions.
        """
        if not self.config.install or not self.config.install.service:
            logger.info("No service configuration, skipping registration")
            return

        service_cfg = self.config.install.service

        if platform.system() == "Windows":
            self._register_nssm_service(service_cfg)
        else:
            self._log_service_instructions(service_cfg)

    def _register_nssm_service(self, service_cfg) -> None:
        """Register service using NSSM on Windows.

        Args:
            service_cfg: Service configuration
        """
        import shutil

        nssm_path = shutil.which("nssm")
        if not nssm_path:
            raise RuntimeError("NSSM not found in PATH")

        service_name = service_cfg.name

        # Resolve working directory
        working_dir = service_cfg.working_directory.replace(
            "{root}", str(self.config_dir)
        )

        # Get Python executable
        python_path = shutil.which("python")
        if not python_path:
            raise RuntimeError("Python not found in PATH")

        try:
            # Remove existing service if present
            subprocess.run(
                [nssm_path, "remove", service_name, "confirm"],
                capture_output=True,
            )

            # Install the service
            # haniel run haniel.yaml
            result = subprocess.run(
                [
                    nssm_path,
                    "install",
                    service_name,
                    python_path,
                    "-m",
                    "haniel.cli",
                    "run",
                    "haniel.yaml",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"NSSM install failed: {result.stderr}")

            # Set display name
            if service_cfg.display:
                subprocess.run(
                    [nssm_path, "set", service_name, "DisplayName", service_cfg.display],
                    capture_output=True,
                )

            # Set working directory
            subprocess.run(
                [nssm_path, "set", service_name, "AppDirectory", working_dir],
                capture_output=True,
            )

            # Set environment variables
            if service_cfg.environment:
                env_str = " ".join(
                    f"{k}={v}" for k, v in service_cfg.environment.items()
                )
                subprocess.run(
                    [nssm_path, "set", service_name, "AppEnvironmentExtra", env_str],
                    capture_output=True,
                )

            # Set stdout/stderr logging
            log_dir = Path(working_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                [
                    nssm_path,
                    "set",
                    service_name,
                    "AppStdout",
                    str(log_dir / f"{service_name}.log"),
                ],
                capture_output=True,
            )
            subprocess.run(
                [
                    nssm_path,
                    "set",
                    service_name,
                    "AppStderr",
                    str(log_dir / f"{service_name}.err.log"),
                ],
                capture_output=True,
            )

            logger.info(f"Registered NSSM service: {service_name}")
            logger.info(f"  Start with: nssm start {service_name}")
            logger.info(f"  Stop with: nssm stop {service_name}")
            logger.info(f"  Status: nssm status {service_name}")

        except Exception as e:
            logger.error(f"Failed to register NSSM service: {e}")
            raise

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
        logger.info(f"  haniel run haniel.yaml")
        logger.info("")
        logger.info("For systemd, create /etc/systemd/system/{service_name}.service:")
        logger.info(
            f"""
[Unit]
Description={service_cfg.display or service_name}
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart=/usr/bin/python -m haniel.cli run haniel.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        )
        logger.info("")
        logger.info("Then run:")
        logger.info(f"  sudo systemctl daemon-reload")
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
                    summary["generated_files"].append({
                        "name": name,
                        "path": str(config_path),
                    })

        # Service info
        if self.config.install and self.config.install.service:
            summary["service"] = {
                "name": self.config.install.service.name,
                "display": self.config.install.service.display,
            }

        return summary
