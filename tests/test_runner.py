import json
from datetime import UTC, datetime

import pytest

from oj_checker.catalog import InMemorySubmissionCatalog
from oj_checker.domain import AuditRequest, AuditTaskKind, Submission
from oj_checker.report_store import FileReportStore
from oj_checker.runner import AuditRunner


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
    runner = AuditRunner(
        InMemorySubmissionCatalog(
            [submission("alice", "alice", 90, "digest", "2026-08-20T10:00:00Z")]
        ),
        FileReportStore(tmp_path),
        clock=lambda: datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        git_commit="test-commit",
    )
    first = runner.run(
        AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )
    original = first.manifest_path.read_bytes()

    same = runner.run(
        AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 22, tzinfo=UTC))
    )
    assert same.manifest_path.read_bytes() == original

    with pytest.raises(FileExistsError, match="different manifest"):
        runner.run(
            AuditRequest(run_id="immutable-run", cutoff=datetime(2026, 8, 21, tzinfo=UTC))
        )

    assert first.manifest_path.read_bytes() == original


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
    )
