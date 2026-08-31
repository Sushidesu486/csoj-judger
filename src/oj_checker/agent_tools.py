from __future__ import annotations

import filecmp
import hashlib
import json
import os
import stat
import subprocess
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_ROOT_NAMES = frozenset({"submission", "baseline", "context"})


class ToolError(ValueError):
    """A requested read-only tool operation is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class AgentWorkspace:
    submission: Path
    baseline: Path
    context: Path

    def __post_init__(self) -> None:
        for name in _ROOT_NAMES:
            root = getattr(self, name)
            if not root.is_dir() or root.is_symlink():
                raise ValueError(f"workspace root {name!r} must be a non-symlink directory")

    def root(self, name: str) -> Path:
        roots = {
            "submission": self.submission,
            "baseline": self.baseline,
            "context": self.context,
        }
        try:
            return roots[name]
        except KeyError:
            raise ToolError(f"unknown workspace root: {name!r}") from None


@dataclass(frozen=True, slots=True)
class ToolObservation:
    payload: Mapping[str, Any]
    paths: tuple[str, ...] = ()
    bytes_returned: int = 0


class ReadOnlyToolBroker:
    def __init__(
        self,
        workspace: AgentWorkspace,
        *,
        max_response_bytes: int = 32 * 1024,
        max_tree_entries: int = 500,
        max_search_matches: int = 200,
        max_read_lines: int = 400,
        max_diff_lines: int = 300,
    ) -> None:
        limits = (
            max_response_bytes,
            max_tree_entries,
            max_search_matches,
            max_read_lines,
            max_diff_lines,
        )
        if any(value <= 0 for value in limits):
            raise ValueError("tool limits must be positive")
        self.workspace = workspace
        self.max_response_bytes = max_response_bytes
        self.max_tree_entries = max_tree_entries
        self.max_search_matches = max_search_matches
        self.max_read_lines = max_read_lines
        self.max_diff_lines = max_diff_lines

    def inventory(self) -> dict[str, Any]:
        roots: dict[str, Any] = {}
        for root_name in sorted(_ROOT_NAMES):
            file_count = 0
            total_bytes = 0
            suffixes: Counter[str] = Counter()
            for relative, path in self._walk_files(root_name, "."):
                file_count += 1
                total_bytes += path.stat().st_size
                suffixes[Path(relative).suffix.lower() or "<none>"] += 1
            roots[root_name] = {
                "file_count": file_count,
                "total_bytes": total_bytes,
                "suffixes": dict(sorted(suffixes.items())),
            }
        return {"roots": roots}

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolObservation:
        handlers = {
            "list_tree": self._list_tree,
            "file_info": self._file_info,
            "search": self._search,
            "read_lines": self._read_lines,
            "compare_file": self._compare_file,
            "find_references": self._find_references,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name!r}")
        observation = handler(arguments)
        encoded = json.dumps(
            observation.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > self.max_response_bytes:
            raise RuntimeError(f"tool {name!r} exceeded its response boundary")
        return ToolObservation(observation.payload, observation.paths, len(encoded))

    def validate_evidence(self, value: Mapping[str, Any]) -> str | None:
        root_name = value.get("root")
        path = value.get("path")
        start_line = value.get("start_line")
        end_line = value.get("end_line")
        if not isinstance(root_name, str) or not isinstance(path, str):
            return "evidence root and path must be strings"
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or start_line <= 0
            or end_line < start_line
        ):
            return "evidence line range is invalid"
        try:
            resolved = self._resolve_file(root_name, path)
        except ToolError as error:
            return str(error)
        line_count = self._line_count(resolved)
        if end_line > line_count:
            return f"evidence line {end_line} exceeds file line count {line_count}"
        return None

    def _list_tree(self, arguments: Mapping[str, Any]) -> ToolObservation:
        root_name = _string_argument(arguments, "root")
        path = _optional_string_argument(arguments, "path", ".")
        depth = _optional_int_argument(arguments, "depth", 3, minimum=0, maximum=16)
        cursor = _optional_int_argument(arguments, "cursor", 0, minimum=0)
        directory = self._resolve_directory(root_name, path)
        entries: list[dict[str, Any]] = []

        def visit(current: Path, relative: PurePosixPath, remaining: int) -> None:
            for entry in sorted(os.scandir(current), key=lambda item: item.name):
                item_relative = relative / entry.name
                item_path = Path(entry.path)
                if entry.is_symlink():
                    entries.append({"path": item_relative.as_posix(), "type": "symlink-denied"})
                elif entry.is_file(follow_symlinks=False):
                    entries.append(
                        {
                            "path": item_relative.as_posix(),
                            "type": "file",
                            "bytes": entry.stat(follow_symlinks=False).st_size,
                        }
                    )
                elif entry.is_dir(follow_symlinks=False):
                    entries.append({"path": item_relative.as_posix(), "type": "directory"})
                    if remaining > 0:
                        visit(item_path, item_relative, remaining - 1)

        base_relative = PurePosixPath("" if path == "." else path)
        visit(directory, base_relative, depth)
        selected = entries[cursor : cursor + self.max_tree_entries]
        payload = {
            "root": root_name,
            "path": path,
            "entries": selected,
            "truncated": cursor + len(selected) < len(entries),
            "next_cursor": cursor + len(selected)
            if cursor + len(selected) < len(entries)
            else None,
        }
        return ToolObservation(
            payload,
            tuple(f"{root_name}/{item['path']}" for item in selected),
        )

    def _file_info(self, arguments: Mapping[str, Any]) -> ToolObservation:
        root_name = _string_argument(arguments, "root")
        path = _string_argument(arguments, "path")
        resolved = self._resolve_file(root_name, path)
        digest = hashlib.sha256()
        line_count = 0
        binary = False
        with resolved.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
                if b"\0" in chunk:
                    binary = True
                line_count += chunk.count(b"\n")
        size = resolved.stat().st_size
        if size and not self._ends_with_newline(resolved):
            line_count += 1
        payload = {
            "root": root_name,
            "path": path,
            "bytes": size,
            "sha256": digest.hexdigest(),
            "binary": binary,
            "line_count": line_count,
        }
        return ToolObservation(payload, (f"{root_name}/{path}",))

    def _search(self, arguments: Mapping[str, Any]) -> ToolObservation:
        root_name = _string_argument(arguments, "root")
        path = _optional_string_argument(arguments, "path", ".")
        query = _string_argument(arguments, "query")
        if not query or len(query) > 512:
            raise ToolError("search query length must be between 1 and 512")
        case_sensitive = _optional_bool_argument(arguments, "case_sensitive", True)
        cursor = _optional_int_argument(arguments, "cursor", 0, minimum=0)
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        for relative, resolved in self._walk_files(root_name, path):
            if self._is_binary(resolved):
                continue
            with resolved.open("r", encoding="utf-8", errors="replace") as file:
                for line_number, line in enumerate(file, start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle not in haystack:
                        continue
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line.rstrip("\r\n")[:1000],
                        }
                    )
        selected = matches[cursor : cursor + self.max_search_matches]
        selected = self._fit_list_payload(
            selected,
            lambda items, truncated, next_cursor: {
                "root": root_name,
                "path": path,
                "query": query,
                "matches": items,
                "truncated": truncated,
                "next_cursor": next_cursor,
            },
            cursor,
            len(matches),
        )
        payload = {
            "root": root_name,
            "path": path,
            "query": query,
            "matches": selected,
            "truncated": cursor + len(selected) < len(matches),
            "next_cursor": cursor + len(selected)
            if cursor + len(selected) < len(matches)
            else None,
        }
        return ToolObservation(
            payload,
            tuple(dict.fromkeys(f"{root_name}/{item['path']}" for item in selected)),
        )

    def _find_references(self, arguments: Mapping[str, Any]) -> ToolObservation:
        symbol = _string_argument(arguments, "symbol")
        if not symbol or len(symbol) > 256 or any(character.isspace() for character in symbol):
            raise ToolError("symbol must be a non-empty token without whitespace")
        return self._search(
            {
                "root": arguments.get("root", "submission"),
                "path": arguments.get("path", "."),
                "query": symbol,
                "case_sensitive": arguments.get("case_sensitive", True),
                "cursor": arguments.get("cursor", 0),
            }
        )

    def _read_lines(self, arguments: Mapping[str, Any]) -> ToolObservation:
        root_name = _string_argument(arguments, "root")
        path = _string_argument(arguments, "path")
        start_line = _int_argument(arguments, "start_line", minimum=1)
        end_line = _int_argument(arguments, "end_line", minimum=start_line)
        if end_line - start_line + 1 > self.max_read_lines:
            end_line = start_line + self.max_read_lines - 1
        resolved = self._resolve_file(root_name, path)
        if self._is_binary(resolved):
            raise ToolError("read_lines does not accept binary files")
        lines: list[dict[str, Any]] = []
        with resolved.open("r", encoding="utf-8", errors="replace") as file:
            for line_number, line in enumerate(file, start=1):
                if line_number < start_line:
                    continue
                if line_number > end_line:
                    break
                text = line.rstrip("\r\n")
                candidate: dict[str, Any] = {"line": line_number, "text": text}
                proposed = [*lines, candidate]
                payload = {
                    "root": root_name,
                    "path": path,
                    "lines": proposed,
                    "truncated": True,
                    "next_start_line": line_number + 1,
                }
                if _json_size(payload) > self.max_response_bytes:
                    if not lines:
                        candidate["text"] = text[: max(1, self.max_response_bytes // 4)]
                        candidate["line_truncated"] = True
                        lines.append(candidate)
                    break
                lines.append(candidate)
        next_line = lines[-1]["line"] + 1 if lines and lines[-1]["line"] < end_line else None
        payload = {
            "root": root_name,
            "path": path,
            "lines": lines,
            "truncated": next_line is not None,
            "next_start_line": next_line,
        }
        return ToolObservation(payload, (f"{root_name}/{path}",))

    def _compare_file(self, arguments: Mapping[str, Any]) -> ToolObservation:
        submission_path = _string_argument(arguments, "submission_path")
        baseline_path = _optional_string_argument(
            arguments,
            "baseline_path",
            submission_path,
        )
        cursor = _optional_int_argument(arguments, "cursor", 0, minimum=0)
        context_lines = _optional_int_argument(
            arguments,
            "context_lines",
            3,
            minimum=0,
            maximum=20,
        )
        submission = self._resolve_file("submission", submission_path)
        baseline = self._resolve_file("baseline", baseline_path)
        if self._is_binary(submission) or self._is_binary(baseline):
            raise ToolError("compare_file does not accept binary files")
        identical = filecmp.cmp(submission, baseline, shallow=False)
        selected: list[str] = []
        truncated = False
        if not identical:
            selected, truncated = self._stream_diff(
                submission,
                baseline,
                submission_path,
                baseline_path,
                context_lines,
                cursor,
            )
            selected_before_fit = len(selected)
            selected = self._fit_list_payload(
                selected,
                lambda items, was_truncated, next_cursor: {
                    "submission_path": submission_path,
                    "baseline_path": baseline_path,
                    "diff_lines": items,
                    "truncated": was_truncated,
                    "next_cursor": next_cursor,
                },
                cursor,
                cursor + len(selected) + (1 if truncated else 0),
            )
            truncated = truncated or len(selected) < selected_before_fit
        payload = {
            "submission_path": submission_path,
            "baseline_path": baseline_path,
            "identical": identical,
            "diff_lines": selected,
            "truncated": truncated,
            "next_cursor": cursor + len(selected) if truncated else None,
        }
        return ToolObservation(
            payload,
            (f"submission/{submission_path}", f"baseline/{baseline_path}"),
        )

    def _stream_diff(
        self,
        submission: Path,
        baseline: Path,
        submission_path: str,
        baseline_path: str,
        context_lines: int,
        cursor: int,
    ) -> tuple[list[str], bool]:
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
        }
        try:
            process = subprocess.Popen(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"--unified={context_lines}",
                    "--",
                    str(baseline),
                    str(submission),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        except FileNotFoundError:
            raise RuntimeError("compare_file requires the trusted git binary") from None
        selected: list[str] = []
        truncated = False
        assert process.stdout is not None
        try:
            for index, raw_line in enumerate(process.stdout):
                if index < cursor:
                    continue
                if len(selected) >= self.max_diff_lines:
                    truncated = True
                    break
                line = raw_line.rstrip("\r\n")[:2000]
                line = line.replace(str(baseline), f"baseline/{baseline_path}")
                line = line.replace(str(submission), f"submission/{submission_path}")
                selected.append(line)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if not truncated and process.returncode not in {0, 1}:
            assert process.stderr is not None
            error = process.stderr.read(1000).strip()
            raise RuntimeError(f"trusted git diff failed: {error or process.returncode}")
        return selected, truncated

    def _fit_list_payload(
        self,
        values: list[Any],
        build: Any,
        cursor: int,
        total: int,
    ) -> list[Any]:
        selected = list(values)
        while selected:
            truncated = cursor + len(selected) < total
            next_cursor = cursor + len(selected) if truncated else None
            if _json_size(build(selected, truncated, next_cursor)) <= self.max_response_bytes:
                return selected
            selected.pop()
        return selected

    def _resolve_file(self, root_name: str, path: str) -> Path:
        resolved = self._resolve(root_name, path)
        try:
            mode = resolved.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            raise ToolError(f"file does not exist: {root_name}/{path}") from None
        if not stat.S_ISREG(mode):
            raise ToolError(f"path is not a regular file: {root_name}/{path}")
        return resolved

    def _resolve_directory(self, root_name: str, path: str) -> Path:
        resolved = self._resolve(root_name, path)
        try:
            mode = resolved.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            raise ToolError(f"directory does not exist: {root_name}/{path}") from None
        if not stat.S_ISDIR(mode):
            raise ToolError(f"path is not a directory: {root_name}/{path}")
        return resolved

    def _resolve(self, root_name: str, path: str) -> Path:
        root = self.workspace.root(root_name)
        parts = _safe_parts(path)
        current = root
        for part in parts:
            current = current / part
            try:
                mode = current.stat(follow_symlinks=False).st_mode
            except FileNotFoundError:
                raise ToolError(f"path does not exist: {root_name}/{path}") from None
            if stat.S_ISLNK(mode):
                raise ToolError(f"symlinks are not allowed: {root_name}/{path}")
        return current

    def _walk_files(self, root_name: str, path: str) -> list[tuple[str, Path]]:
        root = self.workspace.root(root_name)
        base = self._resolve(root_name, path)
        try:
            mode = base.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            raise ToolError(f"path does not exist: {root_name}/{path}") from None
        if stat.S_ISREG(mode):
            return [(base.relative_to(root).as_posix(), base)]
        if not stat.S_ISDIR(mode):
            raise ToolError(f"path is not a file or directory: {root_name}/{path}")
        files: list[tuple[str, Path]] = []

        def visit(directory: Path) -> None:
            for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                if entry.is_symlink():
                    continue
                entry_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    visit(entry_path)
                elif entry.is_file(follow_symlinks=False):
                    files.append((entry_path.relative_to(root).as_posix(), entry_path))

        visit(base)
        return files

    @staticmethod
    def _is_binary(path: Path) -> bool:
        with path.open("rb") as file:
            return b"\0" in file.read(8192)

    @staticmethod
    def _line_count(path: Path) -> int:
        count = 0
        size = 0
        last = b""
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                size += len(chunk)
                count += chunk.count(b"\n")
                last = chunk[-1:]
        return count + (1 if size and last != b"\n" else 0)

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as file:
            file.seek(-1, os.SEEK_END)
            return file.read(1) == b"\n"


def _safe_parts(path: str) -> tuple[str, ...]:
    if not path or "\\" in path or "\0" in path:
        raise ToolError(f"unsafe relative path: {path!r}")
    if path == ".":
        return ()
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ToolError(f"unsafe relative path: {path!r}")
    return pure.parts


def _string_argument(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    return value


def _optional_string_argument(arguments: Mapping[str, Any], name: str, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    return default if value == "" else value


def _int_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ToolError(f"{name} is outside the allowed range")
    return value


def _optional_int_argument(
    arguments: Mapping[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if name not in arguments:
        return default
    return _int_argument(arguments, name, minimum=minimum, maximum=maximum)


def _optional_bool_argument(arguments: Mapping[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ToolError(f"{name} must be a boolean")
    return value


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
