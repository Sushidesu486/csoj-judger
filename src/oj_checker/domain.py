import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class SelectionPolicy(StrEnum):
    BEST_PER_OWNER_LAB = "best_per_owner_lab"
    ALL_QUALIFYING = "all_qualifying"


class AuditTaskKind(StrEnum):
    SINGLE_REVIEW = "single_review"
    EXACT_DUPLICATE = "exact_duplicate"
    PLAGIARISM_REVIEW = "plagiarism_review"


@dataclass(frozen=True, slots=True)
class Submission:
    id: str
    owner: str
    lab_id: str
    score: int
    input_digest: str
    submitted_at: datetime
    input_manifest: Mapping[str, Any]
    lab_definition: Mapping[str, Any]
    active_run_id: int | None = None
    run_state: str | None = None
    run_result_info: Mapping[str, Any] = field(default_factory=dict)
    run_failure_class: str | None = None
    run_failure_reason: str | None = None
    run_score: int | None = None
    run_performance: float | None = None
    run_finished_at: datetime | None = None
    status: str = "Success"
    is_valid: bool = True

    def __post_init__(self) -> None:
        if self.submitted_at.tzinfo is None or self.submitted_at.utcoffset() is None:
            raise ValueError("submitted_at must be timezone-aware")
        object.__setattr__(self, "submitted_at", self.submitted_at.astimezone(UTC))
        if self.run_finished_at is not None:
            if self.run_finished_at.tzinfo is None or self.run_finished_at.utcoffset() is None:
                raise ValueError("run_finished_at must be timezone-aware")
            object.__setattr__(self, "run_finished_at", self.run_finished_at.astimezone(UTC))

    def to_manifest_entry(self, lab_definition_key: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "lab_id": self.lab_id,
            "score": self.score,
            "input_digest": self.input_digest,
            "submitted_at": self.submitted_at.isoformat(),
            "input_manifest": dict(self.input_manifest),
            "lab_definition_key": lab_definition_key,
            "active_run": {
                "id": self.active_run_id,
                "state": self.run_state,
                "result_info": dict(self.run_result_info),
                "failure_class": self.run_failure_class,
                "failure_reason": self.run_failure_reason,
                "score": self.run_score,
                "performance": self.run_performance,
                "finished_at": (
                    self.run_finished_at.isoformat() if self.run_finished_at is not None else None
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    policy: SelectionPolicy
    cutoff: datetime
    min_score: int = 60
    labs: tuple[str, ...] = field(default_factory=tuple)
    owners: tuple[str, ...] = field(default_factory=tuple)
    limit: int | None = None
    submission_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("snapshot cutoff must be timezone-aware")
        object.__setattr__(self, "cutoff", self.cutoff.astimezone(UTC))
        if self.limit is not None and self.limit <= 0:
            raise ValueError("snapshot limit must be positive")
        if any(not submission_id.strip() for submission_id in self.submission_ids):
            raise ValueError("snapshot submission IDs must not be empty")


@dataclass(frozen=True, slots=True)
class SubmissionSnapshot:
    policy: SelectionPolicy
    cutoff: datetime
    submissions: tuple[Submission, ...]


@dataclass(frozen=True, slots=True)
class AuditRequest:
    run_id: str
    cutoff: datetime
    min_score: int = 60
    labs: tuple[str, ...] = field(default_factory=tuple)
    owners: tuple[str, ...] = field(default_factory=tuple)
    limit: int | None = None
    rules_version: str = "audit-rules-v1"
    prompt_version: str | None = None
    model: str | None = None
    similarity_threshold: float | None = None
    completion_count: int = 1
    submission_id: str | None = None
    execute_reviews: bool = False

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("audit cutoff must be timezone-aware")
        object.__setattr__(self, "cutoff", self.cutoff.astimezone(UTC))
        if self.limit is not None and self.limit <= 0:
            raise ValueError("audit limit must be positive")
        if self.completion_count <= 0:
            raise ValueError("completion_count must be positive")
        if self.submission_id is not None and not self.submission_id.strip():
            raise ValueError("submission_id must not be empty")


@dataclass(frozen=True, slots=True)
class AuditTask:
    key: str
    kind: AuditTaskKind
    lab_id: str
    submission_ids: tuple[str, ...]
    input_digest: str
    input_digests: tuple[str, ...] = field(default_factory=tuple)
    source_delta_digests: tuple[str, ...] = field(default_factory=tuple)
    similarity_signal: str | None = None
    jaccard: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "lab_id": self.lab_id,
            "submission_ids": list(self.submission_ids),
            "input_digest": self.input_digest,
            "input_digests": list(self.input_digests),
            "source_delta_digests": list(self.source_delta_digests),
            "similarity_signal": self.similarity_signal,
            "jaccard": self.jaccard,
        }


@dataclass(frozen=True, slots=True)
class RunManifest:
    schema_version: int
    run_id: str
    generated_at: datetime
    cutoff: datetime
    git_commit: str
    min_score: int
    labs: tuple[str, ...]
    owners: tuple[str, ...]
    rules_version: str
    prompt_version: str | None
    model: str | None
    similarity_threshold: float | None
    completion_count: int
    single_review_corpus_size: int
    plagiarism_corpus_size: int
    submissions: tuple[Submission, ...]
    tasks: tuple[AuditTask, ...]
    review_configuration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("manifest cutoff must be timezone-aware")
        object.__setattr__(self, "generated_at", self.generated_at.astimezone(UTC))
        object.__setattr__(self, "cutoff", self.cutoff.astimezone(UTC))

    @property
    def task_counts(self) -> dict[AuditTaskKind, int]:
        counts: dict[AuditTaskKind, int] = {}
        for task in self.tasks:
            counts[task.kind] = counts.get(task.kind, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        lab_definitions: dict[str, Mapping[str, Any]] = {}
        submissions = []
        for submission in self.submissions:
            lab_definition_key = _json_fingerprint(submission.lab_definition)
            lab_definitions[lab_definition_key] = submission.lab_definition
            submissions.append(submission.to_manifest_entry(lab_definition_key))

        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "git_commit": self.git_commit,
            "min_score": self.min_score,
            "labs": list(self.labs),
            "owners": list(self.owners),
            "rules_version": self.rules_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "similarity_threshold": self.similarity_threshold,
            "completion_count": self.completion_count,
            "single_review_corpus_size": self.single_review_corpus_size,
            "plagiarism_corpus_size": self.plagiarism_corpus_size,
            "task_counts": {kind.value: count for kind, count in self.task_counts.items()},
            "lab_definitions": lab_definitions,
            "submissions": submissions,
            "tasks": [task.to_dict() for task in self.tasks],
            "review_configuration": dict(self.review_configuration),
        }


@dataclass(frozen=True, slots=True)
class RunSummary:
    manifest: RunManifest
    manifest_path: Path
    cache_hit_count: int = 0
    llm_call_count: int = 0
    completed_review_count: int = 0
    inconclusive_review_count: int = 0
    failed_review_count: int = 0
    similarity_exclusion_count: int = 0
    result_paths: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def task_counts(self) -> dict[AuditTaskKind, int]:
        return self.manifest.task_counts


def _json_fingerprint(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
