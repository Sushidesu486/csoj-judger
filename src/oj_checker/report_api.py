from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, TypeGuard, cast
from urllib.parse import urlsplit

from oj_checker.agent_runs import AgentRunError, AgentRunQueueFull
from oj_checker.review_bundle import ReviewBundleError

_COMPLIANCE_DECISIONS = frozenset({"compliant", "violation", "inconclusive"})
_PLAGIARISM_DECISIONS = frozenset({"plagiarism", "independent", "inconclusive"})
_PLAGIARISM_RELATIONSHIPS = frozenset(
    {"exact", "near_identical", "minor_edit", "shared_template", "independent", "unclear"}
)
_SIMILARITY_SIGNALS = frozenset(
    {"exact_submission", "exact_delta", "minhash", "exhaustive_jaccard"}
)

_LOGGER = logging.getLogger(__name__)


class ComplianceReportReader(Protocol):
    def get_submission_report(self, submission_id: str) -> dict[str, Any] | None: ...

    def refresh(self) -> None: ...


class PlagiarismReportReader(Protocol):
    def get_submission_reports(self, submission_id: str) -> list[dict[str, Any]]: ...

    def refresh(self) -> None: ...


class ReviewLauncher(Protocol):
    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult: ...


class AgentRunService(Protocol):
    def create(self, envelope: bytes) -> dict[str, Any]: ...

    def get(self, run_id: str) -> dict[str, Any]: ...

    def latest(self, submission_id: str) -> dict[str, Any] | None: ...

    def latest_many(self, submission_ids: Iterable[str]) -> dict[str, dict[str, Any]]: ...


class PlagiarismRunService(Protocol):
    def create(self, envelope: bytes) -> dict[str, Any]: ...

    def get(self, run_id: str) -> dict[str, Any]: ...

    def latest(self, submission_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class ReviewLaunchResult:
    run_id: str
    error: str | None = None


def _agent_run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: run[key]
        for key in (
            "run_id",
            "submission_id",
            "model",
            "source",
            "state",
            "created_at",
            "updated_at",
            "error_code",
        )
        if key in run
    }
    result = run.get("result")
    if isinstance(result, Mapping):
        decision = result.get("decision")
        if isinstance(decision, str):
            summary["decision"] = decision
        provenance = result.get("provenance")
        if isinstance(provenance, Mapping):
            completed_at = provenance.get("completed_at")
            if isinstance(completed_at, str):
                summary["completed_at"] = completed_at
    return summary


class FileComplianceReportReader:
    """Read the latest immutable single-submission report for a submission."""

    def __init__(self, root: str | Path, *, refresh_seconds: float = 30.0) -> None:
        if refresh_seconds < 0:
            raise ValueError("refresh_seconds must not be negative")
        self._root = Path(root)
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._indexed_at = 0.0
        self._reports: dict[str, dict[str, Any]] = {}

    def get_submission_report(self, submission_id: str) -> dict[str, Any] | None:
        self._refresh_if_needed()
        report = self._reports.get(submission_id)
        return dict(report) if report is not None else None

    def refresh(self) -> None:
        with self._lock:
            self._reports = self._build_index()
            self._indexed_at = time.monotonic()

    def _refresh_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._indexed_at < self._refresh_seconds:
            return
        with self._lock:
            now = time.monotonic()
            if now - self._indexed_at < self._refresh_seconds:
                return
            self._reports = self._build_index()
            self._indexed_at = now

    def _build_index(self) -> dict[str, dict[str, Any]]:
        reports: dict[str, tuple[tuple[float, str], dict[str, Any]]] = {}
        owners_root = self._root / "owners"
        if not owners_root.is_dir():
            return {}
        for report_path in owners_root.glob("*/*.json"):
            if (
                report_path.is_symlink()
                or report_path.parent.is_symlink()
                or not report_path.is_file()
            ):
                continue
            candidate = self._read_report(report_path)
            if candidate is None:
                continue
            submission_id, sort_key, report = candidate
            current = reports.get(submission_id)
            if current is None or sort_key > current[0]:
                reports[submission_id] = (sort_key, report)
        return {submission_id: report for submission_id, (_, report) in reports.items()}

    @staticmethod
    def _read_report(
        report_path: Path,
    ) -> tuple[str, tuple[float, str], dict[str, Any]] | None:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("kind") != "single_review":
            return None
        review = payload.get("review")
        if not isinstance(review, dict):
            return None
        identity = review.get("identity")
        verdict = review.get("verdict")
        if not isinstance(identity, dict) or not isinstance(verdict, dict):
            return None
        submission_ids = identity.get("submission_ids")
        if not isinstance(submission_ids, list) or len(submission_ids) != 1:
            return None
        submission_id = submission_ids[0]
        if not isinstance(submission_id, str):
            return None
        lab_id = identity.get("lab_id")
        review_key = review.get("review_key")
        completed_at = review.get("completed_at")
        decision = verdict.get("decision")
        if not isinstance(lab_id, str):
            return None
        if not isinstance(review_key, str):
            return None
        if not isinstance(completed_at, str):
            return None
        if not isinstance(decision, str) or decision not in _COMPLIANCE_DECISIONS:
            return None
        evidence = review.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        try:
            parsed_completed_at = datetime.fromisoformat(completed_at)
        except ValueError:
            return None
        if parsed_completed_at.tzinfo is None or parsed_completed_at.utcoffset() is None:
            return None
        completed_sort = parsed_completed_at.timestamp()
        state = (
            "inconclusive"
            if decision == "inconclusive" or review.get("conclusive") is False
            else "completed"
        )
        compliant: bool | None
        if decision == "compliant":
            compliant = True
        elif decision == "violation":
            compliant = False
        else:
            compliant = None
        report = {
            "schema_version": 1,
            "submission": {
                "id": submission_id,
                "owner": report_path.parent.name,
                "lab_id": lab_id,
                "score": payload.get("score"),
            },
            "state": state,
            "decision": decision,
            "compliant": compliant,
            "confidence": verdict.get("confidence"),
            "summary": verdict.get("summary", ""),
            "violations": verdict.get("violations", []),
            "evidence": evidence,
            "provenance": {
                "review_key": review_key,
                "completed_at": completed_at,
                "basis_commit": identity.get("basis_commit"),
                "rules_version": identity.get("rules_version"),
                "prompt_version": identity.get("prompt_version"),
                "schema_version": identity.get("schema_version"),
                "model": identity.get("model"),
            },
        }
        return submission_id, (completed_sort, review_key), report


class FilePlagiarismReportReader:
    """Index immutable pair reviews by both participating submissions."""

    def __init__(self, root: str | Path, *, refresh_seconds: float = 30.0) -> None:
        if refresh_seconds < 0:
            raise ValueError("refresh_seconds must not be negative")
        self._root = Path(root)
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._indexed_at = 0.0
        self._reports: dict[str, list[dict[str, Any]]] = {}

    def get_submission_reports(self, submission_id: str) -> list[dict[str, Any]]:
        self._refresh_if_needed()
        return [dict(report) for report in self._reports.get(submission_id, ())]

    def refresh(self) -> None:
        with self._lock:
            self._reports = self._build_index()
            self._indexed_at = time.monotonic()

    def _refresh_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._indexed_at < self._refresh_seconds:
            return
        with self._lock:
            now = time.monotonic()
            if now - self._indexed_at < self._refresh_seconds:
                return
            self._reports = self._build_index()
            self._indexed_at = now

    def _build_index(self) -> dict[str, list[dict[str, Any]]]:
        indexed: dict[str, dict[str, tuple[tuple[float, str], dict[str, Any]]]] = {}
        plagiarism_root = self._root / "plagiarism"
        if not plagiarism_root.is_dir() or plagiarism_root.is_symlink():
            return {}
        for report_path in plagiarism_root.glob("*/*.json"):
            if (
                report_path.is_symlink()
                or report_path.parent.is_symlink()
                or not report_path.is_file()
            ):
                continue
            candidate = self._read_report(report_path)
            if candidate is None:
                continue
            submission_ids, sort_key, reports = candidate
            for submission_id, report in zip(submission_ids, reports, strict=True):
                counterpart = cast(dict[str, Any], report["counterpart"])
                counterpart_id = cast(str, counterpart["submission_id"])
                by_counterpart = indexed.setdefault(submission_id, {})
                current = by_counterpart.get(counterpart_id)
                if current is None or sort_key > current[0]:
                    by_counterpart[counterpart_id] = (sort_key, report)
        return {
            submission_id: [
                report
                for _, report in sorted(
                    by_counterpart.values(), key=lambda candidate: candidate[0], reverse=True
                )
            ]
            for submission_id, by_counterpart in indexed.items()
        }

    @staticmethod
    def _read_report(
        report_path: Path,
    ) -> tuple[tuple[str, str], tuple[float, str], tuple[dict[str, Any], dict[str, Any]]] | None:
        try:
            payload = _read_bounded_json_object(report_path, max_bytes=2 << 20)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if payload.get("kind") != "plagiarism_review":
            return None
        review = payload.get("review")
        if not isinstance(review, dict):
            return None
        identity = review.get("identity")
        verdict = review.get("verdict")
        if not isinstance(identity, dict) or not isinstance(verdict, dict):
            return None
        submission_ids = _canonical_uuid_pair(payload.get("submission_ids"))
        identity_ids = _canonical_uuid_pair(identity.get("submission_ids"))
        if submission_ids is None or identity_ids != submission_ids:
            return None
        owners = payload.get("owners")
        submitted_at = payload.get("submitted_at")
        if not _string_pair(owners) or not _string_pair(submitted_at):
            return None
        if any(_aware_timestamp(value) is None for value in submitted_at):
            return None
        lab_id = identity.get("lab_id")
        review_key = review.get("review_key")
        task_key = payload.get("task_key")
        completed_at = review.get("completed_at")
        model = identity.get("model")
        if (
            identity.get("task_type") != "plagiarism"
            or not isinstance(lab_id, str)
            or not lab_id
            or not isinstance(review_key, str)
            or len(review_key) != 64
            or any(character not in "0123456789abcdef" for character in review_key)
            or task_key != review_key
            or not isinstance(completed_at, str)
            or not isinstance(model, str)
            or not model
        ):
            return None
        completed_timestamp = _aware_timestamp(completed_at)
        if completed_timestamp is None:
            return None
        decision = verdict.get("decision")
        relationship = verdict.get("relationship")
        conclusive = review.get("conclusive")
        confidence = verdict.get("confidence")
        summary = verdict.get("summary")
        signal = payload.get("similarity_signal")
        jaccard = payload.get("jaccard")
        human_review_status = payload.get("human_review_status")
        evidence = verdict.get("evidence")
        if (
            decision not in _PLAGIARISM_DECISIONS
            or relationship not in _PLAGIARISM_RELATIONSHIPS
            or not isinstance(conclusive, bool)
            or conclusive != (decision != "inconclusive")
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
            or not isinstance(summary, str)
            or signal not in _SIMILARITY_SIGNALS
            or isinstance(jaccard, bool)
            or not isinstance(jaccard, int | float)
            or not 0 <= jaccard <= 1
            or human_review_status != "pending"
            or not isinstance(evidence, list)
        ):
            return None
        normalized_evidence: list[dict[str, str]] = []
        for item in evidence:
            if not isinstance(item, dict):
                return None
            first_path = item.get("first_path")
            second_path = item.get("second_path")
            description = item.get("description")
            if not all(isinstance(value, str) for value in (first_path, second_path, description)):
                return None
            normalized_evidence.append(
                {
                    "first_path": cast(str, first_path),
                    "second_path": cast(str, second_path),
                    "description": cast(str, description),
                }
            )
        common = {
            "review_key": review_key,
            "lab_id": lab_id,
            "decision": decision,
            "relationship": relationship,
            "similarity": {"signal": signal, "jaccard": jaccard},
            "confidence": confidence,
            "summary": summary,
            "completed_at": completed_at,
            "model": model,
            "human_review_status": human_review_status,
        }
        reports = tuple(
            {
                **common,
                "counterpart": {
                    "submission_id": submission_ids[1 - index],
                    "owner": owners[1 - index],
                    "submitted_at": submitted_at[1 - index],
                },
                "evidence": [
                    {
                        "submission_path": item["first_path" if index == 0 else "second_path"],
                        "counterpart_path": item["second_path" if index == 0 else "first_path"],
                        "description": item["description"],
                    }
                    for item in normalized_evidence
                ],
            }
            for index in range(2)
        )
        return (
            submission_ids,
            (completed_timestamp, review_key),
            cast(tuple[dict[str, Any], dict[str, Any]], reports),
        )


def _read_bounded_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ValueError("report is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise ValueError("report exceeds the size limit")
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("report must be a JSON object")
        return value
    finally:
        os.close(descriptor)


def _canonical_uuid_pair(value: Any) -> tuple[str, str] | None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) for item in value)
    ):
        return None
    try:
        pair = tuple(str(uuid.UUID(item)) for item in value)
    except ValueError:
        return None
    if tuple(value) != pair or pair[0] == pair[1]:
        return None
    return cast(tuple[str, str], pair)


def _string_pair(value: Any) -> TypeGuard[list[str]]:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, str) and item for item in value)
    )


def _aware_timestamp(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: bytes


class ComplianceApi:
    """Small HTTP adapter exposing authenticated single-submission checker operations."""

    def __init__(
        self,
        reader: ComplianceReportReader,
        launcher: ReviewLauncher,
        *,
        allowed_models: Iterable[str],
        auth_token: str | None = None,
        max_body_bytes: int = 2 << 20,
        agent_runs: AgentRunService | None = None,
        plagiarism_reader: PlagiarismReportReader | None = None,
        plagiarism_runs: PlagiarismRunService | None = None,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._reader = reader
        self._launcher = launcher
        self._allowed_models = tuple(dict.fromkeys(allowed_models))
        if not self._allowed_models or any(
            not model or model.strip() != model for model in self._allowed_models
        ):
            raise ValueError("allowed_models must contain non-empty model names")
        self._auth_token = auth_token
        self._max_body_bytes = max_body_bytes
        self._agent_runs = agent_runs
        self._plagiarism_reader = plagiarism_reader
        self._plagiarism_runs = plagiarism_runs
        self._launch_lock = threading.Lock()

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlsplit(target).path
        if path == "/healthz" and method == "GET":
            return self._json(HTTPStatus.OK, {"status": "ok"})
        if self._auth_token and headers.get("authorization") != f"Bearer {self._auth_token}":
            return self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "authentication required")
        if len(body) > self._max_body_bytes:
            return self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "BODY_TOO_LARGE",
                "request body is too large",
            )

        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:3] == ["v1", "plagiarism", "submissions"]:
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            submission_id = self._normalize_submission_id(parts[3])
            if submission_id is None:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "submission_id must be a UUID",
                )
            if self._plagiarism_reader is None:
                return self._error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "PLAGIARISM_REPORTS_DISABLED",
                    "plagiarism reports are not configured",
                )
            try:
                reports = self._plagiarism_reader.get_submission_reports(submission_id)
            except Exception:
                return self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "PLAGIARISM_REPORT_READ_FAILED",
                    "plagiarism reports could not be read",
                )
            return self._json(
                HTTPStatus.OK,
                {
                    "schema_version": 1,
                    "submission_id": submission_id,
                    "items": reports,
                },
            )

        if parts == ["v1", "plagiarism", "review-runs"]:
            if method != "POST":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._create_plagiarism_run(body)

        if len(parts) == 4 and parts[:3] == ["v1", "plagiarism", "review-runs"]:
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._get_plagiarism_run(parts[3])

        if (
            len(parts) == 6
            and parts[:3] == ["v1", "plagiarism", "submissions"]
            and parts[4:] == ["review-runs", "latest"]
        ):
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            submission_id = self._normalize_submission_id(parts[3])
            if submission_id is None:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "submission_id must be a UUID",
                )
            return self._latest_plagiarism_run(submission_id)

        if parts == ["v1", "compliance", "models"]:
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._json(HTTPStatus.OK, {"models": list(self._allowed_models)})

        if parts == ["v1", "compliance", "review-runs"]:
            if method != "POST":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._create_agent_run(body)

        if parts == ["v1", "compliance", "review-runs", "latest:batch"]:
            if method != "POST":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._latest_agent_runs_batch(body)

        if len(parts) == 4 and parts[:3] == ["v1", "compliance", "review-runs"]:
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._get_agent_run(parts[3])

        if (
            len(parts) == 6
            and parts[:3] == ["v1", "compliance", "submissions"]
            and parts[4:] == ["review-runs", "latest"]
        ):
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            submission_id = self._normalize_submission_id(parts[3])
            if submission_id is None:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "submission_id must be a UUID",
                )
            return self._latest_agent_run(submission_id)

        if len(parts) == 4 and parts[:3] == ["v1", "compliance", "submissions"]:
            if method != "GET":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            submission_id = self._normalize_submission_id(parts[3])
            if submission_id is None:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "submission_id must be a UUID",
                )
            report = self._latest_completed_agent_report(submission_id)
            if report is None:
                report = self._reader.get_submission_report(submission_id)
            if report is None:
                return self._error(
                    HTTPStatus.NOT_FOUND,
                    "REPORT_NOT_FOUND",
                    "no compliance report found",
                )
            return self._json(HTTPStatus.OK, report)

        if parts == ["v1", "compliance", "reviews"]:
            if method != "POST":
                return self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "method not allowed",
                )
            return self._launch(body)

        return self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "endpoint not found")

    def _create_agent_run(self, body: bytes) -> ApiResponse:
        if self._agent_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "AGENT_RUNS_DISABLED",
                "Agent review runs are not configured",
            )
        try:
            run = self._agent_runs.create(body)
        except ReviewBundleError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REVIEW_BUNDLE",
                "review bundle verification failed",
            )
        except AgentRunQueueFull:
            return self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "REVIEW_QUEUE_FULL",
                "review queue is full",
            )
        except AgentRunError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REVIEW_REQUEST",
                "review run request is invalid",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REVIEW_RUN_CREATE_FAILED",
                "review run could not be created",
            )
        return self._json(HTTPStatus.ACCEPTED, run)

    def _create_plagiarism_run(self, body: bytes) -> ApiResponse:
        if self._plagiarism_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "PLAGIARISM_RUNS_DISABLED",
                "plagiarism review runs are not configured",
            )
        try:
            run = self._plagiarism_runs.create(body)
        except ReviewBundleError as error:
            _LOGGER.warning("plagiarism bundle verification failed: %s", error)
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLAGIARISM_BUNDLE",
                "plagiarism bundle verification failed",
            )
        except AgentRunQueueFull:
            return self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "PLAGIARISM_QUEUE_FULL",
                "plagiarism review queue is full",
            )
        except AgentRunError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLAGIARISM_REQUEST",
                "plagiarism review request is invalid",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "PLAGIARISM_RUN_CREATE_FAILED",
                "plagiarism review run could not be created",
            )
        return self._json(HTTPStatus.ACCEPTED, run)

    def _get_plagiarism_run(self, run_id: str) -> ApiResponse:
        if self._plagiarism_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "PLAGIARISM_RUNS_DISABLED",
                "plagiarism review runs are not configured",
            )
        try:
            run = self._plagiarism_runs.get(run_id)
        except (LookupError, AgentRunError):
            return self._error(
                HTTPStatus.NOT_FOUND,
                "PLAGIARISM_RUN_NOT_FOUND",
                "plagiarism review run was not found",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "PLAGIARISM_RUN_READ_FAILED",
                "plagiarism review run could not be read",
            )
        return self._json(HTTPStatus.OK, run)

    def _latest_plagiarism_run(self, submission_id: str) -> ApiResponse:
        if self._plagiarism_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "PLAGIARISM_RUNS_DISABLED",
                "plagiarism review runs are not configured",
            )
        try:
            run = self._plagiarism_runs.latest(submission_id)
        except AgentRunError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLAGIARISM_REQUEST",
                "plagiarism review request is invalid",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "PLAGIARISM_RUN_READ_FAILED",
                "plagiarism review run could not be read",
            )
        if run is None:
            return self._error(
                HTTPStatus.NOT_FOUND,
                "PLAGIARISM_RUN_NOT_FOUND",
                "plagiarism review run was not found",
            )
        return self._json(HTTPStatus.OK, run)

    def _get_agent_run(self, run_id: str) -> ApiResponse:
        if self._agent_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "AGENT_RUNS_DISABLED",
                "Agent review runs are not configured",
            )
        try:
            run = self._agent_runs.get(run_id)
        except (LookupError, AgentRunError):
            return self._error(
                HTTPStatus.NOT_FOUND,
                "REVIEW_RUN_NOT_FOUND",
                "review run was not found",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REVIEW_RUN_READ_FAILED",
                "review run could not be read",
            )
        return self._json(HTTPStatus.OK, run)

    def _latest_agent_run(self, submission_id: str) -> ApiResponse:
        if self._agent_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "AGENT_RUNS_DISABLED",
                "Agent review runs are not configured",
            )
        try:
            run = self._agent_runs.latest(submission_id)
        except AgentRunError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REVIEW_REQUEST",
                "review run request is invalid",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REVIEW_RUN_READ_FAILED",
                "review run could not be read",
            )
        if run is None:
            return self._error(
                HTTPStatus.NOT_FOUND,
                "REVIEW_RUN_NOT_FOUND",
                "review run was not found",
            )
        return self._json(HTTPStatus.OK, run)

    def _latest_agent_runs_batch(self, body: bytes) -> ApiResponse:
        if self._agent_runs is None:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "AGENT_RUNS_DISABLED",
                "Agent review runs are not configured",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "body must be JSON")
        if not isinstance(payload, dict) or set(payload) != {"submission_ids"}:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "body must contain exactly submission_ids",
            )
        submission_ids = payload.get("submission_ids")
        if (
            not isinstance(submission_ids, list)
            or not submission_ids
            or len(submission_ids) > 1000
            or any(not isinstance(value, str) for value in submission_ids)
            or len(set(submission_ids)) != len(submission_ids)
        ):
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "submission_ids must contain 1-1000 unique UUIDs",
            )
        normalized: list[str] = []
        for value in submission_ids:
            submission_id = self._normalize_submission_id(cast(str, value))
            if submission_id is None:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "submission_ids must contain 1-1000 unique UUIDs",
                )
            normalized.append(submission_id)
        try:
            runs = self._agent_runs.latest_many(normalized)
        except AgentRunError:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_REVIEW_REQUEST",
                "review run request is invalid",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REVIEW_RUN_READ_FAILED",
                "review runs could not be read",
            )
        items = [
            _agent_run_summary(runs[submission_id])
            for submission_id in normalized
            if submission_id in runs
        ]
        return self._json(HTTPStatus.OK, {"items": items})

    def _latest_completed_agent_report(self, submission_id: str) -> dict[str, Any] | None:
        if self._agent_runs is None:
            return None
        try:
            run = self._agent_runs.latest(submission_id)
        except AgentRunError:
            return None
        if run is None or run.get("state") != "completed":
            return None
        result = run.get("result")
        run_id = run.get("run_id")
        if not isinstance(result, Mapping) or not isinstance(run_id, str):
            return None
        return {**result, "run_id": run_id}

    def _launch(self, body: bytes) -> ApiResponse:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "body must be JSON")
        if not isinstance(payload, dict) or set(payload) != {"submission_id", "model"}:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "body must contain exactly one submission_id and model",
            )
        submission_id = payload.get("submission_id")
        if not isinstance(submission_id, str):
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "submission_id must be a UUID",
            )
        submission_id = self._normalize_submission_id(submission_id)
        if submission_id is None:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "submission_id must be a UUID",
            )
        model = payload.get("model")
        if not isinstance(model, str) or model not in self._allowed_models:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "MODEL_NOT_ALLOWED",
                "model is not allowed",
            )
        if not self._launch_lock.acquire(blocking=False):
            return self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "REVIEW_IN_PROGRESS",
                "another review is running",
            )
        try:
            result = self._launcher.launch(submission_id, model)
        except LookupError:
            return self._error(
                HTTPStatus.NOT_FOUND,
                "SUBMISSION_NOT_FOUND",
                "submission is not reviewable",
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REVIEW_FAILED",
                "review could not be started",
            )
        finally:
            self._launch_lock.release()
        if result.error is not None:
            return self._json(
                HTTPStatus.BAD_GATEWAY,
                {"state": "failed", "run_id": result.run_id, "error": result.error},
            )
        self._reader.refresh()
        report = self._reader.get_submission_report(submission_id)
        if report is None:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "REPORT_WRITE_MISSING",
                "review completed without a report",
            )
        return self._json(HTTPStatus.OK, {**report, "run_id": result.run_id})

    @staticmethod
    def _normalize_submission_id(value: str) -> str | None:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _json(status: int | HTTPStatus, payload: Mapping[str, Any]) -> ApiResponse:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        return ApiResponse(int(status), body)

    @classmethod
    def _error(cls, status: int | HTTPStatus, code: str, message: str) -> ApiResponse:
        return cls._json(status, {"code": code, "message": message})


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "oj-checker-report-api/1"

    def _app(self) -> ComplianceApi:
        return cast(ComplianceApi, self.server.app)  # type: ignore[attr-defined]

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_response(
                self._app()._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "invalid Content-Length",
                )
            )
            return
        if length < 0:
            self._write_response(
                self._app()._error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "invalid Content-Length",
                )
            )
            return
        if length > self._app()._max_body_bytes:
            self.close_connection = True
            oversized_body = b"x" * (self._app()._max_body_bytes + 1)
            response = self._app().handle(
                self.command,
                self.path,
                dict(self.headers.items()),
                oversized_body,
            )
        else:
            body = self.rfile.read(length)
            response = self._app().handle(
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
            )
        self._write_response(response)

    def _write_response(self, response: ApiResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve_report_api(app: ComplianceApi, listen: str) -> None:
    host, separator, raw_port = listen.rpartition(":")
    if not separator or not host or not raw_port.isdigit():
        raise ValueError("listen must be HOST:PORT")
    server = ThreadingHTTPServer((host, int(raw_port)), _RequestHandler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        server.server_close()


class RunnerReviewLauncher:
    def __init__(
        self,
        runner: Any,
        *,
        min_score: int = 0,
        rules_version: str = "audit-rules-v2",
        clock: Callable[[], datetime],
    ) -> None:
        self._runner = runner
        self._min_score = min_score
        self._rules_version = rules_version
        self._clock = clock

    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        from oj_checker.domain import AuditRequest

        started_at = self._clock()
        run_id = f"manual-{submission_id}-{started_at.strftime('%Y%m%dT%H%M%S%fZ')}"
        try:
            summary = self._runner.run(
                AuditRequest(
                    run_id=run_id,
                    cutoff=started_at,
                    min_score=self._min_score,
                    submission_id=submission_id,
                    rules_version=self._rules_version,
                    prompt_version="compliance-v5",
                    model=model,
                    execute_reviews=True,
                )
            )
        except LookupError:
            raise
        except Exception as error:
            return ReviewLaunchResult(run_id=run_id, error=type(error).__name__)
        if getattr(summary, "failed_review_count", 0):
            return ReviewLaunchResult(run_id=run_id, error="ReviewError")
        return ReviewLaunchResult(run_id=run_id)
