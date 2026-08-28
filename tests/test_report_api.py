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
                    "evidence": {
                        "incomplete": decision == "inconclusive",
                        "baseline_delta": {"truncated_file_count": 2},
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
    assert report["evidence"] == {
        "incomplete": False,
        "baseline_delta": {"truncated_file_count": 2},
    }


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
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
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


def test_api_lists_allowed_models_and_requires_an_allowed_model_for_review(tmp_path) -> None:
    write_report(tmp_path, decision="compliant")
    launcher = FakeLauncher()
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path, refresh_seconds=0),
        launcher,
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
    )

    models = api.handle("GET", "/v1/compliance/models")
    missing_model = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_id": SUBMISSION_ID}).encode(),
    )
    unknown_model = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps(
            {"submission_id": SUBMISSION_ID, "model": "unapproved-model"}
        ).encode(),
    )
    accepted = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps(
            {"submission_id": SUBMISSION_ID, "model": "gpt-5.6-luna"}
        ).encode(),
    )

    assert models.status == 200
    assert json.loads(models.body) == {"models": ["glm-5.3", "gpt-5.6-luna"]}
    assert missing_model.status == 400
    assert unknown_model.status == 400
    assert json.loads(unknown_model.body)["code"] == "MODEL_NOT_ALLOWED"
    assert accepted.status == 200
    assert launcher.requests == [(SUBMISSION_ID, "gpt-5.6-luna")]
    assert json.loads(accepted.body)["compliant"] is True


def test_api_post_reports_failed_review_without_claiming_compliance(tmp_path) -> None:
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path, refresh_seconds=0),
        FailingLauncher(),
        allowed_models=("glm-5.3",),
    )

    response = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_id": SUBMISSION_ID, "model": "glm-5.3"}).encode(),
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
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    result = launcher.launch(SUBMISSION_ID, "gpt-5.6-luna")

    assert result.error is None
    assert runner.request.submission_id == SUBMISSION_ID
    assert runner.request.min_score == 0
    assert runner.request.labs == ()
    assert runner.request.owners == ()
    assert runner.request.execute_reviews is True
    assert runner.request.prompt_version == "compliance-v3"
    assert runner.request.model == "gpt-5.6-luna"



class FakeLauncher:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        self.requests.append((submission_id, model))
        return ReviewLaunchResult("manual-run")


class FailingLauncher:
    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        return ReviewLaunchResult("manual-run", error="ReviewError")


class CapturingRunner:
    def __init__(self) -> None:
        self.request = None

    def run(self, request) -> None:
        self.request = request
