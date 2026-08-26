import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from oj_checker.domain import Submission
from oj_checker.review_basis import BaselineFile, ReviewBasis
from oj_checker.similarity import (
    BaselineDeltaBuilder,
    SimilarityDetector,
    SimilarityDocument,
    SimilarityPolicy,
    SimilaritySignal,
)
from oj_checker.submission_store import OmissionReason, SourceBundle, SourceFile


def test_baseline_delta_keeps_only_real_student_changes() -> None:
    basis = review_basis(
        {
            "src/kernel.cpp": "int answer() {\n  return 1;\n}\n",
            "run.sh": "python baseline.py\n",
        }
    )
    bundle = source_bundle(
        {
            "src/kernel.cpp": "int answer() {\n  return 42;\n}\n",
            "run.sh": "python baseline.py\n",
            "student/new.cu": "__global__ void solve() {}\n",
        }
    )

    delta = BaselineDeltaBuilder().build(bundle, basis)

    assert [file.path for file in delta.files] == ["src/kernel.cpp", "student/new.cu"]
    assert delta.files[0].added_text == "  return 42;\n"
    assert delta.files[0].removed_text == "  return 1;\n"
    assert delta.files[1].added_text == "__global__ void solve() {}\n"
    assert delta.files[1].removed_text == ""
    assert not delta.incomplete


def test_detector_ignores_public_baseline_and_finds_near_delta_across_owners() -> None:
    basis = review_basis({"kernel.cpp": "int baseline = 0;\n"})
    baseline_only = "int baseline = 0;\n"
    alice_source = (
        "int optimize() { int sum = 0; for (int i = 0; i < 100; ++i) "
        "{ sum += i * i; } return sum; }\n"
    )
    bob_source = alice_source.replace("100", "128")
    builder = BaselineDeltaBuilder()
    documents = [
        similarity_document("baseline-a", "alice", "digest-a", baseline_only, builder, basis),
        similarity_document("baseline-b", "bob", "digest-b", baseline_only, builder, basis),
        similarity_document("alice-near", "alice", "digest-c", alice_source, builder, basis),
        similarity_document("bob-near", "bob", "digest-d", bob_source, builder, basis),
    ]

    result = SimilarityDetector().detect(
        documents,
        SimilarityPolicy(jaccard_threshold=0.7, shingle_size=3),
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.submission_ids == ("alice-near", "bob-near")
    assert candidate.signal is SimilaritySignal.MINHASH
    assert candidate.jaccard >= 0.7


def test_baseline_delta_does_not_treat_budget_truncation_as_a_student_change() -> None:
    basis = review_basis({"large.cpp": "first line\nsecond line\nthird line\n"})
    truncated = SourceFile(
        path="large.cpp",
        declared_bytes=34,
        declared_sha256=None,
        content="first line\n",
        bytes_read=11,
        omission_reason=OmissionReason.FILE_BUDGET,
    )

    delta = BaselineDeltaBuilder().build(
        SourceBundle("submission-id", (truncated,), truncated.bytes_read),
        basis,
    )

    assert delta.files == ()
    assert delta.incomplete


def test_baseline_delta_records_a_missing_required_baseline_file_as_removed() -> None:
    basis = review_basis(
        {
            "student/required.cpp": "void required_stage() {}\n",
            "checker/internal.cpp": "void checker_only() {}\n",
        }
    )
    bundle = SourceBundle(
        submission_id="submission-id",
        files=(),
        total_bytes_read=0,
        declared_paths=(),
        required_patterns=("student/required.cpp",),
        allowed_patterns=("checker/internal.cpp",),
    )

    delta = BaselineDeltaBuilder().build(bundle, basis)

    assert [(file.path, file.added_text, file.removed_text) for file in delta.files] == [
        ("checker/internal.cpp", "", "void checker_only() {}\n"),
        ("student/required.cpp", "", "void required_stage() {}\n"),
    ]


def test_detector_does_not_make_near_candidates_from_incomplete_deltas() -> None:
    basis = review_basis({"kernel.cpp": "int baseline = 0;\n"})
    builder = BaselineDeltaBuilder()
    first = similarity_document("first", "alice", "input-a", near_source("100"), builder, basis)
    second = similarity_document("second", "bob", "input-b", near_source("128"), builder, basis)
    documents = [
        replace(first, delta=replace(first.delta, incomplete=True)),
        replace(second, delta=replace(second.delta, incomplete=True)),
    ]

    result = SimilarityDetector().detect(
        documents,
        SimilarityPolicy(jaccard_threshold=0.7, shingle_size=3),
    )

    assert result.candidates == ()
    assert [item.submission_id for item in result.exclusions] == ["first", "second"]
    assert result.exclusions[0].skipped_layers == ("exact_delta", "minhash_lsh")


def test_parallel_detector_matches_single_process_candidates() -> None:
    basis = review_basis({"kernel.cpp": "int baseline = 0;\n"})
    builder = BaselineDeltaBuilder()
    documents = [
        similarity_document(
            "alice-near",
            "alice",
            "digest-a",
            near_source("100"),
            builder,
            basis,
        ),
        similarity_document(
            "bob-near",
            "bob",
            "digest-b",
            near_source("128"),
            builder,
            basis,
        ),
        similarity_document(
            "carol-other",
            "carol",
            "digest-c",
            "int unrelated() { return 7; }\n",
            builder,
            basis,
        ),
    ]
    policy = SimilarityPolicy(jaccard_threshold=0.7, shingle_size=3)

    single_process = SimilarityDetector(max_workers=1).detect(documents, policy)
    parallel = SimilarityDetector(max_workers=2).detect(documents, policy)

    assert parallel == single_process


def test_detector_rejects_non_positive_worker_count() -> None:
    with pytest.raises(ValueError, match="max_workers"):
        SimilarityDetector(max_workers=0)


def review_basis(files: dict[str, str]) -> ReviewBasis:
    baseline_files = tuple(
        BaselineFile(path, content.encode(), hashlib.sha256(content.encode()).hexdigest())
        for path, content in sorted(files.items())
    )
    return ReviewBasis(
        lab_id="lab4-cpu",
        upstream_commit="upstream-commit",
        source_path="src/lab4",
        document_path="docs/lab/Lab4-AMSS-NCKU/index.md",
        files=baseline_files,
        tree_digest="tree-digest",
        document="# Lab4\n",
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
        "submission-id",
        source_files,
        sum(file.bytes_read for file in source_files),
    )


def similarity_document(
    submission_id: str,
    owner: str,
    input_digest: str,
    content: str,
    builder: BaselineDeltaBuilder,
    basis: ReviewBasis,
) -> SimilarityDocument:
    bundle = source_bundle({"kernel.cpp": content})
    bundle = SourceBundle(submission_id, bundle.files, bundle.total_bytes_read)
    submission = Submission(
        id=submission_id,
        owner=owner,
        lab_id="lab4-cpu",
        score=90,
        input_digest=input_digest,
        submitted_at=datetime(2026, 8, 25, tzinfo=UTC),
        input_manifest={"files": []},
        lab_definition={},
    )
    return SimilarityDocument(submission, builder.build(bundle, basis))


def near_source(limit: str) -> str:
    return (
        f"int optimize() {{ int sum = 0; for (int i = 0; i < {limit}; ++i) "
        "{ sum += i * i; } return sum; }\n"
    )
