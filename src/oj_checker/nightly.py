from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from oj_checker.catalog import SubmissionCatalog
from oj_checker.domain import SelectionPolicy, SnapshotRequest

NIGHTLY_MODEL = "glm-5.3"
RULES_VERSION = "audit-rules-v1"
PROMPT_VERSION = "compliance-v2"
SCHEMA_VERSION = "compliance-result-v1"
_MAX_RESPONSE_BYTES = 1 << 20


class ComplianceClient(Protocol):
    def allowed_models(self) -> tuple[str, ...]: ...

    def report(self, submission_id: str) -> dict[str, Any] | None: ...

    def review(self, submission_id: str, model: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class NightlyReviewSummary:
    cutoff: datetime
    candidate_count: int
    skipped_count: int
    reviewed_count: int
    failed_count: int
    failed_submission_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.failed_count == 0 else "partial_failure",
            "model": NIGHTLY_MODEL,
            "cutoff": self.cutoff.isoformat(),
            "candidate_count": self.candidate_count,
            "skipped_count": self.skipped_count,
            "reviewed_count": self.reviewed_count,
            "failed_count": self.failed_count,
            "failed_submission_ids": list(self.failed_submission_ids),
        }


class NightlyReviewRunner:
    def __init__(
        self,
        catalog: SubmissionCatalog,
        client: ComplianceClient,
        *,
        basis_commit: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not basis_commit:
            raise ValueError("basis_commit is required")
        self._catalog = catalog
        self._client = client
        self._basis_commit = basis_commit
        self._clock = clock

    def run(self) -> NightlyReviewSummary:
        cutoff = self._clock()
        allowed_models = self._client.allowed_models()
        if NIGHTLY_MODEL not in allowed_models:
            raise RuntimeError(f"nightly model {NIGHTLY_MODEL!r} is not allowed by report API")
        candidates = self._catalog.snapshot(
            SnapshotRequest(
                SelectionPolicy.BEST_PER_OWNER_LAB,
                cutoff=cutoff,
                min_score=0,
            )
        ).submissions

        skipped = 0
        reviewed = 0
        failed: list[str] = []
        for submission in candidates:
            try:
                current = self._client.report(submission.id)
                if current is not None and self._can_skip(
                    current,
                    submission.id,
                    allowed_models,
                ):
                    skipped += 1
                    continue
                self._client.review(submission.id, NIGHTLY_MODEL)
                reviewed += 1
            except Exception:
                failed.append(submission.id)

        return NightlyReviewSummary(
            cutoff=cutoff,
            candidate_count=len(candidates),
            skipped_count=skipped,
            reviewed_count=reviewed,
            failed_count=len(failed),
            failed_submission_ids=tuple(failed),
        )

    def _can_skip(
        self,
        report: Mapping[str, Any],
        submission_id: str,
        allowed_models: tuple[str, ...],
    ) -> bool:
        submission = report.get("submission")
        provenance = report.get("provenance")
        return (
            isinstance(submission, Mapping)
            and submission.get("id") == submission_id
            and report.get("state") == "completed"
            and report.get("decision") == "compliant"
            and isinstance(provenance, Mapping)
            and provenance.get("basis_commit") == self._basis_commit
            and provenance.get("rules_version") == RULES_VERSION
            and provenance.get("prompt_version") == PROMPT_VERSION
            and provenance.get("schema_version") == SCHEMA_VERSION
            and provenance.get("model") in allowed_models
        )


class HTTPComplianceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        report_timeout_seconds: float = 10,
        review_timeout_seconds: float = 400,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("report API URL must be HTTP(S)")
        if not token.strip():
            raise ValueError("report API token is required")
        self._base_url = base_url
        self._token = token.strip()
        self._report_timeout_seconds = report_timeout_seconds
        self._review_timeout_seconds = review_timeout_seconds

    def allowed_models(self) -> tuple[str, ...]:
        payload = self._request(
            "GET",
            "/v1/compliance/models",
            None,
            self._report_timeout_seconds,
        )
        models = payload.get("models")
        if not isinstance(models, list) or any(not isinstance(model, str) for model in models):
            raise RuntimeError("report API returned an invalid model list")
        return tuple(models)

    def report(self, submission_id: str) -> dict[str, Any] | None:
        try:
            return self._request(
                "GET",
                f"/v1/compliance/submissions/{submission_id}",
                None,
                self._report_timeout_seconds,
            )
        except ComplianceAPIError as error:
            if error.status == 404 and error.code == "REPORT_NOT_FOUND":
                return None
            raise

    def review(self, submission_id: str, model: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/compliance/reviews",
            {"submission_id": submission_id, "model": model},
            self._review_timeout_seconds,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        raw_body = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self._base_url + path,
            data=raw_body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if raw_body is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            payload = error.read(_MAX_RESPONSE_BYTES + 1)
            envelope = _decode_json(payload)
            raise ComplianceAPIError(
                error.code,
                str(envelope.get("code", "")),
            ) from None
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("report API response is too large")
        return _decode_json(payload)


class ComplianceAPIError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"report API returned status {status} ({code})")
        self.status = status
        self.code = code


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("report API returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("report API returned a non-object response")
    return value
