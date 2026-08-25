import json
from datetime import UTC, datetime

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
