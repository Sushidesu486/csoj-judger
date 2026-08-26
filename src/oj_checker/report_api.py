from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

_COMPLIANCE_DECISIONS = frozenset({"compliant", "violation", "inconclusive"})


class ComplianceReportReader(Protocol):
    def get_submission_report(self, submission_id: str) -> dict[str, Any] | None: ...

    def refresh(self) -> None: ...


class ReviewLauncher(Protocol):
    def launch(self, submission_id: str) -> ReviewLaunchResult: ...


@dataclass(frozen=True, slots=True)
class ReviewLaunchResult:
    run_id: str
    error: str | None = None


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


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: bytes


class ComplianceApi:
    """Small HTTP adapter exposing only one-submission compliance operations."""

    def __init__(
        self,
        reader: ComplianceReportReader,
        launcher: ReviewLauncher,
        *,
        auth_token: str | None = None,
        max_body_bytes: int = 64 * 1024,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self._reader = reader
        self._launcher = launcher
        self._auth_token = auth_token
        self._max_body_bytes = max_body_bytes
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

    def _launch(self, body: bytes) -> ApiResponse:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "body must be JSON")
        if not isinstance(payload, dict) or set(payload) != {"submission_id"}:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "BAD_REQUEST",
                "body must contain exactly one submission_id",
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
        if not self._launch_lock.acquire(blocking=False):
            return self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "REVIEW_IN_PROGRESS",
                "another review is running",
            )
        try:
            result = self._launcher.launch(submission_id)
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
        model: str,
        min_score: int = 0,
        rules_version: str = "audit-rules-v1",
        clock: Callable[[], datetime],
    ) -> None:
        self._runner = runner
        self._model = model
        self._min_score = min_score
        self._rules_version = rules_version
        self._clock = clock

    def launch(self, submission_id: str) -> ReviewLaunchResult:
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
                    prompt_version="compliance-v1",
                    model=self._model,
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
