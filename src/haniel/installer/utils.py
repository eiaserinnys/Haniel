"""
Installer utility functions.

Shared helpers used across installer phases.
"""

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _darwin_tool_directories() -> list[Path]:
    """Return the Darwin directories that commonly hold installer tools."""
    directories = [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    home = os.environ.get("HOME")
    if home:
        directories.append(Path(home) / ".local" / "bin")
        directories.append(Path(home) / ".npm-global" / "bin")
    directories.extend(
        [Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin")]
    )
    return directories


def get_tool_search_path() -> str:
    """Return the PATH used to resolve installation tools.

    A LaunchAgent or GUI installer on macOS starts from a sparse PATH that
    lacks the Homebrew and user tool directories, so the same candidate
    directories as the runner are appended there when they exist. Other
    platforms keep the inherited PATH unchanged.

    Returns:
        PATH string with existing Darwin tool directories appended
    """
    current = os.environ.get("PATH", "")
    if sys.platform != "darwin":
        return current

    entries = [entry for entry in current.split(os.pathsep) if entry]
    seen = set(entries)
    for directory in _darwin_tool_directories():
        entry = str(directory)
        if entry in seen or not directory.is_dir():
            continue
        entries.append(entry)
        seen.add(entry)
    return os.pathsep.join(entries)


def find_tool(command: str) -> str | None:
    """Find an executable using the installation tool search path."""
    return shutil.which(command, path=get_tool_search_path())


def get_tool_environment() -> dict[str, str]:
    """Return a subprocess environment that can resolve installation tools.

    On Darwin the PATH is replaced with the tool search path so Homebrew
    binaries are findable. The process environment itself is not modified.
    """
    environment = os.environ.copy()
    if sys.platform == "darwin":
        environment["PATH"] = get_tool_search_path()
    return environment


def find_winsw(config_dir: Path) -> Path | None:
    """Find the WinSW executable.

    Walks up from config_dir looking for bin/winsw.exe, then falls back
    to PATH. This handles the standard layout where winsw.exe lives in
    the haniel install root's bin/ directory, regardless of how deeply
    nested the service config directory is.

    Args:
        config_dir: The service configuration directory

    Returns:
        Path to winsw.exe, or None if not found
    """
    current = config_dir.resolve()
    logger.debug(f"Searching for WinSW starting from: {current}")
    for _ in range(5):
        candidate = current / "bin" / "winsw.exe"
        logger.debug(f"  Checking: {candidate} (exists={candidate.exists()})")
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break  # Reached filesystem root
        current = parent

    found = shutil.which("winsw")
    if found:
        logger.debug(f"  Found in PATH: {found}")
        return Path(found)

    logger.debug("  WinSW not found anywhere")
    return None


def detect_tool_paths(commands: list[str]) -> list[str]:
    """Detect directories containing specified executables.

    Used to find Node.js, pnpm, npx etc. for PATH injection into
    WinSW service environment and subprocess calls.

    Args:
        commands: List of command names to search for (e.g. ["node", "pnpm", "npx"])

    Returns:
        List of unique directory paths containing the found executables
    """
    paths: list[str] = []
    for cmd in commands:
        found = find_tool(cmd)
        if found:
            parent = str(Path(found).absolute().parent)
            if parent not in paths:
                paths.append(parent)
    return paths
