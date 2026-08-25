from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class SelectionPolicy(StrEnum):
    BEST_PER_OWNER_LAB = "best_per_owner_lab"
    ALL_QUALIFYING = "all_qualifying"


class AuditTaskKind(StrEnum):
    SINGLE_REVIEW = "single_review"
    EXACT_DUPLICATE = "exact_duplicate"


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
    status: str = "Success"
    is_valid: bool = True


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    policy: SelectionPolicy
    cutoff: datetime
    min_score: int = 60
    labs: tuple[str, ...] = field(default_factory=tuple)
    owners: tuple[str, ...] = field(default_factory=tuple)
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("snapshot cutoff must be timezone-aware")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("snapshot limit must be positive")


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

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            raise ValueError("audit cutoff must be timezone-aware")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("audit limit must be positive")


@dataclass(frozen=True, slots=True)
class AuditTask:
    key: str
    kind: AuditTaskKind
    lab_id: str
    submission_ids: tuple[str, ...]
    input_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "lab_id": self.lab_id,
            "submission_ids": list(self.submission_ids),
            "input_digest": self.input_digest,
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
    single_review_corpus_size: int
    plagiarism_corpus_size: int
    tasks: tuple[AuditTask, ...]

    @property
    def task_counts(self) -> dict[AuditTaskKind, int]:
        counts = {kind: 0 for kind in AuditTaskKind}
        for task in self.tasks:
            counts[task.kind] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "git_commit": self.git_commit,
            "min_score": self.min_score,
            "labs": list(self.labs),
            "owners": list(self.owners),
            "single_review_corpus_size": self.single_review_corpus_size,
            "plagiarism_corpus_size": self.plagiarism_corpus_size,
            "task_counts": {kind.value: count for kind, count in self.task_counts.items()},
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass(frozen=True, slots=True)
class RunSummary:
    manifest: RunManifest
    manifest_path: Path

    @property
    def task_counts(self) -> dict[AuditTaskKind, int]:
        return self.manifest.task_counts
