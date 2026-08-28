from datetime import UTC, datetime, timedelta, timezone

from oj_checker.catalog import InMemorySubmissionCatalog
from oj_checker.domain import SelectionPolicy, SnapshotRequest, Submission
from oj_checker.postgres_catalog import PostgresSubmissionCatalog


def test_snapshot_keeps_history_only_for_plagiarism_corpus() -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice-lab2-old", "alice", "lab2", 70, "2026-08-20T10:00:00Z"),
            submission("alice-lab2-best", "alice", "lab2", 90, "2026-08-21T10:00:00Z"),
            submission("alice-lab2-future", "alice", "lab2", 95, "2026-08-23T10:00:00Z"),
            submission("bob-lab2", "bob", "lab2", 85, "2026-08-21T12:00:00Z"),
            submission("alice-lab3", "alice", "lab3", 80, "2026-08-21T14:00:00Z"),
            submission("below-threshold", "carol", "lab2", 59, "2026-08-21T15:00:00Z"),
        ]
    )
    cutoff = datetime(2026, 8, 22, tzinfo=UTC)

    single_review = catalog.snapshot(
        SnapshotRequest(SelectionPolicy.BEST_PER_OWNER_LAB, cutoff=cutoff, min_score=60)
    )
    plagiarism = catalog.snapshot(
        SnapshotRequest(SelectionPolicy.ALL_QUALIFYING, cutoff=cutoff, min_score=60)
    )

    assert [item.id for item in single_review.submissions] == [
        "alice-lab2-best",
        "alice-lab3",
        "bob-lab2",
    ]
    assert [item.id for item in plagiarism.submissions] == [
        "alice-lab2-old",
        "alice-lab2-best",
        "alice-lab3",
        "bob-lab2",
    ]


def test_snapshot_normalizes_cutoff_to_utc() -> None:
    catalog = InMemorySubmissionCatalog([])
    china_standard_time = timezone(timedelta(hours=8))

    snapshot = catalog.snapshot(
        SnapshotRequest(
            SelectionPolicy.ALL_QUALIFYING,
            cutoff=datetime(2026, 8, 25, 10, 30, tzinfo=china_standard_time),
        )
    )

    assert snapshot.cutoff.tzinfo is UTC
    assert snapshot.cutoff.isoformat() == "2026-08-25T02:30:00+00:00"


def test_snapshot_can_select_one_submission() -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice-old", "alice", "lab2", 70, "2026-08-20T10:00:00Z"),
            submission("alice-best", "alice", "lab2", 90, "2026-08-21T10:00:00Z"),
            submission("bob", "bob", "lab2", 85, "2026-08-21T12:00:00Z"),
        ]
    )

    snapshot = catalog.snapshot(
        SnapshotRequest(
            SelectionPolicy.ALL_QUALIFYING,
            cutoff=datetime(2026, 8, 22, tzinfo=UTC),
            submission_ids=("alice-old",),
        )
    )

    assert [item.id for item in snapshot.submissions] == ["alice-old"]


def test_postgres_best_snapshot_uses_authoritative_score_table() -> None:
    query, parameters = PostgresSubmissionCatalog._query(
        SnapshotRequest(
            SelectionPolicy.BEST_PER_OWNER_LAB,
            cutoff=datetime(2026, 8, 22, tzinfo=UTC),
            min_score=0,
            limit=12,
        )
    )

    assert "FROM oj_user_lab_best_scores b" in query
    assert "JOIN oj_submissions s ON s.id = b.submission_id" in query
    assert "JOIN oj_submission_runs r ON r.id = b.submission_run_id" in query
    assert "b.score AS score" in query
    assert query.rstrip().endswith("LIMIT %s")
    assert parameters[-1] == 12


def submission(
    submission_id: str,
    owner: str,
    lab_id: str,
    score: int,
    submitted_at: str,
) -> Submission:
    return Submission(
        id=submission_id,
        owner=owner,
        lab_id=lab_id,
        score=score,
        input_digest=f"digest-{submission_id}",
        submitted_at=datetime.fromisoformat(submitted_at.replace("Z", "+00:00")),
        input_manifest={"files": []},
        lab_definition={},
    )
