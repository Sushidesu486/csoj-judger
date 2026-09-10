from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from oj_checker.agent_runs import AgentRunError, AgentRunFailure, AgentRunQueueFull
from oj_checker.plagiarism_bundle import verify_plagiarism_bundle
from oj_checker.review_bundle import VerifiedReviewBundle

_RUN_ID = re.compile(r"^plagiarism-[0-9a-f]{64}$")
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_ACTIVE_STATES = frozenset({"preparing", "running", "finalizing"})
_RETRYABLE_FAILURES = frozenset(
    {"JOB_LOST", "PLAGIARISM_EXECUTION_FAILED", "SOURCE_BUNDLE_INVALID"}
)
_QUEUE_STOP = object()
_LOGGER = logging.getLogger(__name__)


class PlagiarismRunExecutor(Protocol):
    def execute(self, bundle: VerifiedReviewBundle) -> Mapping[str, Any]: ...


class FilePlagiarismRunService:
    """Persistent bounded queue for signed single-target plagiarism reviews."""

    def __init__(
        self,
        root: str | Path,
        *,
        public_keys: Mapping[str, bytes | bytearray | str],
        allowed_models: Iterable[str],
        executor: PlagiarismRunExecutor | None = None,
        worker_count: int = 0,
        max_queued: int = 100,
        max_request_bytes: int = 16 << 20,
        max_run_attempts: int = 3,
        reconcile_interval_seconds: float = 0,
        reconcile_batch_size: int = 4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        models = tuple(dict.fromkeys(allowed_models))
        if not models or any(not model or model != model.strip() for model in models):
            raise ValueError("allowed_models must contain trimmed model names")
        if worker_count < 0 or worker_count > 4:
            raise ValueError("worker_count must be between zero and four")
        if executor is None and worker_count:
            raise ValueError("worker_count requires a plagiarism executor")
        if max_queued <= 0:
            raise ValueError("max_queued must be positive")
        if max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
        if max_run_attempts <= 0:
            raise ValueError("max_run_attempts must be positive")
        if reconcile_interval_seconds < 0:
            raise ValueError("reconcile_interval_seconds cannot be negative")
        if reconcile_batch_size <= 0:
            raise ValueError("reconcile_batch_size must be positive")
        if not public_keys:
            raise ValueError("at least one plagiarism bundle public key is required")
        self._root = Path(root) / "plagiarism-runs"
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_keys = dict(public_keys)
        self._allowed_models = frozenset(models)
        self._executor = executor
        self._worker_count = worker_count
        self._max_request_bytes = max_request_bytes
        self._max_run_attempts = max_run_attempts
        self._reconcile_interval_seconds = reconcile_interval_seconds
        self._reconcile_batch_size = reconcile_batch_size
        self._clock = clock or (lambda: datetime.now(UTC))
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=max_queued)
        self._lock = threading.RLock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._latest_by_submission: dict[str, tuple[str, str]] = {}
        self._threads: list[threading.Thread] = []
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()
        self._started = False
        self._rebuild_index()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._reconcile_stop.clear()
            self._recover()
            for index in range(self._worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"oj-plagiarism-run-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            if (
                self._executor is not None
                and self._worker_count
                and self._reconcile_interval_seconds > 0
            ):
                self.reconcile_failed()
                self._reconcile_thread = threading.Thread(
                    target=self._reconcile_loop,
                    name="oj-plagiarism-run-reconciler",
                    daemon=True,
                )
                self._reconcile_thread.start()

    def close(self) -> None:
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
            reconcile_thread = self._reconcile_thread
            self._reconcile_thread = None
            self._reconcile_stop.set()
            self._started = False
        if reconcile_thread is not None:
            reconcile_thread.join(timeout=5)
        for _ in threads:
            self._queue.put(_QUEUE_STOP)
        for thread in threads:
            thread.join(timeout=5)

    def create(self, envelope: bytes) -> dict[str, Any]:
        if len(envelope) > self._max_request_bytes:
            raise AgentRunError("plagiarism bundle exceeds its size limit")
        now = self._aware_now()
        bundle = verify_plagiarism_bundle(envelope, self._public_keys, now=now)
        model = bundle.payload["model"]
        if not isinstance(model, str) or model not in self._allowed_models:
            raise AgentRunError("bundle model is not allowed")
        submission_id = bundle.payload["target_submission_id"]
        if not isinstance(submission_id, str):
            raise AgentRunError("verified bundle target submission ID disappeared")
        run_id = f"plagiarism-{bundle.payload_digest}"
        with self._lock:
            if self._queue.full():
                raise AgentRunQueueFull("plagiarism review queue is full")
            run_root = self._root / run_id
            if run_root.exists():
                return self.get(run_id)
            run_root.mkdir(mode=0o750)
            (run_root / "events").mkdir(mode=0o750)
            metadata = {
                "schema_version": 1,
                "run_id": run_id,
                "payload_digest": bundle.payload_digest,
                "submission_id": submission_id,
                "model": model,
                "source": bundle.payload["source"],
                "created_at": now.isoformat(),
            }
            _create_file(run_root / "request.json", envelope)
            _create_json(run_root / "metadata.json", metadata)
            self._append_event(run_id, "queued", at=now)
            _create_file(run_root / "_READY", b"")
            created = self._read_run(run_root)
            self._remember(created)
            if self._executor is not None and self._worker_count:
                self._queue.put_nowait(run_id)
            return dict(created)

    def get(self, run_id: str) -> dict[str, Any]:
        if _RUN_ID.fullmatch(run_id) is None:
            raise AgentRunError("invalid plagiarism run ID")
        with self._lock:
            cached = self._runs.get(run_id)
            if cached is not None:
                return dict(cached)
            response = self._read_run(self._run_root(run_id))
            self._remember(response)
            return dict(response)

    def _read_run(self, run_root: Path) -> dict[str, Any]:
        metadata = _read_json_object(run_root / "metadata.json", max_bytes=64 * 1024)
        event = self._latest_event(run_root)
        response = {**metadata, **event, "attempts": self._attempt_count(run_root)}
        result_path = run_root / "result.json"
        if result_path.is_file() and not result_path.is_symlink():
            response["result"] = _read_json_object(result_path, max_bytes=16 << 20)
        return response

    def latest(self, submission_id: str) -> dict[str, Any] | None:
        canonical = _canonical_submission_id(submission_id)
        with self._lock:
            candidate = self._latest_by_submission.get(canonical)
            if candidate is None:
                return None
            run = self._runs.get(candidate[1])
            return dict(run) if run is not None else None

    def _rebuild_index(self) -> None:
        for run_root in self._safe_run_roots():
            try:
                self._remember(self._read_run(run_root))
            except (AgentRunError, OSError):
                continue

    def _remember(self, run: dict[str, Any]) -> None:
        run_id = run.get("run_id")
        submission_id = run.get("submission_id")
        created_at = run.get("created_at")
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise AgentRunError("plagiarism run record has an invalid run ID")
        if not isinstance(submission_id, str) or not isinstance(created_at, str):
            raise AgentRunError("plagiarism run record has incomplete metadata")
        self._runs[run_id] = run
        candidate = (created_at, run_id)
        current = self._latest_by_submission.get(submission_id)
        if current is None or candidate > current:
            self._latest_by_submission[submission_id] = candidate

    def _recover(self) -> None:
        for run_root in self._safe_run_roots():
            try:
                event = self._latest_event(run_root)
            except (AgentRunError, OSError):
                continue
            state = event.get("state")
            if state == "finalizing" and self._recover_persisted_result(run_root.name):
                continue
            if state in _ACTIVE_STATES:
                self._append_event(run_root.name, "failed", error_code="JOB_LOST")
            elif state == "queued" and self._executor is not None and self._worker_count:
                try:
                    self._queue.put_nowait(run_root.name)
                except queue.Full:
                    break

    def reconcile_failed(self) -> int:
        if not self._started or self._executor is None or not self._worker_count:
            return 0
        with self._lock:
            candidates = sorted(
                (
                    dict(run)
                    for run in self._runs.values()
                    if run.get("state") == "failed"
                    and run.get("error_code") in _RETRYABLE_FAILURES
                ),
                key=lambda run: (str(run.get("updated_at", "")), str(run.get("run_id", ""))),
            )
            reconciled = 0
            for run in candidates:
                if reconciled >= self._reconcile_batch_size or self._queue.full():
                    break
                run_id = run.get("run_id")
                if not isinstance(run_id, str):
                    continue
                if self._recover_persisted_result(run_id):
                    continue
                if self._attempt_count(self._run_root(run_id)) >= self._max_run_attempts:
                    continue
                self._append_event(run_id, "queued")
                self._queue.put_nowait(run_id)
                reconciled += 1
            return reconciled

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(self._reconcile_interval_seconds):
            try:
                self.reconcile_failed()
            except Exception:
                _LOGGER.exception("Plagiarism run reconciliation failed")

    def _recover_persisted_result(self, run_id: str) -> bool:
        result_path = self._run_root(run_id) / "result.json"
        if result_path.is_symlink() or not result_path.exists():
            return False
        try:
            _read_json_object(result_path, max_bytes=16 << 20)
        except (AgentRunError, OSError):
            self._append_event(
                run_id,
                "failed",
                error_code="PLAGIARISM_RESULT_RECORD_INVALID",
            )
            return True
        self._append_event(run_id, "completed")
        return True

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                if isinstance(item, str):
                    self._execute(item)
            finally:
                self._queue.task_done()

    def _execute(self, run_id: str) -> None:
        if self._executor is None:
            return
        try:
            self._append_event(run_id, "preparing")
            envelope = _read_file(
                self._run_root(run_id) / "request.json",
                max_bytes=self._max_request_bytes,
            )
            bundle = verify_plagiarism_bundle(envelope, self._public_keys, now=None)
            self._append_event(run_id, "running")
            result = dict(self._executor.execute(bundle))
            self._append_event(run_id, "finalizing")
            _create_json(self._run_root(run_id) / "result.json", result)
            self._append_event(run_id, "completed")
        except AgentRunFailure as error:
            self._append_event(run_id, "failed", error_code=error.code)
        except Exception:
            self._append_event(run_id, "failed", error_code="PLAGIARISM_EXECUTION_FAILED")

    def _append_event(
        self,
        run_id: str,
        state: str,
        *,
        at: datetime | None = None,
        error_code: str | None = None,
    ) -> None:
        if state not in {"queued", "preparing", "running", "finalizing", *_TERMINAL_STATES}:
            raise ValueError("invalid plagiarism run state")
        with self._lock:
            events_root = self._run_root(run_id) / "events"
            sequence = len(tuple(events_root.glob("*.json"))) + 1
            event: dict[str, Any] = {
                "sequence": sequence,
                "state": state,
                "updated_at": (at or self._aware_now()).isoformat(),
            }
            if error_code is not None:
                event["error_code"] = error_code
            _create_json(events_root / f"{sequence:08d}__{state}.json", event)
            cached = self._runs.get(run_id)
            if cached is not None:
                updated = {**cached, **event}
                updated["attempts"] = self._attempt_count(self._run_root(run_id))
                result_path = self._run_root(run_id) / "result.json"
                if result_path.is_file() and not result_path.is_symlink():
                    updated["result"] = _read_json_object(result_path, max_bytes=16 << 20)
                self._runs[run_id] = updated

    @staticmethod
    def _attempt_count(run_root: Path) -> int:
        return len(tuple((run_root / "events").glob("*__preparing.json")))

    def _latest_event(self, run_root: Path) -> dict[str, Any]:
        event_paths = sorted((run_root / "events").glob("*.json"))
        if not event_paths:
            raise AgentRunError("plagiarism run has no state events")
        return _read_json_object(event_paths[-1], max_bytes=64 * 1024)

    def _run_root(self, run_id: str) -> Path:
        if _RUN_ID.fullmatch(run_id) is None:
            raise AgentRunError("invalid plagiarism run ID")
        root = self._root / run_id
        if not root.is_dir() or root.is_symlink():
            raise LookupError(run_id)
        return root

    def _safe_run_roots(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in self._root.glob("plagiarism-*")
            if _RUN_ID.fullmatch(path.name) is not None
            and path.is_dir()
            and not path.is_symlink()
            and (path / "_READY").is_file()
            and not (path / "_READY").is_symlink()
        )

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("plagiarism run clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)


def _canonical_submission_id(submission_id: str) -> str:
    if not isinstance(submission_id, str):
        raise AgentRunError("submission ID must be a UUID")
    try:
        canonical = str(uuid.UUID(submission_id))
    except ValueError as error:
        raise AgentRunError("submission ID must be a UUID") from error
    if canonical != submission_id:
        raise AgentRunError("submission ID must be canonical")
    return canonical


def _create_json(path: Path, value: Mapping[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _create_file(path, (content + "\n").encode())


def _create_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o640)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while creating plagiarism run record")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise AgentRunError("plagiarism run record must be a regular file")
    content = path.read_bytes()
    if len(content) > max_bytes:
        raise AgentRunError("plagiarism run record exceeds its size limit")
    return content


def _read_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        value = json.loads(_read_file(path, max_bytes=max_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentRunError("plagiarism run record is invalid JSON") from error
    if not isinstance(value, dict):
        raise AgentRunError("plagiarism run record must be a JSON object")
    return value
