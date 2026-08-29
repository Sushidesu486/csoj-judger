import hashlib
from dataclasses import dataclass

from oj_checker.review_basis import BaselineFile, ReviewBasis
from oj_checker.review_scope import (
    ComplianceReviewScopeBuilder,
    ReferenceSnapshot,
)
from oj_checker.similarity import BaselineDeltaBuilder
from oj_checker.submission_store import SourceBundle, SourceFile


def test_lab4_scope_uses_build_include_closure_and_pinned_public_origin() -> None:
    common = {
        "CMakeLists.txt": "add_executable(ABE src/main.C src/extra.C)\n",
        "compile.sh": "cmake -B build -S . && cmake --build build\n",
        "run.sh": "./build/TwoPunctureABE && ./build/ABE\n",
    }
    basis = review_basis(
        {
            **common,
            "src/main.C": '#include "optimize.h"\nint main() { return optimized(); }\n',
        }
    )
    bundle = source_bundle(
        {
            **common,
            "AMSS_NCKU_Input.py": "MPI_processes = 6\nOMP_threads = 5\nFinal_Time = 1\n",
            "src/main.C": '#include "optimize.h"\nint main() { return optimized(); }\n',
            "src/extra.C": "int public_helper() { return 1; }\n",
            "src/optimize.h": "int optimized();\n",
            "src/unused.C": "int hardcoded_result() { return 42; }\n",
            "src/unused.dat": "precomputed output\n",
        }
    )
    reference = StaticReferenceProvider(
        (
            ReferenceSnapshot(
                repository="public/origin",
                revision="public-commit",
                files={
                    "src/main.C": (
                        '#include "optimize.h"\r\nint main() { return optimized(); }\r\n'
                    ),
                    "src/extra.C": "int public_helper() { return 1; }\n",
                    "src/optimize.h": "int original();\n",
                    "src/unused.C": "int upstream_tool();\n",
                },
                tree_digest="public-tree",
            ),
        )
    )

    scope = ComplianceReviewScopeBuilder(
        BaselineDeltaBuilder(),
        lab4_references=reference,
        minimum_reference_files=1,
        minimum_reference_bytes=1,
        minimum_reference_ratio=0.1,
    ).build("lab4-cpu", bundle, basis)

    assert {file.path for file in scope.source_bundle.files} == {
        "CMakeLists.txt",
        "compile.sh",
        "run.sh",
        "src/extra.C",
        "src/main.C",
        "src/optimize.h",
    }
    assert [file.path for file in scope.delta.files] == ["src/optimize.h"]
    assert scope.delta.files[0].baseline_kind == "public_origin"
    assert scope.delta.files[0].baseline_revision == "public-commit"
    assert scope.diagnostics["complete"] is True
    assert scope.diagnostics["input_parameters"] == {
        "MPI_processes": 6,
        "OMP_threads": 5,
        "admitted_cpu": 60,
        "valid_product": True,
        "other_assignments_ignored_by_judge": True,
    }
    excluded = {
        item["path"]: item["reason"] for item in scope.diagnostics["excluded"]["files"]
    }
    assert excluded == {
        "AMSS_NCKU_Input.py": "judge_reads_only_mpi_omp_literals",
        "src/unused.C": "outside_static_build_include_runtime_closure",
        "src/unused.dat": "outside_static_build_include_runtime_closure",
    }


def test_lab4_scope_falls_back_to_all_compiled_sources_for_dynamic_build() -> None:
    basis = review_basis({})
    bundle = source_bundle(
        {
            "CMakeLists.txt": (
                "aux_source_directory(src SOURCES)\nadd_executable(ABE ${SOURCES})\n"
            ),
            "compile.sh": "cmake -B build -S . && cmake --build build\n",
            "run.sh": "./build/ABE\n",
            "src/main.C": "int main() { return 0; }\n",
            "src/indirect.C": "int indirect() { return 1; }\n",
            "src/unread.dat": "not a compiled source\n",
        }
    )

    scope = ComplianceReviewScopeBuilder(BaselineDeltaBuilder()).build(
        "lab4-gpu", bundle, basis
    )

    selected = {file.path for file in scope.source_bundle.files}
    assert "src/main.C" in selected
    assert "src/indirect.C" in selected
    assert "src/unread.dat" not in selected
    assert (
        scope.diagnostics["fallback_reason"]
        == "dynamic_build_construct_requires_all_compiled_sources"
    )


def test_lab4_scope_keeps_runtime_assets_selected_by_script_glob() -> None:
    bundle = source_bundle(
        {
            "CMakeLists.txt": "add_executable(ABE src/main.C)\n",
            "compile.sh": "cmake -B build -S . && cmake --build build\n",
            "run.sh": "cp src/*.psid run-output/ && ./build/ABE\n",
            "src/main.C": "int main() { return 0; }\n",
            "src/initial.psid": "runtime initial data\n",
            "src/unread.dat": "not referenced\n",
        }
    )

    scope = ComplianceReviewScopeBuilder(BaselineDeltaBuilder()).build(
        "lab4-cpu", bundle, review_basis({})
    )

    selected = {file.path for file in scope.source_bundle.files}
    assert "src/initial.psid" in selected
    assert "src/unread.dat" not in selected


@dataclass(frozen=True, slots=True)
class StaticReferenceProvider:
    snapshots: tuple[ReferenceSnapshot, ...]

    def load(self) -> tuple[ReferenceSnapshot, ...]:
        return self.snapshots


def review_basis(files: dict[str, str]) -> ReviewBasis:
    baseline_files = tuple(
        BaselineFile(path, content.encode(), hashlib.sha256(content.encode()).hexdigest())
        for path, content in sorted(files.items())
    )
    return ReviewBasis(
        lab_id="lab4-cpu",
        upstream_commit="course-commit",
        source_path="src/lab4",
        document_path="docs/lab/Lab4-AMSS-NCKU/index.md",
        files=baseline_files,
        tree_digest="course-tree",
        document="# Lab 4\n",
        document_digest="document-digest",
    )


def source_bundle(files: dict[str, str]) -> SourceBundle:
    source_files = tuple(
        SourceFile(
            path=path,
            declared_bytes=len(content.encode()),
            declared_sha256=None,
            content=content,
            bytes_read=len(content.encode()),
            omission_reason=None,
        )
        for path, content in sorted(files.items())
    )
    return SourceBundle(
        submission_id="submission-id",
        files=source_files,
        total_bytes_read=sum(file.bytes_read for file in source_files),
        declared_paths=tuple(file.path for file in source_files),
        required_patterns=("CMakeLists.txt", "compile.sh", "run.sh", "src/*"),
        allowed_patterns=(
            "CMakeLists.txt",
            "compile.sh",
            "run.sh",
            "AMSS_NCKU_Input.py",
            "src/*",
        ),
    )
