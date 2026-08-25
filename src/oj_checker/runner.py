import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime

from oj_checker.catalog import SubmissionCatalog, best_per_owner_lab
from oj_checker.domain import (
    AuditRequest,
    AuditTask,
    AuditTaskKind,
    RunManifest,
    RunSummary,
    SelectionPolicy,
    SnapshotRequest,
    Submission,
)
from oj_checker.report_store import FileReportStore


class AuditRunner:
    def __init__(
        self,
        catalog: SubmissionCatalog,
        report_store: FileReportStore,
        *,
        clock: Callable[[], datetime],
        git_commit: str,
    ) -> None:
        self._catalog = catalog
        self._report_store = report_store
        self._clock = clock
        self._git_commit = git_commit

    def run(self, request: AuditRequest) -> RunSummary:
        plagiarism = self._catalog.snapshot(
            SnapshotRequest(
                policy=SelectionPolicy.ALL_QUALIFYING,
                cutoff=request.cutoff,
                min_score=request.min_score,
                labs=request.labs,
                owners=request.owners,
                limit=None,
            )
        )
        plagiarism_submissions = plagiarism.submissions
        single_review_submissions = best_per_owner_lab(plagiarism_submissions)
        if request.limit is not None:
            plagiarism_submissions = plagiarism_submissions[: request.limit]
            single_review_submissions = single_review_submissions[: request.limit]

        tasks = [self._single_review_task(item) for item in single_review_submissions]
        tasks.extend(self._exact_duplicate_tasks(plagiarism_submissions))
        tasks.sort(key=lambda task: (task.kind.value, task.lab_id, task.key))

        manifest = RunManifest(
            schema_version=1,
            run_id=request.run_id,
            generated_at=self._clock(),
            cutoff=request.cutoff,
            git_commit=self._git_commit,
            min_score=request.min_score,
            labs=request.labs,
            owners=request.owners,
            single_review_corpus_size=len(single_review_submissions),
            plagiarism_corpus_size=len(plagiarism_submissions),
            tasks=tuple(tasks),
        )
        return RunSummary(manifest, self._report_store.write_manifest(manifest))

    @classmethod
    def _single_review_task(cls, submission: Submission) -> AuditTask:
        payload = {
            "kind": AuditTaskKind.SINGLE_REVIEW.value,
            "submission_id": submission.id,
            "input_digest": submission.input_digest,
        }
        return AuditTask(
            key=cls._task_key(payload),
            kind=AuditTaskKind.SINGLE_REVIEW,
            lab_id=submission.lab_id,
            submission_ids=(submission.id,),
            input_digest=submission.input_digest,
        )

    @classmethod
    def _exact_duplicate_tasks(
        cls, submissions: tuple[Submission, ...]
    ) -> list[AuditTask]:
        groups: dict[tuple[str, str], list[Submission]] = defaultdict(list)
        for submission in submissions:
            if submission.input_digest:
                groups[(submission.lab_id, submission.input_digest)].append(submission)

        tasks = []
        for (lab_id, digest), members in groups.items():
            if len({member.owner for member in members}) < 2:
                continue
            submission_ids = tuple(sorted(member.id for member in members))
            payload = {
                "kind": AuditTaskKind.EXACT_DUPLICATE.value,
                "lab_id": lab_id,
                "input_digest": digest,
                "submission_ids": submission_ids,
            }
            tasks.append(
                AuditTask(
                    key=cls._task_key(payload),
                    kind=AuditTaskKind.EXACT_DUPLICATE,
                    lab_id=lab_id,
                    submission_ids=submission_ids,
                    input_digest=digest,
                )
            )
        return tasks

    @staticmethod
    def _task_key(payload: Mapping[str, object]) -> str:
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
