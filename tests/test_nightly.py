from datetime import UTC, datetime, timedelta

from oj_checker.catalog import InMemorySubmissionCatalog
from oj_checker.domain import Submission
from oj_checker.nightly import NIGHTLY_MODEL, NightlyReviewRunner


def test_nightly_skips_only_current_approved_compliant_reports_and_continues_failures() -> None:
    catalog = InMemorySubmissionCatalog(
        [
            submission("alice", 100),
            submission("bob", 90),
            submission("carol", 80),
            submission("dave", 70),
        ]
    )
    client = FakeComplianceClient(
        reports={
            "alice": report("alice", decision="compliant", model="gpt-5.6-luna"),
            "bob": report("bob", decision="compliant", prompt_version="compliance-v1"),
            "carol": report("carol", decision="violation"),
            "dave": RuntimeError("report API unavailable"),
        }
    )
    runner = NightlyReviewRunner(
        catalog,
        client,
        basis_commit="basis-current",
        clock=lambda: datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
    )

    summary = runner.run()

    assert summary.candidate_count == 4
    assert summary.skipped_count == 1
    assert summary.reviewed_count == 2
    assert summary.failed_count == 1
    assert client.review_requests == [("bob", NIGHTLY_MODEL), ("carol", NIGHTLY_MODEL)]


def test_nightly_has_no_review_call_budget() -> None:
    candidates = [submission(f"student-{index:03}", 100) for index in range(150)]
    client = FakeComplianceClient(reports={item.id: None for item in candidates})
    runner = NightlyReviewRunner(
        InMemorySubmissionCatalog(candidates),
        client,
        basis_commit="basis-current",
        clock=lambda: datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
    )

    summary = runner.run()

    assert summary.candidate_count == 150
    assert summary.reviewed_count == 150
    assert summary.failed_count == 0
    assert len(client.review_requests) == 150


class FakeComplianceClient:
    def __init__(self, reports: dict[str, dict | Exception | None]) -> None:
        self._reports = reports
        self.review_requests: list[tuple[str, str]] = []

    def allowed_models(self) -> tuple[str, ...]:
        return ("glm-5.3", "gpt-5.6-luna")

    def report(self, submission_id: str) -> dict | None:
        value = self._reports[submission_id]
        if isinstance(value, Exception):
            raise value
        return value

    def review(self, submission_id: str, model: str) -> dict:
        self.review_requests.append((submission_id, model))
        return report(submission_id, decision="compliant", model=model)


def report(
    submission_id: str,
    *,
    decision: str,
    model: str = "glm-5.3",
    prompt_version: str = "compliance-v3",
) -> dict:
    return {
        "submission": {"id": submission_id},
        "state": "completed",
        "decision": decision,
        "provenance": {
            "basis_commit": "basis-current",
            "rules_version": "audit-rules-v1",
            "prompt_version": prompt_version,
            "schema_version": "compliance-result-v1",
            "model": model,
        },
    }


def submission(submission_id: str, score: int) -> Submission:
    return Submission(
        id=submission_id,
        owner=submission_id,
        lab_id="lab2",
        score=score,
        input_digest=f"digest-{submission_id}",
        submitted_at=datetime(2026, 8, 27, tzinfo=UTC) + timedelta(seconds=score),
        input_manifest={},
        lab_definition={},
    )
