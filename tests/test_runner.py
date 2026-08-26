import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from oj_checker.catalog import InMemorySubmissionCatalog
from oj_checker.domain import AuditRequest, AuditTaskKind, Submission
from oj_checker.report_store import FileReportStore
from oj_checker.review_basis import BaselineFile, ReviewBasis
from oj_checker.review_ledger import CompletedReview, FileReviewLedger
from oj_checker.reviewer import ComplianceReviewTask, PlagiarismReviewTask, ReviewTask
from oj_checker.runner import AuditRunner, ReviewPipeline
from oj_checker.similarity import BaselineDeltaBuilder, SimilarityDetector, SimilarityPolicy
from oj_checker.submission_store import (
    SourceBundle,
    SourceFile,
    SourcePolicy,
)


def test_run_keeps_non_best_exact_duplicates_in_plagiarism_tasks(tmp_path) -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice-shared", "alice", 70, "shared", "2026-08-20T10:00:00Z"),
            submission("alice-best", "alice", 100, "alice-best", "2026-08-21T10:00:00Z"),
            submission("bob-shared", "bob", 75, "shared", "2026-08-20T11:00:00Z"),
            submission("bob-best", "bob", 90, "bob-best", "2026-08-21T11:00:00Z"),
        ]
    )
    runner = AuditRunner(
        catalog,
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        git_commit="test-commit",
    )

    summary = runner.run(
        AuditRequest(run_id="known-non-best-copy", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )

    assert summary.task_counts == {
        AuditTaskKind.SINGLE_REVIEW: 2,
        AuditTaskKind.EXACT_DUPLICATE: 1,
    }
    exact_task = next(
        task for task in summary.manifest.tasks if task.kind is AuditTaskKind.EXACT_DUPLICATE
    )
    assert exact_task.submission_ids == ("alice-shared", "bob-shared")
    assert summary.manifest_path.is_file()
    persisted = json.loads(summary.manifest_path.read_text())
    assert persisted["git_commit"] == "test-commit"
    assert persisted["task_counts"] == {"exact_duplicate": 1, "single_review": 2}
    assert [item["id"] for item in persisted["submissions"]] == [
        "alice-shared",
        "alice-best",
        "bob-shared",
        "bob-best",
    ]
    assert persisted["submissions"][0]["owner"] == "alice"
    assert persisted["submissions"][0]["input_manifest"] == {"files": []}
    assert persisted["submissions"][0]["active_run"] == {
        "id": 123,
        "state": "Succeeded",
        "result_info": {"track": "cpu"},
        "failure_class": None,
        "failure_reason": None,
        "score": 70,
        "performance": 1.25,
        "finished_at": "2026-08-20T10:05:00+00:00",
    }


def test_exact_duplicate_tasks_are_pairs_between_distinct_owners(tmp_path) -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice-first", "alice", 70, "shared", "2026-08-20T10:00:00Z"),
            submission("alice-repeat", "alice", 80, "shared", "2026-08-21T10:00:00Z"),
            submission("bob-copy", "bob", 85, "shared", "2026-08-21T11:00:00Z"),
            submission("carol-copy", "carol", 90, "shared", "2026-08-21T12:00:00Z"),
        ]
    )
    runner = AuditRunner(
        catalog,
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        git_commit="test-commit",
    )

    summary = runner.run(
        AuditRequest(run_id="pairwise-exact-copy", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )

    pairs = {
        task.submission_ids
        for task in summary.manifest.tasks
        if task.kind is AuditTaskKind.EXACT_DUPLICATE
    }
    assert pairs == {
        ("alice-first", "bob-copy"),
        ("alice-first", "carol-copy"),
        ("bob-copy", "carol-copy"),
    }


def test_single_review_limit_does_not_truncate_plagiarism_corpus(tmp_path) -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice-shared", "alice", 70, "shared", "2026-08-20T10:00:00Z"),
            submission("alice-best", "alice", 100, "alice-best", "2026-08-21T10:00:00Z"),
            submission("bob-shared", "bob", 75, "shared", "2026-08-20T11:00:00Z"),
            submission("bob-best", "bob", 90, "bob-best", "2026-08-21T11:00:00Z"),
        ]
    )
    runner = AuditRunner(
        catalog,
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        git_commit="test-commit",
    )

    summary = runner.run(
        AuditRequest(
            run_id="limited-single-review",
            cutoff=datetime(2026, 8, 22, tzinfo=UTC),
            limit=1,
        )
    )

    assert summary.manifest.single_review_corpus_size == 1
    assert summary.manifest.plagiarism_corpus_size == 4
    assert summary.task_counts[AuditTaskKind.EXACT_DUPLICATE] == 1


def test_run_id_cannot_overwrite_a_different_manifest(tmp_path) -> None:
    catalog = InMemorySubmissionCatalog(
        [submission("alice", "alice", 90, "digest", "2026-08-20T10:00:00Z")]
    )
    report_store = FileReportStore(tmp_path)
    runner = AuditRunner(
        catalog,
        report_store,
        clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        git_commit="test-commit",
    )
    first = runner.run(
        AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )
    original = first.manifest_path.read_bytes()

    retry_runner = AuditRunner(
        catalog,
        report_store,
        clock=lambda: datetime(2026, 8, 25, 2, 31, tzinfo=UTC),
        git_commit="test-commit",
    )
    same = retry_runner.run(
        AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )
    assert same.manifest_path.read_bytes() == original
    assert same.manifest.generated_at == first.manifest.generated_at

    with pytest.raises(FileExistsError, match="different manifest"):
        runner.run(
            AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 21, tzinfo=UTC))
        )

    assert first.manifest_path.read_bytes() == original


def test_runner_requires_a_traceable_git_commit(tmp_path) -> None:
    with pytest.raises(ValueError, match="git_commit"):
        AuditRunner(
            InMemorySubmissionCatalog([]),
            FileReportStore(tmp_path),
            clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
            git_commit="unknown",
        )


def test_formal_run_reviews_each_students_best_and_reuses_exact_completed_identity(
    tmp_path,
) -> None:
    submissions = [
        submission("alice-old", "alice", 70, "alice-old", "2026-08-20T10:00:00Z"),
        submission("alice-best", "alice", 100, "alice-best", "2026-08-21T10:00:00Z"),
        submission("bob-best", "bob", 90, "bob-best", "2026-08-21T11:00:00Z"),
    ]
    source_by_id = {
        "alice-old": near_source("100"),
        "alice-best": "int optimize() { return 7; }\n",
        "bob-best": near_source("128"),
    }
    reviewer = CountingReviewer()
    pipeline = ReviewPipeline(
        submission_store=MappingSubmissionStore(source_by_id),
        basis_provider=StaticBasisProvider(),
        delta_builder=BaselineDeltaBuilder(),
        similarity_detector=SimilarityDetector(),
        similarity_policy=SimilarityPolicy(jaccard_threshold=0.7, shingle_size=3),
        reviewer=reviewer,
        ledger=FileReviewLedger(tmp_path),
        source_policy=SourcePolicy(),
        model_parameters=(("temperature", 0),),
    )
    runner = AuditRunner(
        InMemorySubmissionCatalog(submissions),
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        git_commit="test-commit",
        review_pipeline=pipeline,
    )
    request = AuditRequest(
        run_id="formal-first",
        cutoff=datetime(2026, 8, 22, tzinfo=UTC),
        labs=("lab4-cpu",),
        model="gpt-5.6-luna",
        execute_reviews=True,
    )

    first = runner.run(request)
    second = runner.run(
        AuditRequest(
            run_id="formal-second",
            cutoff=request.cutoff,
            labs=request.labs,
            model=request.model,
            execute_reviews=True,
        )
    )
    resumed_first = runner.run(request)

    assert set(reviewer.compliance_submission_ids) == {"alice-best", "bob-best"}
    assert reviewer.plagiarism_submission_ids == [("alice-old", "bob-best")]
    assert first.llm_call_count == 3
    assert first.cache_hit_count == 0
    assert len(list((tmp_path / "owners" / "alice").glob("*.json"))) == 1
    assert len(list((tmp_path / "owners" / "bob").glob("*.json"))) == 1
    plagiarism_reports = list((tmp_path / "plagiarism" / "lab4-cpu").glob("*.json"))
    assert len(plagiarism_reports) == 1
    plagiarism_report = json.loads(plagiarism_reports[0].read_text())
    assert plagiarism_report["submitted_at"] == [
        "2026-08-20T10:00:00+00:00",
        "2026-08-21T11:00:00+00:00",
    ]
    assert plagiarism_report["similarity_signal"] == "minhash"
    assert plagiarism_report["jaccard"] >= 0.7
    assert plagiarism_report["human_review_status"] == "pending"
    assert second.llm_call_count == 0
    assert second.cache_hit_count == 3
    assert second.completed_review_count == 3
    assert resumed_first.manifest.run_id == "formal-first"
    assert len(reviewer.compliance_submission_ids) == 2
    assert len(reviewer.plagiarism_submission_ids) == 1


def test_single_submission_review_does_not_generate_plagiarism_tasks(tmp_path) -> None:
    submissions = [
        submission("alice-old", "alice", 70, "alice-old", "2026-08-20T10:00:00Z"),
        submission("alice-best", "alice", 100, "alice-best", "2026-08-21T10:00:00Z"),
        submission("bob-best", "bob", 90, "bob-best", "2026-08-21T11:00:00Z"),
    ]
    reviewer = CountingReviewer()
    pipeline = ReviewPipeline(
        submission_store=MappingSubmissionStore(
            {item.id: near_source("100") for item in submissions}
        ),
        basis_provider=StaticBasisProvider(),
        delta_builder=BaselineDeltaBuilder(),
        similarity_detector=SimilarityDetector(),
        similarity_policy=SimilarityPolicy(jaccard_threshold=0.7, shingle_size=3),
        reviewer=reviewer,
        ledger=FileReviewLedger(tmp_path),
        source_policy=SourcePolicy(),
        model_parameters=(),
    )
    runner = AuditRunner(
        InMemorySubmissionCatalog(submissions),
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        git_commit="test-commit",
        review_pipeline=pipeline,
    )

    summary = runner.run(
        AuditRequest(
            run_id="single-submission-review",
            cutoff=datetime(2026, 8, 22, tzinfo=UTC),
            submission_id="alice-old",
            min_score=0,
            model="gpt-5.6-luna",
            execute_reviews=True,
        )
    )

    assert reviewer.compliance_submission_ids == ["alice-old"]
    assert reviewer.plagiarism_submission_ids == []
    assert summary.manifest.single_review_corpus_size == 1
    assert summary.manifest.plagiarism_corpus_size == 0
    assert summary.task_counts == {AuditTaskKind.SINGLE_REVIEW: 1}
    assert summary.manifest.review_configuration["single_submission_id"] == "alice-old"


def test_single_submission_review_rejects_missing_submission(tmp_path) -> None:
    runner = AuditRunner(
        InMemorySubmissionCatalog([]),
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        git_commit="test-commit",
    )

    with pytest.raises(LookupError, match="not reviewable"):
        runner.run(
            AuditRequest(
                run_id="missing-single-submission",
                cutoff=datetime(2026, 8, 22, tzinfo=UTC),
                submission_id="missing",
            )
        )


def submission(
    submission_id: str,
    owner: str,
    score: int,
    digest: str,
    submitted_at: str,
) -> Submission:
    return Submission(
        id=submission_id,
        owner=owner,
        lab_id="lab4-cpu",
        score=score,
        input_digest=digest,
        submitted_at=datetime.fromisoformat(submitted_at.replace("Z", "+00:00")),
        input_manifest={"files": []},
        lab_definition={},
        active_run_id=123,
        run_state="Succeeded",
        run_result_info={"track": "cpu"},
        run_score=score,
        run_performance=1.25,
        run_finished_at=datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        .replace(minute=5),
    )


def near_source(limit: str) -> str:
    return (
        f"int optimize() {{ int sum = 0; for (int i = 0; i < {limit}; ++i) "
        "{ sum += i * i; } return sum; }\n"
    )


class MappingSubmissionStore:
    def __init__(self, source_by_id: Mapping[str, str]) -> None:
        self._source_by_id = source_by_id

    def load_bundle(self, submission: Submission, policy: SourcePolicy) -> SourceBundle:
        content = self._source_by_id[submission.id]
        file = SourceFile(
            path="kernel.cpp",
            declared_bytes=len(content.encode()),
            declared_sha256=None,
            content=content,
            bytes_read=len(content.encode()),
            omission_reason=None,
        )
        return SourceBundle(submission.id, (file,), file.bytes_read)


class StaticBasisProvider:
    upstream_commit = "upstream-commit"

    def load(self, lab_id: str) -> ReviewBasis:
        content = "int baseline = 0;\n"
        return ReviewBasis(
            lab_id=lab_id,
            upstream_commit=self.upstream_commit,
            source_path="src/lab4",
            document_path="docs/lab/Lab4-AMSS-NCKU/index.md",
            files=(BaselineFile("kernel.cpp", content.encode(), "baseline-file-digest"),),
            tree_digest="tree-digest",
            document="# Lab4\nDo not skip required stages.\n",
            document_digest="document-digest",
        )


class CountingReviewer:
    def __init__(self) -> None:
        self.compliance_submission_ids: list[str] = []
        self.plagiarism_submission_ids: list[tuple[str, str]] = []

    def review(self, task: ReviewTask) -> CompletedReview:
        if isinstance(task, ComplianceReviewTask):
            self.compliance_submission_ids.append(task.identity.submission_ids[0])
            verdict: dict[str, Any] = {
                "decision": "compliant",
                "confidence": 0.9,
                "violations": [],
                "summary": "No violation found.",
                "requires_human_review": False,
            }
        elif isinstance(task, PlagiarismReviewTask):
            self.plagiarism_submission_ids.append(task.identity.submission_ids)
            verdict = {
                "decision": "independent",
                "relationship": "independent",
                "confidence": 0.8,
                "evidence": [],
                "summary": "Insufficient uncommon overlap.",
                "requires_human_review": False,
            }
        else:
            raise AssertionError("unexpected review task")
        return CompletedReview(
            identity=task.identity,
            completed_at=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
            verdict=verdict,
            model_response_digest="response-digest",
            conclusive=True,
        )
