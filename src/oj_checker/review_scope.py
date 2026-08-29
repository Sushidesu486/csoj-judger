# ruff: noqa: RUF001
import ast
import fnmatch
import hashlib
import re
import subprocess
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from oj_checker.review_basis import ReviewBasis
from oj_checker.similarity import BaselineDelta, BaselineDeltaBuilder, DeltaBaseline
from oj_checker.submission_store import SourceBundle, SourceFile

_LAB4_IDS = frozenset({"lab4-cpu", "lab4-gpu"})
_ROOT_EXECUTION_FILES = frozenset({"CMakeLists.txt", "compile.sh", "run.sh"})
_INPUT_FILE = "AMSS_NCKU_Input.py"
_COURSE_CRITICAL_PATHS = frozenset({"src/macrodef.h", "src/macrodef.fh"})
_COURSE_CRITICAL_MACROS = (
    "ABEtype",
    "GAUGE",
    "SommerType",
    "Psi4type",
    "RPS",
    "AGM",
    "RPB",
    "MAPBH",
    "Cell",
    "Vertex",
    "ghost_width",
)
_COMPILED_SUFFIXES = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".cu", ".f", ".f77", ".f90", ".f95", ".for"}
)
_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".cuh", ".fh", ".inc"})
_BUILD_FILE_NAMES = frozenset({"CMakeLists.txt", "Makefile", "makefile"})
_INCLUDE_PATTERN = re.compile(
    r"(?im)^\s*(?:#\s*include\s*[<\"]([^>\"]+)[>\"]|include\s*['\"]([^'\"]+)['\"])"
)
_PATH_GLOB_PATTERN = re.compile(r"src/[A-Za-z0-9_./*?\[\]-]+")
_DYNAMIC_BUILD_PATTERN = re.compile(
    r"(?i)\b(?:aux_source_directory|execute_process|add_custom_command|"
    r"meson|scons|bazel)\b"
)


LAB4_REVIEW_POLICY = """Lab 4 审查规则（OJ 实际执行语义，精简版）：
- 本任务只审查违规优化、预计算、硬编码或绕过评测，不审查代码查重。
- 允许修改 C/C++/Fortran/CUDA 源码、头文件、CMakeLists.txt、compile.sh、run.sh 和必要辅助文件；
  允许 MPI、OpenMP、CUDA、编译器及数据布局优化。
- 禁止减少必须完成的物理计算、缩小网格、缩短演化时间、跳过必要输出、伪造/预置结果，
  或针对 checker 硬编码答案。
- OJ 从提交的 AMSS_NCKU_Input.py 中只读取整数字面量 MPI_processes 与 OMP_threads；
  物理参数、网格、CPU/GPU track 和演化终点均由可信 judge 恢复。该文件的其他内容不会执行。
- 正式运行必须重新执行 TwoPunctureABE 和演化程序（CPU 为 ABE，GPU 为 ABEGPU）。
  OJ 计量完整 run.sh 的单调时钟耗时。
- 被审查对象是已成功计分的有效提交，说明本次正式运行已经通过下述 OJ 数值检查；
  不要把一般并行 bug、潜在数据竞争、性能退化或未采用某个 tuning 参数本身判成违规优化。
- OJ 校验 bssn_BH.dat 的完整时间覆盖和轨迹 RMS <= 0.001；并校验 bssn_constraint.dat
  的完整时间覆盖及 Hamiltonian/Px/Py/Pz <= 2.0。
- bssn_ADMQs.dat 与 bssn_psi4.dat 当前只要求存在且非空，因此仍需审查是否伪造或绕过其真实计算。
- 删除仅用于调试、日志、profiling 或 NaN 诊断的检查本身不属于违规优化；
  只有它同时减少必要物理计算、伪造输出或让无效结果绕过正式 OJ 校验时才报告违规。
- 大规模重构、替换插值/kernel 实现或缺少等价性证明本身不是违规证据。
  必须指出具体被跳过的计算、缩小的范围、硬编码数据或输出伪造路径。
- review_scope.critical_configuration 是程序对提交与课程编译期宏的确定性比较；
  matches_course=true 的宏不得再按公共上游差异报告为配置违规。
- evidence 中 public_origin baseline 表示课程文档明确列出的公共 AMSS-NCKU 上游固定提交；
  与该固定提交完全相同且处于执行闭包内的文件已由程序核验，可不重复发送。
  课程规则和 OJ 强制参数仍然优先。
- 审查输入由静态构建定义、脚本和递归本地 include 闭包保守生成；
  被证明不参与构建或运行的备份、旧实现和源目录数据文件不会发送给模型。
"""


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    repository: str
    revision: str
    files: Mapping[str, str]
    tree_digest: str


class Lab4ReferenceProvider(Protocol):
    def load(self) -> tuple[ReferenceSnapshot, ...]: ...


class GitLab4ReferenceProvider:
    """Load explicitly pinned public AMSS-NCKU source snapshots.

    The upstream repository stores sources in categorized subdirectories while
    OJ submissions use a flat ``src`` directory. Only unambiguous basenames are
    exposed to the matcher.
    """

    def __init__(
        self,
        repository: str | Path,
        revisions: Sequence[str],
        *,
        label: str = "xiaoqu0000/NR-amssncku",
        source_path: str = "amss-ncku-python/AMSS_NCKU_source",
    ) -> None:
        if not revisions:
            raise ValueError("at least one Lab 4 reference revision is required")
        self._repository = Path(repository)
        self._label = label
        self._source_path = source_path.rstrip("/")
        self._revisions = tuple(
            self._git_text("rev-parse", "--verify", f"{revision}^{{commit}}")
            for revision in revisions
        )
        self._cache: tuple[ReferenceSnapshot, ...] | None = None

    def load(self) -> tuple[ReferenceSnapshot, ...]:
        if self._cache is None:
            self._cache = tuple(self._load_revision(revision) for revision in self._revisions)
        return self._cache

    def _load_revision(self, revision: str) -> ReferenceSnapshot:
        raw_paths = self._git_bytes(
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            revision,
            "--",
            self._source_path,
        )
        paths = tuple(path for path in raw_paths.decode().split("\0") if path)
        by_name: dict[str, list[str]] = defaultdict(list)
        for path in paths:
            by_name[PurePosixPath(path).name].append(path)

        files: dict[str, str] = {}
        tree_hasher = hashlib.sha256()
        for name, candidates in sorted(by_name.items()):
            if len(candidates) != 1:
                continue
            content = self._git_bytes("show", f"{revision}:{candidates[0]}")
            relative = f"src/{name}"
            files[relative] = content.decode("utf-8", errors="replace")
            tree_hasher.update(relative.encode())
            tree_hasher.update(b"\0")
            tree_hasher.update(hashlib.sha256(content).digest())
        return ReferenceSnapshot(
            repository=self._label,
            revision=revision,
            files=files,
            tree_digest=tree_hasher.hexdigest(),
        )

    def _git_text(self, *arguments: str) -> str:
        return self._git_bytes(*arguments).decode().strip()

    def _git_bytes(self, *arguments: str) -> bytes:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self._repository,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
        return result.stdout


@dataclass(frozen=True, slots=True)
class ComplianceReviewScope:
    source_bundle: SourceBundle
    delta: BaselineDelta
    policy: str | None
    diagnostics: Mapping[str, Any]


class ComplianceReviewScopeBuilder:
    def __init__(
        self,
        delta_builder: BaselineDeltaBuilder,
        *,
        lab4_references: Lab4ReferenceProvider | None = None,
        minimum_reference_files: int = 8,
        minimum_reference_bytes: int = 128_000,
        minimum_reference_ratio: float = 0.3,
    ) -> None:
        if minimum_reference_files <= 0 or minimum_reference_bytes <= 0:
            raise ValueError("reference match minimums must be positive")
        if not 0 <= minimum_reference_ratio <= 1:
            raise ValueError("minimum_reference_ratio must be between zero and one")
        self._delta_builder = delta_builder
        self._lab4_references = lab4_references
        self._minimum_reference_files = minimum_reference_files
        self._minimum_reference_bytes = minimum_reference_bytes
        self._minimum_reference_ratio = minimum_reference_ratio

    def build(
        self,
        lab_id: str,
        bundle: SourceBundle,
        basis: ReviewBasis,
    ) -> ComplianceReviewScope:
        if lab_id not in _LAB4_IDS:
            return ComplianceReviewScope(
                source_bundle=bundle,
                delta=self._delta_builder.build(bundle, basis),
                policy=None,
                diagnostics={"strategy": "full-source-v1", "complete": not _has_truncation(bundle)},
            )
        return self._build_lab4(lab_id, bundle, basis)

    def _build_lab4(
        self,
        lab_id: str,
        bundle: SourceBundle,
        basis: ReviewBasis,
    ) -> ComplianceReviewScope:
        files = {file.path: file for file in bundle.files}
        included, reasons, scope_complete, fallback_reason = _lab4_execution_scope(files)
        input_parameters, input_error = _lab4_input_parameters(files.get(_INPUT_FILE), lab_id)
        if input_error is not None:
            scope_complete = False
            fallback_reason = fallback_reason or input_error

        selected_files = tuple(files[path] for path in sorted(included) if path in files)
        selected_bundle = SourceBundle(
            submission_id=bundle.submission_id,
            files=selected_files,
            total_bytes_read=sum(file.bytes_read for file in selected_files),
            declared_paths=bundle.declared_paths,
            required_patterns=bundle.required_patterns,
            allowed_patterns=bundle.allowed_patterns,
        )
        course_baselines = {
            file.path: DeltaBaseline(
                file.text(),
                kind="course",
                revision=basis.upstream_commit,
            )
            for file in basis.files
        }
        critical_configuration = _critical_configuration(files, course_baselines)
        reference, reference_diagnostics = self._select_reference(
            selected_files,
            course_baselines,
        )
        selected_baselines: dict[str, DeltaBaseline] = {}
        for file in selected_files:
            baseline = course_baselines.get(file.path)
            if baseline is not None and (
                not file.path.startswith("src/") or file.path in _COURSE_CRITICAL_PATHS
            ):
                # Root execution files and compile-time physics configuration
                # are course-specific invariants, not generic upstream code.
                selected_baselines[file.path] = baseline
                continue
            if reference is not None and file.path.startswith("src/"):
                reference_text = reference.files.get(file.path)
                if reference_text is not None:
                    selected_baselines[file.path] = DeltaBaseline(
                        reference_text,
                        kind="public_origin",
                        revision=reference.revision,
                    )
                    continue
            if baseline is not None:
                selected_baselines[file.path] = baseline

        delta = self._delta_builder.build_selected(selected_bundle, selected_baselines)
        excluded_files = []
        for path, file in sorted(files.items()):
            if path == _INPUT_FILE:
                reason = "judge_reads_only_mpi_omp_literals"
            elif path not in included:
                reason = "outside_static_build_include_runtime_closure"
            else:
                continue
            excluded_files.append(
                {"path": path, "declared_bytes": file.declared_bytes, "reason": reason}
            )

        delta_paths = {file.path for file in delta.files}
        unchanged_files = [
            {
                "path": file.path,
                "declared_bytes": file.declared_bytes,
                "baseline_kind": selected_baselines[file.path].kind,
                "baseline_revision": selected_baselines[file.path].revision,
            }
            for file in selected_files
            if file.path in selected_baselines and file.path not in delta_paths
        ]
        diagnostics: dict[str, Any] = {
            "strategy": "lab4-execution-scope-v1",
            "track": "cpu" if lab_id == "lab4-cpu" else "gpu",
            "complete": scope_complete and not _has_truncation(bundle),
            "fallback_reason": fallback_reason,
            "execution_targets": (
                ["TwoPunctureABE", "ABE"]
                if lab_id == "lab4-cpu"
                else ["TwoPunctureABE", "ABEGPU"]
            ),
            "input_parameters": input_parameters,
            "input_file_error": input_error,
            "critical_configuration": critical_configuration,
            "original": {
                "file_count": len(bundle.files),
                "bytes_read": bundle.total_bytes_read,
                "truncated_file_count": sum(file.truncated for file in bundle.files),
            },
            "execution_scope": {
                "file_count": len(selected_files),
                "bytes_read": selected_bundle.total_bytes_read,
                "reasons": {path: sorted(values) for path, values in sorted(reasons.items())},
            },
            "excluded": {
                "file_count": len(excluded_files),
                "declared_bytes": sum(item["declared_bytes"] for item in excluded_files),
                "files": excluded_files,
            },
            "unchanged_against_selected_baseline": {
                "file_count": len(unchanged_files),
                "declared_bytes": sum(item["declared_bytes"] for item in unchanged_files),
                "files": unchanged_files,
            },
            "review_delta": {
                "file_count": len(delta.files),
                "hunk_count": sum(len(file.hunks) for file in delta.files),
            },
            "reference_selection": reference_diagnostics,
        }
        return ComplianceReviewScope(
            source_bundle=selected_bundle,
            delta=delta,
            policy=LAB4_REVIEW_POLICY,
            diagnostics=diagnostics,
        )

    def _select_reference(
        self,
        selected_files: tuple[SourceFile, ...],
        course_baselines: Mapping[str, DeltaBaseline],
    ) -> tuple[ReferenceSnapshot | None, Mapping[str, Any]]:
        eligible = tuple(
            file
            for file in selected_files
            if file.path.startswith("src/") and _is_code_path(file.path) and not file.truncated
        )
        course_match = _reference_match(
            eligible,
            {path: baseline.text for path, baseline in course_baselines.items()},
        )
        candidates = []
        references = self._lab4_references.load() if self._lab4_references is not None else ()
        for reference in references:
            match = _reference_match(eligible, reference.files)
            candidates.append(
                {
                    "repository": reference.repository,
                    "revision": reference.revision,
                    "tree_digest": reference.tree_digest,
                    **match,
                }
            )
        candidates.sort(key=lambda item: (item["exact_bytes"], item["exact_file_count"]))
        best = candidates[-1] if candidates else None
        selected = None
        if best is not None:
            enough_evidence = (
                best["exact_file_count"] >= self._minimum_reference_files
                and best["exact_bytes"] >= self._minimum_reference_bytes
                and best["exact_ratio"] >= self._minimum_reference_ratio
            )
            beats_course = (
                best["exact_bytes"],
                best["exact_file_count"],
            ) > (
                course_match["exact_bytes"],
                course_match["exact_file_count"],
            )
            if enough_evidence and beats_course:
                selected = next(
                    reference
                    for reference in references
                    if reference.revision == best["revision"]
                )
        return selected, {
            "selected_repository": selected.repository if selected is not None else "HPC101",
            "selected_revision": (
                selected.revision if selected is not None else "course_baseline"
            ),
            "course_match": course_match,
            "candidates": candidates,
        }


def _lab4_execution_scope(
    files: Mapping[str, SourceFile],
) -> tuple[set[str], dict[str, set[str]], bool, str | None]:
    included: set[str] = set()
    reasons: dict[str, set[str]] = defaultdict(set)

    def include(path: str, reason: str) -> None:
        if path in files:
            included.add(path)
            reasons[path].add(reason)

    for path in _ROOT_EXECUTION_FILES:
        include(path, "mandatory_execution_file")

    compile_file = files.get("compile.sh")
    run_file = files.get("run.sh")
    compile_text = compile_file.content if compile_file is not None else ""
    run_text = run_file.content if run_file is not None else ""
    build_paths = {"CMakeLists.txt"} if "CMakeLists.txt" in files else set()
    if "cmake" in compile_text.lower() or "cmake" in run_text.lower():
        build_paths.update(
            path
            for path in files
            if PurePosixPath(path).name == "CMakeLists.txt"
            or PurePosixPath(path).suffix.lower() == ".cmake"
        )
    if re.search(r"(?i)(?:^|[^A-Za-z0-9_])(?:g?make)(?:[^A-Za-z0-9_]|$)", compile_text):
        build_paths.update(
            path
            for path in files
            if PurePosixPath(path).name in _BUILD_FILE_NAMES
            or PurePosixPath(path).suffix.lower() in {".mk", ".make"}
        )
    for path in build_paths:
        include(path, "build_definition")

    build_text = "\n".join(
        [compile_text, run_text]
        + [files[path].content for path in sorted(build_paths) if path in files]
    )
    dynamic_fallback = bool(_DYNAMIC_BUILD_PATTERN.search(build_text))
    source_paths = [path for path in files if _is_compiled_path(path)]
    for path in source_paths:
        name = PurePosixPath(path).name
        stem = PurePosixPath(path).stem
        if _mentioned_build_input(path, name, stem, build_text):
            include(path, "referenced_by_build_definition")

    for pattern in _PATH_GLOB_PATTERN.findall(build_text):
        for path in files:
            if fnmatch.fnmatchcase(path, pattern):
                include(path, f"matched_build_glob:{pattern}")

    selected_compiled = [path for path in included if _is_compiled_path(path)]
    if dynamic_fallback or not selected_compiled:
        for path in source_paths:
            include(
                path,
                "conservative_dynamic_build_fallback"
                if dynamic_fallback
                else "conservative_no_static_source_match",
            )

    _add_include_closure(files, included, reasons)

    execution_text = compile_text + "\n" + run_text + "\n" + build_text
    if re.search(r"src/(?:\$|`)", compile_text + "\n" + run_text):
        for path in files:
            if path.startswith("src/") and not _is_code_path(path):
                include(path, "conservative_dynamic_runtime_asset_path")
    for path in files:
        if path in included or path == _INPUT_FILE:
            continue
        if path.startswith("src/") and path in execution_text:
            include(path, "literal_runtime_or_build_path")

    if re.search(r"(?m)^\s*(?:cd|pushd)\s+(?:['\"])?src(?:/|['\"]|\s|$)", run_text):
        reachable_text = "\n".join(files[path].content for path in included if path in files)
        for path in files:
            if path.startswith("src/") and PurePosixPath(path).name in reachable_text:
                include(path, "runtime_asset_with_src_working_directory")

    complete = not any(file.truncated for file in files.values())
    fallback_reason = None
    if not complete:
        fallback_reason = "source_bundle_contains_truncated_files"
        for path in files:
            if path != _INPUT_FILE:
                include(path, "conservative_truncated_bundle_fallback")
    elif dynamic_fallback:
        fallback_reason = "dynamic_build_construct_requires_all_compiled_sources"
    return included, reasons, complete, fallback_reason


def _mentioned_build_input(path: str, name: str, stem: str, text: str) -> bool:
    if path in text or name in text:
        return True
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(stem)}(?![A-Za-z0-9_])", text) is not None


def _add_include_closure(
    files: Mapping[str, SourceFile],
    included: set[str],
    reasons: dict[str, set[str]],
) -> None:
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in files:
        by_name[PurePosixPath(path).name].append(path)
    queue = deque(path for path in included if _is_code_path(path))
    scanned: set[str] = set()
    while queue:
        source_path = queue.popleft()
        if source_path in scanned or source_path not in files:
            continue
        scanned.add(source_path)
        parent = PurePosixPath(source_path).parent
        for match in _INCLUDE_PATTERN.finditer(files[source_path].content):
            value = match.group(1) or match.group(2)
            candidates = []
            direct = (parent / value).as_posix()
            if direct in files:
                candidates.append(direct)
            root_relative = PurePosixPath(value).as_posix()
            if root_relative in files:
                candidates.append(root_relative)
            candidates.extend(by_name.get(PurePosixPath(value).name, ()))
            for candidate in sorted(set(candidates)):
                if not _is_header_path(candidate):
                    continue
                if candidate not in included:
                    included.add(candidate)
                    queue.append(candidate)
                reasons[candidate].add(f"included_by:{source_path}")


def _lab4_input_parameters(
    source_file: SourceFile | None,
    lab_id: str,
) -> tuple[Mapping[str, Any], str | None]:
    cpu = 60 if lab_id == "lab4-cpu" else 16
    default_mpi = 30 if lab_id == "lab4-cpu" else 1
    values: dict[str, int] = {}
    if source_file is not None:
        if source_file.truncated:
            return {}, "AMSS_NCKU_Input.py is truncated"
        try:
            tree = ast.parse(source_file.content, filename=source_file.path)
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not isinstance(target, ast.Name) or target.id not in {
                        "MPI_processes",
                        "OMP_threads",
                    }:
                        continue
                    value = ast.literal_eval(node.value)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(f"{target.id} is not an integer literal")
                    values[target.id] = value
        except (SyntaxError, ValueError) as error:
            return {}, f"cannot validate AMSS_NCKU_Input.py: {error}"
    mpi = values.get("MPI_processes", default_mpi)
    omp = values.get("OMP_threads", 1)
    valid = mpi >= 1 and omp >= 1 and mpi <= cpu and omp <= cpu and mpi * omp <= cpu
    return {
        "MPI_processes": mpi,
        "OMP_threads": omp,
        "admitted_cpu": cpu,
        "valid_product": valid,
        "other_assignments_ignored_by_judge": True,
    }, None if valid else "MPI/OpenMP values exceed the selected track CPU allocation"


def _critical_configuration(
    files: Mapping[str, SourceFile],
    course_baselines: Mapping[str, DeltaBaseline],
) -> Mapping[str, Any]:
    submission_values: dict[str, str | None] = {}
    course_values: dict[str, str | None] = {}
    for path in _COURSE_CRITICAL_PATHS:
        source = files.get(path)
        baseline = course_baselines.get(path)
        if source is not None and not source.truncated:
            submission_values.update(_preprocessor_definitions(source.content))
        if baseline is not None:
            course_values.update(_preprocessor_definitions(baseline.text))
    values = {
        name: {
            "submission": submission_values.get(name),
            "course": course_values.get(name),
            "matches_course": submission_values.get(name) == course_values.get(name),
        }
        for name in _COURSE_CRITICAL_MACROS
        if name in submission_values or name in course_values
    }
    return {
        "complete": all(
            path in files and not files[path].truncated for path in _COURSE_CRITICAL_PATHS
        ),
        "all_present_values_match_course": all(
            value["matches_course"] for value in values.values()
        ),
        "macros": values,
    }


def _preprocessor_definitions(value: str) -> dict[str, str | None]:
    definitions: dict[str, str | None] = {}
    for match in re.finditer(
        r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)"
        r"(?:[ \t]+([^/\r\n]+?))?[ \t]*(?://.*)?$",
        value,
    ):
        definitions[match.group(1)] = (match.group(2) or "").strip() or None
    return definitions


def _reference_match(
    files: tuple[SourceFile, ...],
    reference_files: Mapping[str, str],
) -> dict[str, Any]:
    exact_count = 0
    exact_bytes = 0
    comparable_count = 0
    comparable_bytes = 0
    for file in files:
        reference = reference_files.get(file.path)
        if reference is None:
            continue
        comparable_count += 1
        comparable_bytes += file.declared_bytes
        if _normalized_text_digest(file.content) == _normalized_text_digest(reference):
            exact_count += 1
            exact_bytes += file.declared_bytes
    return {
        "exact_file_count": exact_count,
        "exact_bytes": exact_bytes,
        "comparable_file_count": comparable_count,
        "comparable_bytes": comparable_bytes,
        "exact_ratio": exact_count / len(files) if files else 0.0,
    }


def _normalized_text_digest(value: str) -> bytes:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode()).digest()


def _has_truncation(bundle: SourceBundle) -> bool:
    return any(file.truncated for file in bundle.files)


def _is_compiled_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _COMPILED_SUFFIXES


def _is_header_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _HEADER_SUFFIXES


def _is_code_path(path: str) -> bool:
    return _is_compiled_path(path) or _is_header_path(path)
