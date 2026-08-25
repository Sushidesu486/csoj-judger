import errno
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from oj_checker.domain import Submission


class UnsafeSubmissionPath(ValueError):
    """A manifest path cannot be opened beneath the submission input root safely."""


class SubmissionFileError(RuntimeError):
    """A declared submission file is missing or unreadable."""


class OmissionReason(StrEnum):
    FILE_BUDGET = "file_budget"
    TOTAL_BUDGET = "total_budget"


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
    omission_reason: OmissionReason | None

    @property
    def truncated(self) -> bool:
        return self.omission_reason is not None


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

        source_files = []
        total_bytes_read = 0
        for path, parts, declared_bytes, declared_sha256 in sorted(entries):
            if not _is_source_path(path, policy, submission.lab_definition):
                continue
            remaining = policy.max_total_bytes - total_bytes_read
            if remaining <= 0:
                _validate_file_beneath(self._oj_root, submission.id, parts)
                source_files.append(
                    SourceFile(
                        path=path,
                        declared_bytes=declared_bytes,
                        declared_sha256=declared_sha256,
                        content="",
                        bytes_read=0,
                        omission_reason=OmissionReason.TOTAL_BUDGET,
                    )
                )
                continue
            read_limit = min(policy.max_file_bytes, remaining)
            data, truncated = _read_file_beneath(
                self._oj_root, submission.id, parts, read_limit
            )
            omission_reason = None
            if truncated:
                omission_reason = (
                    OmissionReason.FILE_BUDGET
                    if policy.max_file_bytes <= remaining
                    else OmissionReason.TOTAL_BUDGET
                )
            source_files.append(
                SourceFile(
                    path=path,
                    declared_bytes=declared_bytes,
                    declared_sha256=declared_sha256,
                    content=data.decode("utf-8", "replace"),
                    bytes_read=len(data),
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


def _is_source_path(
    path: str, policy: SourcePolicy, lab_definition: Mapping[str, object]
) -> bool:
    name = path.rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if name in policy.source_names or suffix in policy.source_suffixes:
        return True
    return any(
        _matches_lab_pattern(path, pattern)
        for pattern in _lab_file_patterns(lab_definition)
    )


def _lab_file_patterns(lab_definition: Mapping[str, object]) -> tuple[str, ...]:
    spec = lab_definition.get("spec")
    if not isinstance(spec, Mapping):
        return ()
    submissions = spec.get("submissions")
    if not isinstance(submissions, Mapping):
        return ()
    home = submissions.get("home")
    if not isinstance(home, Mapping):
        return ()

    patterns: list[str] = []
    for key in ("required", "allow"):
        values = home.get(key)
        if not isinstance(values, list):
            continue
        patterns.extend(value for value in values if isinstance(value, str))
    return tuple(patterns)


def _matches_lab_pattern(path: str, pattern: str) -> bool:
    if (
        not pattern
        or pattern.startswith("/")
        or "\\" in pattern
        or "\x00" in pattern
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
    ):
        return False

    expression = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*" and index + 1 < len(pattern) and pattern[index + 1] == "*":
            expression.append(".*")
            index += 2
        elif character == "*":
            expression.append("[^/]*")
            index += 1
        elif character == "?":
            expression.append("[^/]")
            index += 1
        else:
            expression.append(re.escape(character))
            index += 1
    return re.fullmatch("".join(expression), path) is not None


def _read_file_beneath(
    oj_root: Path,
    submission_id: str,
    parts: tuple[str, ...],
    read_limit: int,
) -> tuple[bytes, bool]:
    file_fd = _open_regular_file_beneath(oj_root, submission_id, parts)
    with os.fdopen(file_fd, "rb") as file:
        data = file.read(read_limit + 1)
    return data[:read_limit], len(data) > read_limit


def _validate_file_beneath(
    oj_root: Path, submission_id: str, parts: tuple[str, ...]
) -> None:
    file_fd = _open_regular_file_beneath(oj_root, submission_id, parts)
    os.close(file_fd)


def _open_regular_file_beneath(
    oj_root: Path, submission_id: str, parts: tuple[str, ...]
) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure submission reads require O_NOFOLLOW")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_fds.append(os.open(oj_root, directory_flags))
        directory_parts = ("submissions", submission_id, "input", *parts[:-1])
        for part in directory_parts:
            directory_fds.append(os.open(part, directory_flags, dir_fd=directory_fds[-1]))
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fds[-1])
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            file_fd = None
            raise UnsafeSubmissionPath("declared submission path is not a regular file")
    except OSError as error:
        if file_fd is not None:
            os.close(file_fd)
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
    return file_fd
