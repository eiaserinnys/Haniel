"""
haniel git module.

Provides functions for git operations: clone, fetch, pull, and change detection.
haniel doesn't care what it clones - it just executes git commands as specified.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

# Default timeout for git operations (5 minutes)
DEFAULT_GIT_TIMEOUT = 300

logger = logging.getLogger(__name__)


class GitError(Exception):
    """Base class for git-related errors."""

    pass


class GitCloneError(GitError):
    """Error during git clone operation.

    May indicate:
    - Invalid URL
    - SSH key not configured
    - Network issues
    - Permission denied
    - Destination path already exists
    """

    def __init__(
        self,
        message: str,
        url: str | None = None,
        stderr: str | None = None,
        returncode: int | None = None,
    ):
        self.url = url
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.url:
            parts.append(f"URL: {self.url}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr}")
        return " | ".join(parts)


class GitFetchError(GitError):
    """Error during git fetch operation.

    May indicate:
    - Network issues
    - Remote not configured
    - Permission denied
    """

    def __init__(
        self,
        message: str,
        path: Path | None = None,
        stderr: str | None = None,
        returncode: int | None = None,
    ):
        self.path = path
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.path:
            parts.append(f"Path: {self.path}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr}")
        return " | ".join(parts)


class GitPullError(GitError):
    """Error during git pull operation.

    May indicate:
    - Network issues
    - Merge conflicts
    - Remote not configured
    """

    def __init__(
        self,
        message: str,
        path: Path | None = None,
        stderr: str | None = None,
        returncode: int | None = None,
    ):
        self.path = path
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.path:
            parts.append(f"Path: {self.path}")
        if self.stderr:
            parts.append(f"stderr: {self.stderr}")
        return " | ".join(parts)


class GitTimeoutError(GitError):
    """Error when git operation times out."""

    def __init__(self, message: str, timeout: int):
        self.timeout = timeout
        super().__init__(message)


def _validate_git_url(url: str) -> None:
    """Validate git URL for security.

    Blocks potentially malicious URL patterns:
    - ext:: protocol (can execute arbitrary commands)
    - URLs with embedded credentials (security risk if logged)

    Args:
        url: Git URL to validate

    Raises:
        ValueError: If URL appears malicious
    """
    # Block ext:: protocol (can execute arbitrary commands)
    if url.startswith("ext::"):
        raise ValueError(f"Unsupported git protocol in URL: {url}")

    # Block URLs with embedded credentials (security risk if logged)
    if re.match(r"https?://[^/:]+:[^@]+@", url):
        raise ValueError("URLs with embedded credentials are not allowed")


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = DEFAULT_GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Args:
        args: Git command arguments (without 'git' prefix)
        cwd: Working directory for the command
        check: If True, raise CalledProcessError on non-zero exit
        timeout: Timeout in seconds (default: 300)

    Returns:
        CompletedProcess with captured stdout and stderr

    Raises:
        subprocess.TimeoutExpired: If command times out
    """
    cmd = ["git"] + args

    # Prevent git from prompting for credentials (would hang automated processes)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        timeout=timeout,
        env=env,
    )


def get_head(path: Path) -> str:
    """Get the current HEAD commit hash.

    Args:
        path: Path to the git repository

    Returns:
        40-character commit hash

    Raises:
        GitError: If the path is not a git repository or doesn't exist
    """
    if not path.exists():
        raise GitError(f"Path does not exist: {path}")

    try:
        result = _run_git(["rev-parse", "HEAD"], cwd=path)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise GitError(f"Failed to get HEAD for {path}: {e.stderr.strip()}") from e


def get_remote_head(path: Path, branch: str, remote: str = "origin") -> str:
    """Get the remote branch's HEAD commit hash.

    Requires a prior fetch to have accurate remote refs.

    Args:
        path: Path to the git repository
        branch: Branch name to check
        remote: Remote name (default: origin)

    Returns:
        40-character commit hash

    Raises:
        GitError: If the remote branch doesn't exist or path is not a git repo
    """
    if not path.exists():
        raise GitError(f"Path does not exist: {path}")

    try:
        result = _run_git(["rev-parse", f"{remote}/{branch}"], cwd=path)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise GitError(
            f"Failed to get remote HEAD for {remote}/{branch} in {path}: {e.stderr.strip()}"
        ) from e


def clone_repo(
    url: str, branch: str, path: Path, timeout: int = DEFAULT_GIT_TIMEOUT
) -> None:
    """Clone a repository to the specified path.

    Args:
        url: Git clone URL (HTTPS or SSH)
        branch: Branch to clone
        path: Destination path for the clone
        timeout: Timeout in seconds (default: 300)

    Raises:
        GitCloneError: If clone fails (invalid URL, network issue, etc.)
                       or if the destination path already exists
        ValueError: If URL is malicious
        GitTimeoutError: If operation times out
    """
    # Validate URL for security
    _validate_git_url(url)

    if path.exists():
        raise GitCloneError(
            f"Destination path already exists: {path}",
            url=url,
        )

    try:
        _run_git(["clone", "--branch", branch, url, str(path)], timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GitTimeoutError(
            f"Clone operation timed out after {timeout}s",
            timeout=timeout,
        ) from e
    except subprocess.CalledProcessError as e:
        raise GitCloneError(
            "Failed to clone repository",
            url=url,
            stderr=e.stderr.strip() if e.stderr else None,
            returncode=e.returncode,
        ) from e


def fetch_repo(path: Path, branch: str, remote: str = "origin") -> bool:
    """Fetch updates from remote and check if there are changes.

    Args:
        path: Path to the git repository
        branch: Branch to fetch
        remote: Remote name (default: origin)

    Returns:
        True if there are new commits to pull, False otherwise

    Raises:
        GitFetchError: If fetch fails (network issue, no remote, etc.)
    """
    if not path.exists():
        raise GitFetchError(
            f"Path does not exist: {path}",
            path=path,
        )

    if not (path / ".git").is_dir():
        raise GitFetchError(
            f"Not a git repository: {path}",
            path=path,
        )

    # Get local HEAD before fetch
    try:
        local_head = get_head(path)
    except GitError as e:
        raise GitFetchError(
            f"Failed to get local HEAD: {e}",
            path=path,
        ) from e

    # Fetch from remote
    try:
        _run_git(["fetch", remote, branch], cwd=path)
    except subprocess.CalledProcessError as e:
        raise GitFetchError(
            f"Failed to fetch from {remote}/{branch}",
            path=path,
            stderr=e.stderr.strip(),
            returncode=e.returncode,
        ) from e

    # Get remote HEAD after fetch
    try:
        remote_head = get_remote_head(path, branch, remote)
    except GitError as e:
        raise GitFetchError(
            f"Failed to get remote HEAD: {e}",
            path=path,
        ) from e

    return local_head != remote_head


def pull_repo(
    path: Path,
    branch: str,
    remote: str = "origin",
    strategy: str = "merge",
) -> list[str]:
    """Pull updates from remote.

    Args:
        path: Path to the git repository
        branch: Branch to pull
        remote: Remote name (default: origin)
        strategy: Pull strategy. 'force' uses git fetch + reset --hard to discard
            local changes. 'merge' (default) uses git pull.

    Returns:
        list[str]: force 전략일 때 드롭된 파일 목록 (git status --short 형식). 그 외 빈 리스트.

    Raises:
        GitPullError: If pull fails (network issue, merge conflict, etc.)
    """
    if not path.exists():
        raise GitPullError(
            f"Path does not exist: {path}",
            path=path,
        )

    if not (path / ".git").is_dir():
        raise GitPullError(
            f"Not a git repository: {path}",
            path=path,
        )

    if strategy == "force":
        try:
            status = _run_git(["status", "--short"], cwd=path)
            discarded = [
                line.strip()
                for line in status.stdout.strip().splitlines()
                if line.strip()
            ]
            if discarded:
                logger.warning(
                    "Force pull: discarding %d local change(s) in %s: %s",
                    len(discarded),
                    path.name,
                    discarded,
                )
            _run_git(["fetch", remote], cwd=path)
            _run_git(["reset", "--hard", f"{remote}/{branch}"], cwd=path)
        except subprocess.CalledProcessError as e:
            raise GitPullError(
                f"Failed to force-pull from {remote}/{branch}",
                path=path,
                stderr=e.stderr.strip(),
                returncode=e.returncode,
            ) from e
        return discarded
    else:
        try:
            _run_git(["pull", remote, branch], cwd=path)
        except subprocess.CalledProcessError as e:
            raise GitPullError(
                f"Failed to pull from {remote}/{branch}",
                path=path,
                stderr=e.stderr.strip(),
                returncode=e.returncode,
            ) from e
        return []


def get_pending_changes(path: Path, branch: str, remote: str = "origin") -> dict:
    """Get details of pending changes between local and remote.

    Returns commits and file summary for changes that exist on the remote
    but have not been applied locally. Requires a prior fetch.

    Args:
        path: Path to the git repository
        branch: Branch to check
        remote: Remote name (default: origin)

    Returns:
        Dict with:
            - commits: list of strings, each "hash subject" (one-line log)
            - stat: diff stat summary string, or None if no changes
    """
    try:
        log_result = _run_git(
            ["log", "--oneline", f"HEAD..{remote}/{branch}"],
            cwd=path,
            check=False,
        )
        commits = [line for line in log_result.stdout.strip().splitlines() if line]

        if not commits:
            return {"commits": [], "stat": None}

        stat_result = _run_git(
            ["diff", "--stat", f"HEAD..{remote}/{branch}"],
            cwd=path,
            check=False,
        )
        stat = stat_result.stdout.strip() or None
        return {"commits": commits, "stat": stat}
    except Exception:
        return {"commits": [], "stat": None}


def has_changes(path: Path, branch: str, remote: str = "origin") -> bool:
    """Check if there are changes between local and remote.

    Compares local HEAD with remote HEAD. Requires a prior fetch
    to have accurate remote refs.

    Args:
        path: Path to the git repository
        branch: Branch to check
        remote: Remote name (default: origin)

    Returns:
        True if local HEAD differs from remote HEAD

    Raises:
        GitError: If comparison fails
    """
    local_head = get_head(path)
    remote_head = get_remote_head(path, branch, remote)
    return local_head != remote_head
