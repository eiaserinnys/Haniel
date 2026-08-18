"""Filesystem primitives shared by Haniel atomic release helpers."""

from __future__ import annotations

import errno
import json
import ntpath
import os
import re
import stat
import uuid
from pathlib import Path


CURRENT_POINTER = "current.txt"
LEGACY_CURRENT_POINTER = "current"
RELEASE_DIRECTORY_PATTERN = re.compile(
    r"^(?P<commit>[0-9a-f]{12,64})(?:\.retry-[0-9a-f]{8})?$"
)


class ReleaseFilesystemError(RuntimeError):
    """The release filesystem does not satisfy the activation contract."""


def normalized_os_path(
    path: str | os.PathLike[str],
    *,
    platform: str | None = None,
) -> str:
    """Return an absolute OS path, using Windows extended-length syntax."""
    selected_platform = os.name if platform is None else platform
    raw = os.fspath(path)
    if selected_platform != "nt":
        return os.path.abspath(raw)

    absolute = ntpath.abspath(raw)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def release_directory_matches_commit(name: str, commit: str) -> bool:
    """Return whether a canonical or retry directory belongs to a commit."""
    match = RELEASE_DIRECTORY_PATTERN.fullmatch(name)
    return match is not None and commit.startswith(match.group("commit"))


def _is_reparse_leaf(path: str) -> bool:
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def is_reparse_leaf(path: str | os.PathLike[str]) -> bool:
    """Return whether path is a symlink, junction, or other reparse leaf."""
    return _is_reparse_leaf(normalized_os_path(path))


def _make_writable(path: str) -> None:
    mode = os.lstat(path).st_mode
    os.chmod(path, mode | stat.S_IWRITE)


def _retry_readonly(action, path: str) -> None:
    try:
        action(path)
    except PermissionError:
        _make_writable(path)
        action(path)


def remove_reparse_leaf(path: str | os.PathLike[str]) -> None:
    """Remove a directory reparse point or symlink without following it."""
    native = normalized_os_path(path)
    try:
        _retry_readonly(os.rmdir, native)
    except OSError as exc:
        if exc.errno not in {errno.ENOTDIR, errno.EINVAL}:
            raise
        _retry_readonly(os.unlink, native)


def _remove_tree_entry(path: str) -> None:
    if _is_reparse_leaf(path):
        remove_reparse_leaf(path)
        return

    metadata = os.lstat(path)
    if stat.S_ISDIR(metadata.st_mode):
        with os.scandir(path) as entries:
            children = [entry.path for entry in entries]
        for child in children:
            _remove_tree_entry(child)
        _retry_readonly(os.rmdir, path)
        return

    _retry_readonly(os.unlink, path)


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Delete a tree with Windows long-path and reparse-point safety."""
    native = normalized_os_path(path)
    if not os.path.lexists(native):
        return
    _remove_tree_entry(native)


def _validate_release_name(name: str) -> str:
    if RELEASE_DIRECTORY_PATTERN.fullmatch(name) is None:
        raise ReleaseFilesystemError(f"invalid release pointer value: {name!r}")
    return name


def read_release_pointer(release_root: Path) -> str | None:
    """Read and validate the active release directory name."""
    pointer = release_root / CURRENT_POINTER
    if not os.path.lexists(normalized_os_path(pointer)):
        return None
    if pointer.is_symlink() or not pointer.is_file():
        raise ReleaseFilesystemError(f"release pointer is not a regular file: {pointer}")
    try:
        raw = pointer.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseFilesystemError(f"cannot read release pointer: {exc}") from exc
    lines = raw.splitlines()
    if len(lines) != 1 or raw not in {lines[0], lines[0] + "\n"}:
        raise ReleaseFilesystemError("release pointer must contain exactly one line")
    return _validate_release_name(lines[0])


def write_release_pointer(release_root: Path, release_name: str) -> None:
    """Atomically replace the active release pointer file."""
    _validate_release_name(release_name)
    release_root.mkdir(parents=True, exist_ok=True)
    pointer = release_root / CURRENT_POINTER
    if os.path.lexists(normalized_os_path(pointer)) and (
        pointer.is_symlink() or not pointer.is_file()
    ):
        raise ReleaseFilesystemError(f"release pointer is not a regular file: {pointer}")
    temporary = release_root / f".{CURRENT_POINTER}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{release_name}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pointer)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Durably write JSON before atomically replacing a regular file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
