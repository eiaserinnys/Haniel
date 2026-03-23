"""
Mechanical installer - Phase 1.

Handles automated installation steps that don't require user interaction:
- System requirements verification
- Directory creation
- Git repository cloning
- Virtual environment setup
- Static config file generation

haniel doesn't care what it's installing - it just executes the steps.
"""

import json
import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import HanielConfig
from .state import InstallState
import os

from .utils import detect_tool_paths, find_winsw

logger = logging.getLogger(__name__)


class MechanicalInstaller:
    """Handles mechanical installation steps."""

    def __init__(
        self,
        config: HanielConfig,
        config_dir: Path,
        state: InstallState,
    ):
        """Initialize the mechanical installer.

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

    def _check_version(self, actual: str, required: str) -> tuple[bool, str]:
        """Check if a version meets the requirement.

        Args:
            actual: Actual version string (e.g., "3.11.5")
            required: Required version string (e.g., ">=3.11")

        Returns:
            Tuple of (passes, message)
        """
        # Parse requirement
        match = re.match(r"(>=|>|==|<=|<)?(.+)", required)
        if not match:
            return False, f"Invalid version requirement: {required}"

        op = match.group(1) or ">="
        req_version = match.group(2)

        # Parse versions into tuples
        try:
            actual_parts = tuple(int(x) for x in actual.split(".")[:3])
            req_parts = tuple(int(x) for x in req_version.split(".")[:3])
        except ValueError:
            return False, f"Cannot parse version: {actual}"

        # Compare
        if op == ">=":
            passes = actual_parts >= req_parts
        elif op == ">":
            passes = actual_parts > req_parts
        elif op == "==":
            passes = actual_parts == req_parts
        elif op == "<=":
            passes = actual_parts <= req_parts
        elif op == "<":
            passes = actual_parts < req_parts
        else:
            passes = False

        msg = f"{actual} {'meets' if passes else 'does not meet'} {required}"
        return passes, msg

    def check_requirements(self) -> list[dict[str, Any]]:
        """Check system requirements.

        Returns:
            List of requirement check results
        """
        results: list[dict[str, Any]] = []

        if not self.config.install or not self.config.install.requirements:
            return results

        requirements = self.config.install.requirements

        # Check Python
        if "python" in requirements:
            try:
                result = subprocess.run(
                    ["python", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                version = result.stdout.strip().replace("Python ", "")
                passes, msg = self._check_version(version, str(requirements["python"]))
                results.append(
                    {
                        "name": "python",
                        "installed": passes,
                        "version": version,
                        "required": str(requirements["python"]),
                        "message": msg,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "name": "python",
                        "installed": False,
                        "error": str(e),
                    }
                )

        # Check Node.js
        if "node" in requirements:
            try:
                result = subprocess.run(
                    ["node", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                version = result.stdout.strip().lstrip("v")
                passes, msg = self._check_version(version, str(requirements["node"]))
                error_hint = (
                    ""
                    if passes
                    else " Install newer version via: winget install OpenJS.NodeJS.LTS (or re-run install-haniel.ps1)"
                )
                results.append(
                    {
                        "name": "node",
                        "installed": passes,
                        "version": version,
                        "required": str(requirements["node"]),
                        "message": msg + error_hint,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "name": "node",
                        "installed": False,
                        "error": f"Node.js not found. Install via: winget install OpenJS.NodeJS.LTS (or re-run install-haniel.ps1). Detail: {e}",
                    }
                )

        # Check WinSW (Windows only)
        if "winsw" in requirements and requirements["winsw"]:
            if platform.system() == "Windows":
                winsw_path = find_winsw(self.config_dir)
                results.append(
                    {
                        "name": "winsw",
                        "installed": winsw_path is not None,
                        "path": str(winsw_path) if winsw_path else None,
                        "error": None
                        if winsw_path
                        else "WinSW not found in bin/ or PATH",
                    }
                )
            else:
                # WinSW is Windows-only, skip on other platforms
                results.append(
                    {
                        "name": "winsw",
                        "installed": True,
                        "message": "WinSW check skipped (not Windows)",
                    }
                )

        # Check Claude Code
        if "claude-code" in requirements and requirements["claude-code"]:
            claude_path = shutil.which("claude")
            results.append(
                {
                    "name": "claude-code",
                    "installed": claude_path is not None,
                    "path": claude_path,
                    "error": None if claude_path else "Claude Code not found in PATH",
                }
            )

        return results

    def create_directories(self) -> None:
        """Create required directories."""
        if not self.config.install or not self.config.install.directories:
            self.state.mark_complete("directories")
            return

        for dir_path in self.config.install.directories:
            full_path = self._resolve_path(dir_path)
            if not full_path.exists():
                full_path.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created directory: {full_path}")
            else:
                logger.debug(f"Directory already exists: {full_path}")

        self.state.mark_complete("directories")

    def clone_repos(self) -> None:
        """Clone git repositories."""
        if not self.config.repos:
            self.state.mark_complete("repos")
            return

        all_success = True
        for name, repo in self.config.repos.items():
            repo_path = self._resolve_path(repo.path)

            if repo_path.exists():
                # Check if it's a valid git repo
                git_dir = repo_path / ".git"
                if git_dir.exists():
                    logger.info(
                        f"Repository already exists: {name} at {repo_path}, pulling latest"
                    )
                    try:
                        result = subprocess.run(
                            ["git", "-C", str(repo_path), "pull", "--ff-only"],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if result.returncode != 0:
                            error_msg = result.stderr.strip() or result.stdout.strip()
                            logger.warning(
                                f"git pull --ff-only failed for {name}: {error_msg}"
                            )
                        else:
                            logger.info(
                                f"Pulled latest for {name}: {result.stdout.strip()}"
                            )
                            if repo.hooks and repo.hooks.post_pull:
                                self._run_repo_hook(name, "post_pull", repo.hooks.post_pull, repo_path)
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            f"git pull timed out for {name}, continuing with existing code"
                        )
                    except Exception as e:
                        logger.warning(
                            f"git pull failed for {name}: {e}, continuing with existing code"
                        )
                    continue
                else:
                    logger.warning(
                        f"Directory exists but is not a git repo: {repo_path}"
                    )
                    self.state.mark_failed(
                        f"repos:{name}",
                        f"Directory exists but is not a git repo: {repo_path}",
                    )
                    all_success = False
                    continue

            # Clone the repo
            try:
                logger.info(f"Cloning {name} from {repo.url} to {repo_path}")
                result = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--branch",
                        repo.branch,
                        repo.url,
                        str(repo_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    error_msg = result.stderr.strip() or result.stdout.strip()
                    logger.error(f"Failed to clone {name}: {error_msg}")
                    self.state.mark_failed(f"repos:{name}", error_msg)
                    all_success = False
                else:
                    logger.info(f"Successfully cloned {name}")
                    if repo.hooks and repo.hooks.post_pull:
                        self._run_repo_hook(name, "post_pull", repo.hooks.post_pull, repo_path)
            except subprocess.TimeoutExpired:
                self.state.mark_failed(f"repos:{name}", "Clone timed out")
                all_success = False
            except Exception as e:
                self.state.mark_failed(f"repos:{name}", str(e))
                all_success = False

        if all_success:
            self.state.mark_complete("repos")

    def create_environments(self) -> None:
        """Create virtual environments and install dependencies."""
        if not self.config.install or not self.config.install.environments:
            self.state.mark_complete("environments")
            return

        all_success = True
        for name, env in self.config.install.environments.items():
            env_path = self._resolve_path(env.path)

            if env.type == "python-venv":
                success = self._create_python_venv(name, env_path, env.requirements)
                if not success:
                    all_success = False

            elif env.type == "npm":
                success = self._run_npm_install(name, env_path)
                if success and env.build:
                    success = self._run_build(name, env_path, env.build)
                if not success:
                    all_success = False

            elif env.type == "pnpm":
                success = self._run_pnpm_install(name, env_path)
                if success and env.build:
                    success = self._run_build(name, env_path, env.build)
                if not success:
                    all_success = False

            else:
                logger.warning(f"Unknown environment type: {env.type}")
                self.state.mark_failed(
                    f"environments:{name}",
                    f"Unknown environment type: {env.type}",
                )
                all_success = False

        if all_success:
            self.state.mark_complete("environments")

    def _create_python_venv(
        self,
        name: str,
        env_path: Path,
        requirements: list[str] | None,
    ) -> bool:
        """Create a Python virtual environment.

        Args:
            name: Environment name
            env_path: Path to create venv
            requirements: List of requirements files

        Returns:
            True if successful
        """
        try:
            if not env_path.exists():
                logger.info(f"Creating venv: {name} at {env_path}")
                subprocess.run(
                    ["python", "-m", "venv", str(env_path)],
                    check=True,
                    timeout=60,
                )

            # Determine python/pip paths
            if platform.system() == "Windows":
                python_path = env_path / "Scripts" / "python.exe"
                pip_path = env_path / "Scripts" / "pip.exe"
            else:
                python_path = env_path / "bin" / "python"
                pip_path = env_path / "bin" / "pip"

            # Always upgrade pip and setuptools before installing requirements.
            # Python 3.13's bundled setuptools may lack setuptools.backends.legacy:build,
            # and a previous failed install may have left a venv with outdated tools.
            # Use `python -m pip` instead of pip.exe — on Windows, pip.exe cannot
            # upgrade itself (the executable is locked while in use).
            # Install/upgrade build tools before installing requirements.
            # --no-build-isolation (used for editable installs below) means pip
            # will use these tools directly from the venv instead of creating an
            # isolated build environment. All common build backends must be present.
            logger.info(f"Upgrading build tools in venv: {name}")
            subprocess.run(
                [
                    str(python_path),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "pip",
                    "setuptools",
                    "hatchling",
                    "editables",
                ],
                check=True,
                timeout=120,
            )

            # Install requirements if provided
            if requirements:
                for req_file in requirements:
                    req_path = self._resolve_path(req_file)
                    if req_path.exists():
                        # pyproject.toml requires editable install, not -r
                        if req_path.name in ("pyproject.toml", "setup.py", "setup.cfg"):
                            logger.info(
                                f"Installing editable package from {req_path.parent}"
                            )
                            cmd = [
                                str(pip_path),
                                "install",
                                "-e",
                                str(req_path.parent),
                                "--no-build-isolation",
                            ]
                        else:
                            logger.info(f"Installing requirements from {req_path}")
                            cmd = [str(pip_path), "install", "-r", str(req_path)]
                        subprocess.run(cmd, check=True, timeout=300)
                    else:
                        logger.warning(f"Requirements file not found: {req_path}")

            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create venv {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False
        except Exception as e:
            logger.error(f"Error creating venv {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False

    def _run_npm_install(self, name: str, env_path: Path) -> bool:
        """Run npm install in a directory.

        Args:
            name: Environment name
            env_path: Path containing package.json

        Returns:
            True if successful
        """
        try:
            package_json = env_path / "package.json"
            if not package_json.exists():
                logger.warning(f"No package.json found at {env_path}")
                return True  # Not an error

            logger.info(f"Running npm install in {env_path}")
            env = self._env_with_tool_paths(["node", "npm", "npx"])
            subprocess.run(
                ["npm", "install"],
                cwd=str(env_path),
                check=True,
                timeout=300,
                env=env,
                shell=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"npm install failed for {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False
        except Exception as e:
            logger.error(f"Error running npm install for {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False

    def _run_pnpm_install(self, name: str, env_path: Path) -> bool:
        """Run pnpm install in a directory.

        Args:
            name: Environment name
            env_path: Path containing package.json

        Returns:
            True if successful
        """
        try:
            package_json = env_path / "package.json"
            if not package_json.exists():
                logger.warning(f"No package.json found at {env_path}")
                return True  # Not an error

            logger.info(f"Running pnpm install in {env_path}")
            env = self._env_with_tool_paths(["node", "pnpm", "npx"])
            subprocess.run(
                ["pnpm", "install"],
                cwd=str(env_path),
                check=True,
                timeout=300,
                env=env,
                shell=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"pnpm install failed for {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False
        except FileNotFoundError:
            logger.error("pnpm not found. Install it with: npm install -g pnpm")
            self.state.mark_failed(f"environments:{name}", "pnpm not found in PATH")
            return False
        except Exception as e:
            logger.error(f"Error running pnpm install for {name}: {e}")
            self.state.mark_failed(f"environments:{name}", str(e))
            return False

    def _run_repo_hook(self, name: str, hook_name: str, cmd: str, cwd: Path) -> None:
        """Run a lifecycle hook for a repository."""
        try:
            logger.info(f"Running {hook_name} hook for repo {name}: {cmd}")
            env = self._env_with_tool_paths(["node", "pnpm", "npm", "npx"])
            subprocess.run(
                cmd,
                cwd=str(cwd),
                check=True,
                timeout=300,
                env=env,
                shell=True,
            )
            logger.info(f"{hook_name} hook succeeded for repo {name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"{hook_name} hook failed for repo {name}: {e}")
            self.state.mark_failed(f"repos:{name}:{hook_name}", str(e))
        except Exception as e:
            logger.error(f"{hook_name} hook error for repo {name}: {e}")
            self.state.mark_failed(f"repos:{name}:{hook_name}", str(e))

    def _run_build(self, name: str, env_path: Path, build_cmd: str) -> bool:
        """Run a build command in a directory.

        Args:
            name: Environment name
            env_path: Working directory for the build
            build_cmd: Build command string (e.g. "pnpm run build")

        Returns:
            True if successful
        """
        try:
            logger.info(f"Running build for {name}: {build_cmd}")
            env = self._env_with_tool_paths(["node", "pnpm", "npx"])
            subprocess.run(
                build_cmd,
                cwd=str(env_path),
                check=True,
                timeout=300,
                env=env,
                shell=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Build failed for {name}: {e}")
            self.state.mark_failed(f"environments:{name}:build", str(e))
            return False
        except Exception as e:
            logger.error(f"Build error for {name}: {e}")
            self.state.mark_failed(f"environments:{name}:build", str(e))
            return False

    def _env_with_tool_paths(self, commands: list[str]) -> dict[str, str]:
        """Create env dict with tool paths injected into PATH.

        Ensures tools like node, pnpm, npx are findable even when running
        under an administrator PowerShell that lacks the user's PATH entries.
        """
        env = os.environ.copy()
        tool_paths = detect_tool_paths(commands)
        if tool_paths:
            env["PATH"] = env.get("PATH", "") + ";" + ";".join(tool_paths)
        return env

    def create_static_configs(self) -> None:
        """Create static config files (those with 'content' field)."""
        if not self.config.install or not self.config.install.configs:
            return

        for name, cfg in self.config.install.configs.items():
            if cfg.content:
                config_path = self._resolve_path(cfg.path)
                config_path.parent.mkdir(parents=True, exist_ok=True)

                # Substitute {root} with config_dir (forward slashes for portability,
                # avoids breaking JSON/YAML with backslash escapes on Windows)
                root_str = self.config_dir.as_posix()
                content = cfg.content.replace("{root}", root_str)

                config_path.write_text(content, encoding="utf-8")
                logger.info(f"Created static config: {config_path}")

    def determine_pending_configs(self) -> None:
        """Determine which config keys are pending user input."""
        if not self.config.install or not self.config.install.configs:
            return

        for name, cfg in self.config.install.configs.items():
            if cfg.keys:
                # Check which keys are missing
                existing_values = self.state.config_values.get(name, {})

                # Load existing file if it exists
                config_path = self._resolve_path(cfg.path)
                if config_path.exists():
                    existing_values = self._load_existing_config(config_path, cfg.keys)
                    # Merge into state
                    if name not in self.state.config_values:
                        self.state.config_values[name] = {}
                    self.state.config_values[name].update(existing_values)

                # Find missing keys (no default and not filled)
                missing = []
                for key_cfg in cfg.keys:
                    if key_cfg.key not in self.state.config_values.get(name, {}):
                        if key_cfg.default:
                            # Use default value
                            self.state.set_config_value(
                                name, key_cfg.key, key_cfg.default
                            )
                        else:
                            missing.append(key_cfg.key)

                if missing:
                    self.state.pending_configs[name] = missing

    def _load_existing_config(
        self,
        config_path: Path,
        keys: list,
    ) -> dict[str, str]:
        """Load existing values from a config file.

        Args:
            config_path: Path to config file
            keys: Expected keys

        Returns:
            Dict of key -> value for existing keys
        """
        result: dict[str, str] = {}

        if not config_path.exists():
            return result

        # Determine file type by extension
        suffix = config_path.suffix.lower()

        if suffix == ".env":
            # Parse .env file
            content = config_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    result[key.strip()] = value.strip()

        elif suffix == ".json":
            # Parse JSON file
            try:
                content = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    for key_cfg in keys:
                        if key_cfg.key in content:
                            result[key_cfg.key] = str(content[key_cfg.key])
            except json.JSONDecodeError:
                pass

        return result
