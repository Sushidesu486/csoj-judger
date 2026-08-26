from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from oj_checker.domain import (
    SelectionPolicy,
    SnapshotRequest,
    Submission,
    SubmissionSnapshot,
)


class SubmissionCatalog(Protocol):
    def snapshot(self, request: SnapshotRequest) -> SubmissionSnapshot: ...


class InMemorySubmissionCatalog:
    def __init__(self, submissions: Iterable[Submission]) -> None:
        self._submissions = tuple(submissions)

    def snapshot(self, request: SnapshotRequest) -> SubmissionSnapshot:
        eligible = [item for item in self._submissions if self._is_eligible(item, request)]

        if request.policy is SelectionPolicy.BEST_PER_OWNER_LAB:
            selected = list(best_per_owner_lab(eligible))
        else:
            selected = eligible

        selected.sort(key=lambda item: (item.owner, item.lab_id, item.submitted_at, item.id))
        if request.limit is not None:
            selected = selected[: request.limit]

        return SubmissionSnapshot(request.policy, request.cutoff, tuple(selected))

    @staticmethod
    def _is_eligible(item: Submission, request: SnapshotRequest) -> bool:
        return (
            item.status == "Success"
            and item.is_valid
            and item.score >= request.min_score
            and item.submitted_at <= request.cutoff
            and (not request.labs or item.lab_id in request.labs)
            and (not request.owners or item.owner in request.owners)
            and (not request.submission_ids or item.id in request.submission_ids)
        )


def best_per_owner_lab(submissions: Iterable[Submission]) -> tuple[Submission, ...]:
    best: dict[tuple[str, str], Submission] = {}
    for item in submissions:
        key = (item.owner, item.lab_id)
        current = best.get(key)
        if current is None or _rank(item) > _rank(current):
            best[key] = item
    return tuple(sorted(best.values(), key=lambda item: (item.owner, item.lab_id, item.id)))


def _rank(item: Submission) -> tuple[int, datetime, str]:
    return item.score, item.submitted_at, item.id
