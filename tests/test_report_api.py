import json
from datetime import UTC, datetime

from oj_checker.report_api import (
    ComplianceApi,
    FileComplianceReportReader,
    ReviewLaunchResult,
    RunnerReviewLauncher,
)

SUBMISSION_ID = "258fb85f-897b-4f70-9a5b-b5cbf2cf91ea"


def write_report(root, *, decision="violation", completed_at="2026-08-25T12:37:02+00:00"):
    target = root / "owners" / "alice"
    target.mkdir(parents=True)
    (target / "lab2__report__response.json").write_text(
        json.dumps(
            {
                "kind": "single_review",
                "score": 76,
                "review": {
                    "review_key": "a" * 64,
                    "completed_at": completed_at,
                    "conclusive": decision != "inconclusive",
                    "identity": {
                        "submission_ids": [SUBMISSION_ID],
                        "lab_id": "lab2",
                        "basis_commit": "basis",
                        "rules_version": "audit-rules-v1",
                        "prompt_version": "compliance-v1",
                        "schema_version": "compliance-result-v1",
                        "model": "gpt-5.6-luna",
                    },
                    "verdict": {
                        "decision": decision,
                        "confidence": 0.9,
                        "summary": "review summary",
                        "violations": (
                            []
                            if decision != "violation"
                            else [{"category": "baseline_degradation"}]
                        ),
                    },
                },
            }
        )
    )


def test_reader_returns_normalized_single_submission_report(tmp_path) -> None:
    write_report(tmp_path)

    report = FileComplianceReportReader(
        tmp_path,
        refresh_seconds=0,
    ).get_submission_report(SUBMISSION_ID)

    assert report is not None
    assert report["submission"] == {
        "id": SUBMISSION_ID,
        "owner": "alice",
        "lab_id": "lab2",
        "score": 76,
    }
    assert report["decision"] == "violation"
    assert report["compliant"] is False
    assert report["violations"] == [{"category": "baseline_degradation"}]


def test_reader_ignores_unknown_decision_reports(tmp_path) -> None:
    write_report(tmp_path, decision="not-a-decision")

    report = FileComplianceReportReader(tmp_path, refresh_seconds=0).get_submission_report(
        SUBMISSION_ID
    )

    assert report is None


def test_api_get_requires_one_existing_submission_report(tmp_path) -> None:
    write_report(tmp_path)
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path, refresh_seconds=0),
        FakeLauncher(),
        auth_token="secret",
    )

    unauthorized = api.handle("GET", f"/v1/compliance/submissions/{SUBMISSION_ID}")
    missing = api.handle(
        "GET",
        "/v1/compliance/submissions/11111111-1111-4111-8111-111111111111",
        {"Authorization": "Bearer secret"},
    )
    found = api.handle(
        "GET",
        f"/v1/compliance/submissions/{SUBMISSION_ID}",
        {"Authorization": "Bearer secret"},
    )

    assert unauthorized.status == 401
    assert missing.status == 404
    assert found.status == 200
    assert json.loads(found.body)["decision"] == "violation"


def test_api_post_accepts_only_one_submission_id(tmp_path) -> None:
    write_report(tmp_path, decision="compliant")
    launcher = FakeLauncher()
    api = ComplianceApi(FileComplianceReportReader(tmp_path, refresh_seconds=0), launcher)

    rejected = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_ids": [SUBMISSION_ID]}).encode(),
    )
    accepted = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_id": SUBMISSION_ID}).encode(),
    )

    assert rejected.status == 400
    assert accepted.status == 200
    assert launcher.submission_ids == [SUBMISSION_ID]
    assert json.loads(accepted.body)["compliant"] is True


def test_api_post_reports_failed_review_without_claiming_compliance(tmp_path) -> None:
    api = ComplianceApi(FileComplianceReportReader(tmp_path, refresh_seconds=0), FailingLauncher())

    response = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_id": SUBMISSION_ID}).encode(),
    )

    assert response.status == 502
    assert json.loads(response.body) == {
        "state": "failed",
        "run_id": "manual-run",
        "error": "ReviewError",
    }


def test_runner_launcher_builds_a_single_submission_request() -> None:
    runner = CapturingRunner()
    launcher = RunnerReviewLauncher(
        runner,
        model="gpt-5.6-luna",
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    result = launcher.launch(SUBMISSION_ID)

    assert result.error is None
    assert runner.request.submission_id == SUBMISSION_ID
    assert runner.request.min_score == 0
    assert runner.request.labs == ()
    assert runner.request.owners == ()
    assert runner.request.execute_reviews is True
    assert runner.request.prompt_version == "compliance-v2"



class FakeLauncher:
    def __init__(self) -> None:
        self.submission_ids: list[str] = []

    def launch(self, submission_id: str) -> ReviewLaunchResult:
        self.submission_ids.append(submission_id)
        return ReviewLaunchResult("manual-run")


class FailingLauncher:
    def launch(self, submission_id: str) -> ReviewLaunchResult:
        return ReviewLaunchResult("manual-run", error="ReviewError")


class CapturingRunner:
    def __init__(self) -> None:
        self.request = None

    def run(self, request) -> None:
        self.request = request
