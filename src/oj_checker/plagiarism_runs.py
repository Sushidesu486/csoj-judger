from __future__ import annotations

import json
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
_QUEUE_STOP = object()


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
        if not public_keys:
            raise ValueError("at least one plagiarism bundle public key is required")
        self._root = Path(root) / "plagiarism-runs"
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_keys = dict(public_keys)
        self._allowed_models = frozenset(models)
        self._executor = executor
        self._worker_count = worker_count
        self._max_request_bytes = max_request_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=max_queued)
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._recover()
            for index in range(self._worker_count):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"oj-plagiarism-run-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def close(self) -> None:
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
            self._started = False
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
            if self._executor is not None and self._worker_count:
                self._queue.put_nowait(run_id)
            return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        run_root = self._run_root(run_id)
        metadata = _read_json_object(run_root / "metadata.json", max_bytes=64 * 1024)
        event = self._latest_event(run_root)
        response = {**metadata, **event}
        result_path = run_root / "result.json"
        if result_path.is_file() and not result_path.is_symlink():
            response["result"] = _read_json_object(result_path, max_bytes=16 << 20)
        return response

    def latest(self, submission_id: str) -> dict[str, Any] | None:
        canonical = _canonical_submission_id(submission_id)
        candidate: tuple[str, str] | None = None
        for run_root in self._safe_run_roots():
            try:
                metadata = _read_json_object(run_root / "metadata.json", max_bytes=64 * 1024)
            except (AgentRunError, OSError):
                continue
            if metadata.get("submission_id") != canonical:
                continue
            created_at = metadata.get("created_at")
            run_id = metadata.get("run_id")
            if not isinstance(created_at, str) or not isinstance(run_id, str):
                continue
            current = (created_at, run_id)
            if candidate is None or current > candidate:
                candidate = current
        if candidate is None:
            return None
        return self.get(candidate[1])

    def _recover(self) -> None:
        for run_root in self._safe_run_roots():
            try:
                event = self._latest_event(run_root)
            except (AgentRunError, OSError):
                continue
            state = event.get("state")
            if state in _ACTIVE_STATES:
                self._append_event(run_root.name, "failed", error_code="JOB_LOST")
            elif state == "queued" and self._executor is not None and self._worker_count:
                try:
                    self._queue.put_nowait(run_root.name)
                except queue.Full:
                    break

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
