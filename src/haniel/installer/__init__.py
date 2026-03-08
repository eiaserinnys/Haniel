"""
haniel installer module.

Handles the installation of haniel-managed services:
- Phase 1: Mechanical installation (directories, git clone, venv, npm)
- Phase 2: Interactive setup via Claude Code (secrets, config selection)
- Phase 3: Finalization (config file generation, WinSW service registration)

haniel doesn't care what it installs - it just follows the config and
delegates complex decisions to Claude Code.
"""

from .state import InstallState, InstallPhase, StepStatus
from .orchestrator import InstallOrchestrator
from .mechanical import MechanicalInstaller
from .interactive import InteractiveInstaller
from .finalize import Finalizer
from .install_mcp_server import InstallMcpServer

__all__ = [
    "InstallState",
    "InstallPhase",
    "StepStatus",
    "InstallOrchestrator",
    "MechanicalInstaller",
    "InteractiveInstaller",
    "Finalizer",
    "InstallMcpServer",
]
