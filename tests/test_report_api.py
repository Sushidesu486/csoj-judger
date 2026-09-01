import json
from datetime import UTC, datetime
from pathlib import Path

from oj_checker.agent_runs import FileAgentRunService
from oj_checker.report_api import (
    ComplianceApi,
    FileComplianceReportReader,
    FilePlagiarismReportReader,
    ReviewLaunchResult,
    RunnerReviewLauncher,
)

SUBMISSION_ID = "258fb85f-897b-4f70-9a5b-b5cbf2cf91ea"
COUNTERPART_ID = "11111111-1111-4111-8111-111111111111"


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


def write_plagiarism_report(
    root,
    *,
    review_key="b" * 64,
    decision="plagiarism",
    completed_at="2026-08-25T12:37:02+00:00",
):
    target = root / "plagiarism" / "lab2"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{review_key[:16]}__response.json").write_text(
        json.dumps(
            {
                "human_review_status": "pending",
                "jaccard": 0.91,
                "kind": "plagiarism_review",
                "owners": ["alice", "bob"],
                "review": {
                    "review_key": review_key,
                    "completed_at": completed_at,
                    "conclusive": decision != "inconclusive",
                    "identity": {
                        "task_type": "plagiarism",
                        "submission_ids": [SUBMISSION_ID, COUNTERPART_ID],
                        "lab_id": "lab2",
                        "model": "glm-5.3",
                    },
                    "verdict": {
                        "decision": decision,
                        "relationship": "minor_edit",
                        "confidence": 0.96,
                        "summary": "same uncommon control flow",
                        "evidence": [
                            {
                                "first_path": "first/a.cpp",
                                "second_path": "second/b.cpp",
                                "description": "matching uncommon branch structure",
                            }
                        ],
                    },
                },
                "similarity_signal": "minhash",
                "submission_ids": [SUBMISSION_ID, COUNTERPART_ID],
                "submitted_at": [
                    "2026-08-24T10:00:00+00:00",
                    "2026-08-24T11:00:00+00:00",
                ],
                "task_key": review_key,
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


def test_plagiarism_reader_indexes_both_submissions_and_orients_evidence(tmp_path) -> None:
    write_plagiarism_report(tmp_path)
    reader = FilePlagiarismReportReader(tmp_path, refresh_seconds=0)

    first = reader.get_submission_reports(SUBMISSION_ID)
    second = reader.get_submission_reports(COUNTERPART_ID)

    assert len(first) == 1
    assert first[0]["counterpart"] == {
        "submission_id": COUNTERPART_ID,
        "owner": "bob",
        "submitted_at": "2026-08-24T11:00:00+00:00",
    }
    assert first[0]["evidence"] == [
        {
            "submission_path": "first/a.cpp",
            "counterpart_path": "second/b.cpp",
            "description": "matching uncommon branch structure",
        }
    ]
    assert second[0]["counterpart"]["submission_id"] == SUBMISSION_ID
    assert second[0]["counterpart"]["owner"] == "alice"
    assert second[0]["evidence"][0]["submission_path"] == "second/b.cpp"
    assert second[0]["evidence"][0]["counterpart_path"] == "first/a.cpp"


def test_plagiarism_reader_keeps_latest_review_per_pair(tmp_path) -> None:
    write_plagiarism_report(
        tmp_path,
        review_key="a" * 64,
        decision="independent",
        completed_at="2026-08-25T10:00:00+00:00",
    )
    write_plagiarism_report(
        tmp_path,
        review_key="b" * 64,
        decision="plagiarism",
        completed_at="2026-08-25T12:00:00+00:00",
    )

    reports = FilePlagiarismReportReader(tmp_path, refresh_seconds=0).get_submission_reports(
        SUBMISSION_ID
    )

    assert len(reports) == 1
    assert reports[0]["review_key"] == "b" * 64
    assert reports[0]["decision"] == "plagiarism"


def test_plagiarism_reader_ignores_malformed_and_symlinked_reports(tmp_path) -> None:
    target = tmp_path / "plagiarism" / "lab2"
    target.mkdir(parents=True)
    malformed = target / "malformed.json"
    malformed.write_text("{}")
    (target / "linked.json").symlink_to(malformed)

    reports = FilePlagiarismReportReader(tmp_path, refresh_seconds=0).get_submission_reports(
        SUBMISSION_ID
    )

    assert reports == []


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


def test_api_returns_read_only_plagiarism_reports_for_one_submission(tmp_path) -> None:
    write_plagiarism_report(tmp_path)
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path, refresh_seconds=0),
        FakeLauncher(),
        allowed_models=("glm-5.3",),
        auth_token="secret",
        plagiarism_reader=FilePlagiarismReportReader(tmp_path, refresh_seconds=0),
    )
    path = f"/v1/plagiarism/submissions/{SUBMISSION_ID}"

    unauthorized = api.handle("GET", path)
    found = api.handle("GET", path, {"Authorization": "Bearer secret"})
    missing = api.handle(
        "GET",
        "/v1/plagiarism/submissions/22222222-2222-4222-8222-222222222222",
        {"Authorization": "Bearer secret"},
    )
    mutation = api.handle("POST", path, {"Authorization": "Bearer secret"})
    invalid = api.handle(
        "GET", "/v1/plagiarism/submissions/not-a-uuid", {"Authorization": "Bearer secret"}
    )

    assert unauthorized.status == 401
    assert found.status == 200
    assert json.loads(found.body)["items"][0]["decision"] == "plagiarism"
    assert missing.status == 200
    assert json.loads(missing.body)["items"] == []
    assert mutation.status == 405
    assert invalid.status == 400


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
        body=json.dumps({"submission_id": SUBMISSION_ID, "model": "unapproved-model"}).encode(),
    )
    accepted = api.handle(
        "POST",
        "/v1/compliance/reviews",
        body=json.dumps({"submission_id": SUBMISSION_ID, "model": "gpt-5.6-luna"}).encode(),
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


def test_api_accepts_signed_agent_run_and_exposes_polling_endpoints(tmp_path) -> None:
    now = datetime(2026, 8, 30, 2, 5, tzinfo=UTC)
    runs = FileAgentRunService(
        tmp_path,
        public_keys={
            "plat101-review-2026-01": bytes.fromhex(
                "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
            )
        },
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
        clock=lambda: now,
    )
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path, refresh_seconds=0),
        FakeLauncher(),
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
        auth_token="secret",
        agent_runs=runs,
    )
    envelope = Path("tests/fixtures/review_bundle_v1.json").read_bytes()

    accepted = api.handle(
        "POST",
        "/v1/compliance/review-runs",
        {"Authorization": "Bearer secret"},
        envelope,
    )
    created = json.loads(accepted.body)
    polled = api.handle(
        "GET",
        f"/v1/compliance/review-runs/{created['run_id']}",
        {"Authorization": "Bearer secret"},
    )
    latest = api.handle(
        "GET",
        "/v1/compliance/submissions/00000000-0000-4000-8000-000000000001/review-runs/latest",
        {"Authorization": "Bearer secret"},
    )
    batch = api.handle(
        "POST",
        "/v1/compliance/review-runs/latest:batch",
        {"Authorization": "Bearer secret"},
        json.dumps(
            {
                "submission_ids": [
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                ]
            }
        ).encode(),
    )

    assert accepted.status == 202
    assert created["state"] == "queued"
    assert polled.status == 200
    assert json.loads(polled.body)["run_id"] == created["run_id"]
    assert latest.status == 200
    assert json.loads(latest.body)["run_id"] == created["run_id"]
    assert batch.status == 200
    assert json.loads(batch.body) == {
        "items": [
            {
                "created_at": created["created_at"],
                "model": created["model"],
                "run_id": created["run_id"],
                "source": created["source"],
                "state": "queued",
                "submission_id": "00000000-0000-4000-8000-000000000001",
                "updated_at": created["updated_at"],
            }
        ]
    }


def test_api_rejects_unsigned_agent_run_without_exposing_details(tmp_path) -> None:
    runs = FileAgentRunService(
        tmp_path,
        public_keys={"plat101-review-2026-01": b"p" * 32},
        allowed_models=("gpt-5.6-luna",),
        clock=lambda: datetime(2026, 8, 30, 2, 5, tzinfo=UTC),
    )
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path),
        FakeLauncher(),
        allowed_models=("gpt-5.6-luna",),
        agent_runs=runs,
    )

    response = api.handle(
        "POST",
        "/v1/compliance/review-runs",
        body=Path("tests/fixtures/review_bundle_v1.json").read_bytes(),
    )

    assert response.status == 400
    assert json.loads(response.body) == {
        "code": "INVALID_REVIEW_BUNDLE",
        "message": "review bundle verification failed",
    }


def test_api_exposes_asynchronous_plagiarism_run_endpoints(tmp_path) -> None:
    runs = FakePlagiarismRuns()
    api = ComplianceApi(
        FileComplianceReportReader(tmp_path),
        FakeLauncher(),
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
        plagiarism_runs=runs,
    )

    accepted = api.handle("POST", "/v1/plagiarism/review-runs", body=b"signed-envelope")
    created = json.loads(accepted.body)
    polled = api.handle("GET", f"/v1/plagiarism/review-runs/{created['run_id']}")
    latest = api.handle(
        "GET",
        "/v1/plagiarism/submissions/00000000-0000-4000-8000-000000000001/review-runs/latest",
    )

    assert accepted.status == 202
    assert runs.envelope == b"signed-envelope"
    assert polled.status == 200
    assert latest.status == 200
    assert json.loads(latest.body)["run_id"] == created["run_id"]


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
    assert runner.request.prompt_version == "compliance-v5"
    assert runner.request.model == "gpt-5.6-luna"


class FakeLauncher:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        self.requests.append((submission_id, model))
        return ReviewLaunchResult("manual-run")


class FakePlagiarismRuns:
    def __init__(self) -> None:
        self.envelope = b""
        digest = "d" * 64
        self.run = {
            "schema_version": 1,
            "run_id": f"plagiarism-{digest}",
            "payload_digest": digest,
            "submission_id": "00000000-0000-4000-8000-000000000001",
            "model": "gpt-5.6-luna",
            "source": "manual",
            "state": "queued",
            "created_at": "2026-08-30T02:05:00+00:00",
            "updated_at": "2026-08-30T02:05:00+00:00",
        }

    def create(self, envelope: bytes) -> dict[str, object]:
        self.envelope = envelope
        return dict(self.run)

    def get(self, run_id: str) -> dict[str, object]:
        if run_id != self.run["run_id"]:
            raise LookupError(run_id)
        return dict(self.run)

    def latest(self, submission_id: str) -> dict[str, object] | None:
        if submission_id != self.run["submission_id"]:
            return None
        return dict(self.run)


class FailingLauncher:
    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        return ReviewLaunchResult("manual-run", error="ReviewError")


class CapturingRunner:
    def __init__(self) -> None:
        self.request = None

    def run(self, request) -> None:
        self.request = request
