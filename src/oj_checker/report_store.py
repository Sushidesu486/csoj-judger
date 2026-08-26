import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from oj_checker.domain import RunManifest
from oj_checker.immutable_store import write_create_only

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TASK_KEY = re.compile(r"^[a-f0-9]{64}$")


class FileReportStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def write_manifest(self, manifest: RunManifest) -> tuple[RunManifest, Path]:
        if not _SAFE_RUN_ID.fullmatch(manifest.run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore, or dash")

        run_dir = self._root / "runs" / manifest.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "manifest.json"
        temporary = run_dir / f".manifest.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        payload = (
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        try:
            with temporary.open("xb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                existing = json.loads(target.read_text(encoding="utf-8"))
                proposed = manifest.to_dict()
                existing_identity = {
                    key: value for key, value in existing.items() if key != "generated_at"
                }
                proposed_identity = {
                    key: value for key, value in proposed.items() if key != "generated_at"
                }
                if existing_identity != proposed_identity:
                    raise FileExistsError(
                        f"run_id {manifest.run_id!r} already has a different manifest"
                    ) from None
                manifest = replace(
                    manifest,
                    generated_at=datetime.fromisoformat(existing["generated_at"]),
                )
        finally:
            temporary.unlink(missing_ok=True)

        return manifest, target

    def write_task_result(
        self,
        run_id: str,
        task_key: str,
        result: Mapping[str, Any],
    ) -> Path:
        self._validate_run_id(run_id)
        if not _SAFE_TASK_KEY.fullmatch(task_key):
            raise ValueError("task_key must be a lowercase SHA-256 digest")
        target = self._root / "runs" / run_id / "results" / task_key[:2] / f"{task_key}.json"
        return _write_immutable_json(target, result)

    def write_task_attempt(
        self,
        run_id: str,
        task_key: str,
        result: Mapping[str, Any],
    ) -> Path:
        self._validate_run_id(run_id)
        if not _SAFE_TASK_KEY.fullmatch(task_key):
            raise ValueError("task_key must be a lowercase SHA-256 digest")
        attempt_key = _json_digest(result)
        target = (
            self._root
            / "runs"
            / run_id
            / "attempts"
            / task_key[:2]
            / task_key
            / f"{attempt_key}.json"
        )
        return _write_immutable_json(target, result)

    def write_summary(self, run_id: str, summary: Mapping[str, Any]) -> Path:
        self._validate_run_id(run_id)
        target = self._root / "runs" / run_id / "summary.json"
        return _write_immutable_json(target, summary)

    def read_summary(self, run_id: str) -> dict[str, Any] | None:
        self._validate_run_id(run_id)
        target = self._root / "runs" / run_id / "summary.json"
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise RuntimeError(f"run summary is malformed at {target}")
        return value

    def write_attempt_summary(self, run_id: str, summary: Mapping[str, Any]) -> Path:
        self._validate_run_id(run_id)
        target = (
            self._root
            / "runs"
            / run_id
            / "attempt-summaries"
            / f"{_json_digest(summary)}.json"
        )
        return _write_immutable_json(target, summary)

    def write_owner_review(
        self,
        owner: str,
        lab_id: str,
        review_key: str,
        model_response_digest: str,
        review: Mapping[str, Any],
    ) -> Path:
        self._validate_index_component(owner, "owner")
        self._validate_index_component(lab_id, "lab_id")
        if not _SAFE_TASK_KEY.fullmatch(review_key):
            raise ValueError("review_key must be a lowercase SHA-256 digest")
        response_key = hashlib.sha256(model_response_digest.encode()).hexdigest()
        target = (
            self._root
            / "owners"
            / owner
            / f"{lab_id}__{review_key[:16]}__{response_key[:16]}.json"
        )
        return _write_immutable_json(target, review)

    def write_plagiarism_review(
        self,
        lab_id: str,
        review_key: str,
        model_response_digest: str,
        review: Mapping[str, Any],
    ) -> Path:
        self._validate_index_component(lab_id, "lab_id")
        if not _SAFE_TASK_KEY.fullmatch(review_key):
            raise ValueError("review_key must be a lowercase SHA-256 digest")
        response_key = hashlib.sha256(model_response_digest.encode()).hexdigest()
        target = (
            self._root
            / "plagiarism"
            / lab_id
            / f"{review_key[:16]}__{response_key[:16]}.json"
        )
        return _write_immutable_json(target, review)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore, or dash")

    @staticmethod
    def _validate_index_component(value: str, label: str) -> None:
        if not _SAFE_RUN_ID.fullmatch(value):
            raise ValueError(f"{label} is not a safe report path component")


def _write_immutable_json(target: Path, value: Mapping[str, Any]) -> Path:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return write_create_only(
        target,
        payload,
        conflict_message=f"immutable report already exists at {target}",
    )


def _json_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
