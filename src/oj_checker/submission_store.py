import errno
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from oj_checker.domain import Submission


class UnsafeSubmissionPath(ValueError):
    """A manifest path cannot be opened beneath the submission input root safely."""


class SubmissionFileError(RuntimeError):
    """A declared submission file is missing or unreadable."""


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    max_file_bytes: int = 256_000
    max_total_bytes: int = 1_000_000
    source_suffixes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                ".c",
                ".cc",
                ".cpp",
                ".cxx",
                ".cu",
                ".h",
                ".hpp",
                ".py",
                ".sh",
                ".cmake",
                ".yaml",
                ".yml",
                ".toml",
                ".json",
            }
        )
    )
    source_names: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"CMakeLists.txt", "Makefile", "makefile", "compile.sh", "run.sh"}
        )
    )


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    declared_bytes: int
    declared_sha256: str | None
    content: str
    bytes_read: int
    truncated: bool
    omission_reason: str | None


@dataclass(frozen=True, slots=True)
class SourceBundle:
    submission_id: str
    files: tuple[SourceFile, ...]
    total_bytes_read: int


class SubmissionStore(Protocol):
    def load_bundle(self, submission: Submission, policy: SourcePolicy) -> SourceBundle: ...


class NfsSubmissionStore:
    def __init__(self, oj_root: str | Path) -> None:
        self._oj_root = Path(oj_root)

    def load_bundle(self, submission: Submission, policy: SourcePolicy) -> SourceBundle:
        if policy.max_file_bytes <= 0 or policy.max_total_bytes <= 0:
            raise ValueError("source byte budgets must be positive")
        _validate_submission_id(submission.id)
        manifest_files = submission.input_manifest.get("files", [])
        if not isinstance(manifest_files, list):
            raise UnsafeSubmissionPath("input manifest files must be a list")

        entries: list[tuple[str, tuple[str, ...], int, str | None]] = []
        seen: set[str] = set()
        for entry in manifest_files:
            if not isinstance(entry, Mapping):
                raise UnsafeSubmissionPath("input manifest file entry must be an object")
            path = entry.get("path")
            if not isinstance(path, str):
                raise UnsafeSubmissionPath("input manifest path must be a string")
            parts = _safe_path_parts(path)
            if path in seen:
                raise UnsafeSubmissionPath(f"duplicate input manifest path: {path!r}")
            seen.add(path)
            declared_bytes = entry.get("size", 0)
            if (
                not isinstance(declared_bytes, int)
                or isinstance(declared_bytes, bool)
                or declared_bytes < 0
            ):
                raise UnsafeSubmissionPath(f"invalid declared file size for {path!r}")
            declared_sha256 = entry.get("sha256")
            if declared_sha256 is not None and not isinstance(declared_sha256, str):
                raise UnsafeSubmissionPath(f"invalid declared sha256 for {path!r}")
            entries.append((path, parts, declared_bytes, declared_sha256))

        input_root = self._oj_root / "submissions" / submission.id / "input"
        source_files = []
        total_bytes_read = 0
        for path, parts, declared_bytes, declared_sha256 in sorted(entries):
            if not _is_source_path(path, policy):
                continue
            remaining = policy.max_total_bytes - total_bytes_read
            if remaining <= 0:
                source_files.append(
                    SourceFile(
                        path=path,
                        declared_bytes=declared_bytes,
                        declared_sha256=declared_sha256,
                        content="",
                        bytes_read=0,
                        truncated=True,
                        omission_reason="total_budget",
                    )
                )
                continue
            read_limit = min(policy.max_file_bytes, remaining)
            data, truncated = _read_file_beneath(input_root, parts, read_limit)
            omission_reason = None
            if truncated:
                omission_reason = (
                    "file_budget" if policy.max_file_bytes <= remaining else "total_budget"
                )
            source_files.append(
                SourceFile(
                    path=path,
                    declared_bytes=declared_bytes,
                    declared_sha256=declared_sha256,
                    content=data.decode("utf-8", "replace"),
                    bytes_read=len(data),
                    truncated=truncated,
                    omission_reason=omission_reason,
                )
            )
            total_bytes_read += len(data)

        return SourceBundle(submission.id, tuple(source_files), total_bytes_read)


def _validate_submission_id(submission_id: str) -> None:
    try:
        parsed = UUID(submission_id)
    except ValueError:
        raise UnsafeSubmissionPath("submission id is not a UUID") from None
    if str(parsed) != submission_id.lower():
        raise UnsafeSubmissionPath("submission id is not a canonical UUID")


def _safe_path_parts(path: str) -> tuple[str, ...]:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise UnsafeSubmissionPath(f"unsafe input manifest path: {path!r}")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeSubmissionPath(f"unsafe input manifest path: {path!r}")
    return parts


def _is_source_path(path: str, policy: SourcePolicy) -> bool:
    name = path.rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    return name in policy.source_names or suffix in policy.source_suffixes


def _read_file_beneath(
    input_root: Path, parts: tuple[str, ...], read_limit: int
) -> tuple[bytes, bool]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure submission reads require O_NOFOLLOW")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_fds.append(os.open(input_root, directory_flags))
        for part in parts[:-1]:
            directory_fds.append(os.open(part, directory_flags, dir_fd=directory_fds[-1]))
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fds[-1])
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            file_fd = None
            raise UnsafeSubmissionPath("declared submission path is not a regular file")
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeSubmissionPath(
                "submission path contains a symlink or non-directory"
            ) from None
        raise SubmissionFileError("declared submission file is missing or unreadable") from None
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)

    if file_fd is None:
        raise SubmissionFileError("declared submission file could not be opened")
    with os.fdopen(file_fd, "rb") as file:
        data = file.read(read_limit + 1)
    return data[:read_limit], len(data) > read_limit
