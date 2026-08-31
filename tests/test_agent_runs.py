import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oj_checker.agent_runs import AgentRunFailure, FileAgentRunService
from oj_checker.review_bundle import ReviewBundleError, VerifiedReviewBundle

KEY_ID = "plat101-review-2026-01"
PUBLIC_KEY = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
NOW = datetime(2026, 8, 30, 2, 5, tzinfo=UTC)
SUBMISSION_ID = "00000000-0000-4000-8000-000000000001"


def envelope() -> bytes:
    return Path("tests/fixtures/review_bundle_v1.json").read_bytes()


def service(tmp_path: Path, **kwargs: Any) -> FileAgentRunService:
    return FileAgentRunService(
        tmp_path,
        public_keys={KEY_ID: PUBLIC_KEY},
        allowed_models=("glm-5.3", "gpt-5.6-luna"),
        clock=lambda: NOW,
        **kwargs,
    )


def test_create_is_persistent_idempotent_and_queryable_by_submission(tmp_path: Path) -> None:
    runs = service(tmp_path)

    created = runs.create(envelope())
    repeated = runs.create(envelope())
    latest = runs.latest(SUBMISSION_ID)

    assert created["run_id"].startswith("review-")
    assert created["state"] == "queued"
    assert repeated == created
    assert latest == created
    run_root = tmp_path / "agent-runs" / created["run_id"]
    assert (run_root / "request.json").read_bytes() == envelope()
    assert len(list((run_root / "events").glob("*.json"))) == 1


def test_latest_many_returns_requested_runs_and_omits_missing(tmp_path: Path) -> None:
    runs = service(tmp_path)
    created = runs.create(envelope())
    missing = "00000000-0000-4000-8000-000000000002"

    latest = runs.latest_many((SUBMISSION_ID, missing))

    assert latest == {SUBMISSION_ID: created}


def test_create_rejects_tampering_before_writing_a_run(tmp_path: Path) -> None:
    runs = service(tmp_path)
    wire = json.loads(envelope())
    wire["signature"] = ("A" if wire["signature"][0] != "A" else "B") + wire["signature"][1:]
    tampered = json.dumps(wire, separators=(",", ":")).encode()

    with pytest.raises(ReviewBundleError):
        runs.create(tampered)

    assert list((tmp_path / "agent-runs").iterdir()) == []


def test_worker_executes_once_and_persists_terminal_result(tmp_path: Path) -> None:
    executor = CapturingExecutor()
    runs = service(tmp_path, executor=executor, worker_count=1)
    runs.start()
    try:
        created = runs.create(envelope())
        runs._queue.join()
        completed = runs.get(created["run_id"])
    finally:
        runs.close()

    assert completed["state"] == "completed"
    assert completed["result"] == {"report": {"decision": "compliant"}}
    assert executor.calls == 1
    assert executor.submission_id == SUBMISSION_ID


def test_worker_exposes_only_stable_failure_code_and_does_not_retry(tmp_path: Path) -> None:
    executor = FailingExecutor()
    runs = service(tmp_path, executor=executor, worker_count=1)
    runs.start()
    try:
        created = runs.create(envelope())
        runs._queue.join()
        failed = runs.get(created["run_id"])
    finally:
        runs.close()

    assert failed["state"] == "failed"
    assert failed["error_code"] == "MODEL_UNAVAILABLE"
    assert executor.calls == 1
    assert "upstream secret detail" not in str(failed)


def test_recovery_marks_interrupted_run_failed_instead_of_claiming_completion(
    tmp_path: Path,
) -> None:
    first = service(tmp_path)
    created = first.create(envelope())
    first._append_event(created["run_id"], "running")

    recovered = service(tmp_path)
    recovered.start()

    assert recovered.get(created["run_id"])["state"] == "failed"
    assert recovered.get(created["run_id"])["error_code"] == "JOB_LOST"


def test_worker_count_allows_sixteen_workers(tmp_path: Path) -> None:
    runs = service(tmp_path, executor=CapturingExecutor(), worker_count=16)
    runs.start()
    try:
        assert len(runs._threads) == 16
    finally:
        runs.close()


def test_worker_count_rejects_more_than_sixteen_workers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between zero and sixteen"):
        service(tmp_path, executor=CapturingExecutor(), worker_count=17)


class CapturingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.submission_id = ""

    def execute(self, bundle: VerifiedReviewBundle) -> dict[str, Any]:
        self.calls += 1
        submission = bundle.payload["submission"]
        assert isinstance(submission, dict)
        self.submission_id = submission["id"]
        return {"report": {"decision": "compliant"}}


class FailingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _bundle: VerifiedReviewBundle) -> dict[str, Any]:
        self.calls += 1
        try:
            raise RuntimeError("upstream secret detail")
        except RuntimeError as error:
            raise AgentRunFailure("MODEL_UNAVAILABLE") from error
