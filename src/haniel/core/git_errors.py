"""Typed failures raised by Haniel git operations."""

from __future__ import annotations

from pathlib import Path


class GitError(Exception):
    """Base class for git-related errors."""


class GitCloneError(GitError):
    """A clone failed before the destination became usable."""

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
    """A fetch failed before remote state could be observed."""

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
    """A pull or checkout activation failed."""

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
    """A git subprocess exceeded its configured deadline."""

    def __init__(self, message: str, timeout: int):
        self.timeout = timeout
        super().__init__(message)
