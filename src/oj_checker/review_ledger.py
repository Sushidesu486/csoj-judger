import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from oj_checker.immutable_store import write_create_only

type ModelParameter = str | int | float | bool | None

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReviewTaskType(StrEnum):
    COMPLIANCE = "compliance"
    PLAGIARISM = "plagiarism"


@dataclass(frozen=True, slots=True)
class ReviewIdentity:
    task_type: ReviewTaskType
    submission_ids: tuple[str, ...]
    input_digests: tuple[str, ...]
    source_delta_digests: tuple[str, ...]
    lab_id: str
    basis_commit: str
    basis_tree_digest: str
    document_digest: str
    lab_definition_digest: str
    rules_version: str
    prompt_version: str
    schema_version: str
    model: str
    model_parameters: tuple[tuple[str, ModelParameter], ...]
    task_parameters: tuple[tuple[str, ModelParameter], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_type", ReviewTaskType(self.task_type))
        count = len(self.submission_ids)
        if count == 0:
            raise ValueError("review identity requires at least one submission")
        if len(self.input_digests) != count or len(self.source_delta_digests) != count:
            raise ValueError("review identity digest counts must match submission IDs")
        if tuple(sorted(self.model_parameters)) != self.model_parameters:
            raise ValueError("model_parameters must be sorted")
        if tuple(sorted(self.task_parameters)) != self.task_parameters:
            raise ValueError("task_parameters must be sorted")

    @property
    def key(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type.value,
            "submission_ids": list(self.submission_ids),
            "input_digests": list(self.input_digests),
            "source_delta_digests": list(self.source_delta_digests),
            "lab_id": self.lab_id,
            "basis_commit": self.basis_commit,
            "basis_tree_digest": self.basis_tree_digest,
            "document_digest": self.document_digest,
            "lab_definition_digest": self.lab_definition_digest,
            "rules_version": self.rules_version,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "model": self.model,
            "model_parameters": dict(self.model_parameters),
            "task_parameters": dict(self.task_parameters),
        }


@dataclass(frozen=True, slots=True)
class CompletedReview:
    identity: ReviewIdentity
    completed_at: datetime
    verdict: Mapping[str, Any]
    model_response_digest: str
    conclusive: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_key": self.identity.key,
            "identity": self.identity.to_dict(),
            "completed_at": self.completed_at.isoformat(),
            "verdict": dict(self.verdict),
            "model_response_digest": self.model_response_digest,
            "conclusive": self.conclusive,
            "evidence": dict(self.evidence),
        }


class ReviewLedger(Protocol):
    def lookup(self, identity: ReviewIdentity) -> CompletedReview | None: ...

    def record(self, review: CompletedReview) -> Path: ...


class FileReviewLedger:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def lookup(self, identity: ReviewIdentity) -> CompletedReview | None:
        target = self._path(identity)
        try:
            stored = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(stored, dict) or stored.get("identity") != identity.to_dict():
            raise RuntimeError(f"review ledger identity mismatch for {identity.key}")
        if stored.get("review_key") != identity.key:
            raise RuntimeError(f"review ledger key mismatch for {identity.key}")
        if stored.get("conclusive") is not True:
            return None
        verdict = stored.get("verdict")
        if not isinstance(verdict, dict):
            raise RuntimeError(f"review ledger verdict is malformed for {identity.key}")
        completed_at = datetime.fromisoformat(str(stored.get("completed_at")))
        response_digest = stored.get("model_response_digest")
        if not isinstance(response_digest, str):
            raise RuntimeError(f"review ledger response digest is malformed for {identity.key}")
        evidence = stored.get("evidence", {})
        if not isinstance(evidence, dict):
            raise RuntimeError(f"review ledger evidence is malformed for {identity.key}")
        return CompletedReview(
            identity=identity,
            completed_at=completed_at,
            verdict=verdict,
            model_response_digest=response_digest,
            conclusive=True,
            evidence=evidence,
        )

    def record(self, review: CompletedReview) -> Path:
        if not review.conclusive:
            raise ValueError("only conclusive completed reviews may enter the review cache")
        target = self._path(review.identity)
        payload = _canonical_json(review.to_dict()) + b"\n"
        return write_create_only(
            target,
            payload,
            conflict_message=(
                f"review key {review.identity.key!r} already has a different result"
            ),
        )

    def _path(self, identity: ReviewIdentity) -> Path:
        if not _SAFE_COMPONENT.fullmatch(identity.schema_version):
            raise ValueError("schema_version is not a safe path component")
        return (
            self._root
            / "cache"
            / "review"
            / identity.schema_version
            / identity.key[:2]
            / f"{identity.key}.json"
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
