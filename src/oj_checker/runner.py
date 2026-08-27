import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

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
from oj_checker.review_basis import ReviewBasis, ReviewBasisProvider
from oj_checker.review_ledger import (
    CompletedReview,
    ModelParameter,
    ReviewIdentity,
    ReviewLedger,
    ReviewTaskType,
)
from oj_checker.reviewer import (
    ComplianceReviewTask,
    PlagiarismReviewTask,
    Reviewer,
    ReviewError,
    ReviewTask,
)
from oj_checker.similarity import (
    BaselineDeltaBuilder,
    SimilarityDetector,
    SimilarityDocument,
    SimilarityPolicy,
)
from oj_checker.submission_store import SourcePolicy, SubmissionStore


@dataclass(frozen=True, slots=True)
class ReviewPipeline:
    submission_store: SubmissionStore
    basis_provider: ReviewBasisProvider
    delta_builder: BaselineDeltaBuilder
    similarity_detector: SimilarityDetector
    similarity_policy: SimilarityPolicy
    reviewer: Reviewer
    ledger: ReviewLedger
    source_policy: SourcePolicy
    model_parameters: tuple[tuple[str, ModelParameter], ...]
    compliance_prompt_version: str = "compliance-v2"
    compliance_schema_version: str = "compliance-result-v1"
    plagiarism_prompt_version: str = "plagiarism-v1"
    plagiarism_schema_version: str = "plagiarism-result-v1"
    source_policy_version: str = "source-bundle-v1"
    delta_version: str = "line-delta-v1"
    similarity_version: str = "minhash-lsh-v1"
    prompt_evidence_chars: int = 240_000
    near_identical_threshold: float = 0.95

    def __post_init__(self) -> None:
        if tuple(sorted(self.model_parameters)) != self.model_parameters:
            raise ValueError("model_parameters must be sorted")
        if self.prompt_evidence_chars <= 0:
            raise ValueError("prompt_evidence_chars must be positive")
        if not 0 <= self.near_identical_threshold <= 1:
            raise ValueError("near_identical_threshold must be between zero and one")


class AuditRunner:
    def __init__(
        self,
        catalog: SubmissionCatalog,
        report_store: FileReportStore,
        *,
        clock: Callable[[], datetime],
        git_commit: str,
        review_pipeline: ReviewPipeline | None = None,
    ) -> None:
        if git_commit in {"", "unknown", "local-dev", "REQUIRED_AT_DEPLOY_TIME"}:
            raise ValueError("git_commit must identify the code used for this audit run")
        self._catalog = catalog
        self._report_store = report_store
        self._clock = clock
        self._git_commit = git_commit
        self._review_pipeline = review_pipeline

    def run(self, request: AuditRequest) -> RunSummary:
        plagiarism = self._catalog.snapshot(
            SnapshotRequest(
                policy=SelectionPolicy.ALL_QUALIFYING,
                cutoff=request.cutoff,
                min_score=request.min_score,
                labs=request.labs,
                owners=request.owners,
                submission_ids=(request.submission_id,) if request.submission_id else (),
                limit=None,
            )
        )
        plagiarism_submissions = plagiarism.submissions
        if request.submission_id:
            if not plagiarism_submissions:
                raise LookupError(f"submission {request.submission_id!r} is not reviewable")
            single_review_submissions = plagiarism_submissions
        else:
            single_review_submissions = best_per_owner_lab(plagiarism_submissions)
        if request.limit is not None:
            single_review_submissions = single_review_submissions[: request.limit]

        review_tasks: dict[str, ReviewTask] = {}
        review_configuration: Mapping[str, Any] = {}
        if request.execute_reviews:
            tasks, review_tasks, review_configuration = self._formal_review_tasks(
                request,
                plagiarism_submissions,
                single_review_submissions,
            )
        else:
            tasks = [self._single_review_task(item) for item in single_review_submissions]
            if not request.submission_id:
                tasks.extend(self._exact_duplicate_tasks(plagiarism_submissions))
        tasks.sort(key=lambda task: (task.kind.value, task.lab_id, task.key))

        manifest = RunManifest(
            schema_version=2 if request.execute_reviews else 1,
            run_id=request.run_id,
            generated_at=self._clock(),
            cutoff=request.cutoff,
            git_commit=self._git_commit,
            min_score=request.min_score,
            labs=request.labs,
            owners=request.owners,
            rules_version=request.rules_version,
            prompt_version=request.prompt_version,
            model=request.model,
            similarity_threshold=request.similarity_threshold,
            completion_count=request.completion_count,
            single_review_corpus_size=len(single_review_submissions),
            plagiarism_corpus_size=(0 if request.submission_id else len(plagiarism_submissions)),
            submissions=plagiarism_submissions,
            tasks=tuple(tasks),
            review_configuration=review_configuration,
        )
        stored_manifest, manifest_path = self._report_store.write_manifest(manifest)
        if not request.execute_reviews:
            return RunSummary(stored_manifest, manifest_path)
        return self._execute_reviews(stored_manifest, manifest_path, review_tasks)

    def _formal_review_tasks(
        self,
        request: AuditRequest,
        plagiarism_submissions: tuple[Submission, ...],
        single_review_submissions: tuple[Submission, ...],
    ) -> tuple[list[AuditTask], dict[str, ReviewTask], Mapping[str, Any]]:
        pipeline = self._review_pipeline
        if pipeline is None:
            raise ValueError("execute_reviews requires a configured review pipeline")
        if not request.model:
            raise ValueError("execute_reviews requires a model")

        labs = sorted({submission.lab_id for submission in plagiarism_submissions})
        bases = {lab_id: pipeline.basis_provider.load(lab_id) for lab_id in labs}
        single_review_ids = {submission.id for submission in single_review_submissions}
        bundles = {}
        documents: dict[str, SimilarityDocument] = {}
        audit_tasks: list[AuditTask] = []
        for submission in plagiarism_submissions:
            bundle = pipeline.submission_store.load_bundle(submission, pipeline.source_policy)
            if submission.id in single_review_ids:
                bundles[submission.id] = bundle
            delta = pipeline.delta_builder.build(bundle, bases[submission.lab_id])
            documents[submission.id] = SimilarityDocument(submission, delta)

        candidates = pipeline.similarity_detector.detect(
            documents.values(),
            pipeline.similarity_policy,
        )
        review_tasks: dict[str, ReviewTask] = {}

        for submission in single_review_submissions:
            document = documents[submission.id]
            basis = bases[submission.lab_id]
            identity = self._compliance_identity(request, pipeline, document, basis)
            compliance_task = ComplianceReviewTask(
                identity=identity,
                owner=submission.owner,
                score=submission.score,
                lab_definition=submission.lab_definition,
                basis=basis,
                source_bundle=bundles[submission.id],
                delta=document.delta,
            )
            review_tasks[identity.key] = compliance_task
            audit_tasks.append(
                AuditTask(
                    key=identity.key,
                    kind=AuditTaskKind.SINGLE_REVIEW,
                    lab_id=submission.lab_id,
                    submission_ids=identity.submission_ids,
                    input_digest=submission.input_digest,
                    input_digests=identity.input_digests,
                    source_delta_digests=identity.source_delta_digests,
                )
            )

        for candidate in () if request.submission_id else candidates.candidates:
            first = documents[candidate.submission_ids[0]]
            second = documents[candidate.submission_ids[1]]
            basis = bases[candidate.lab_id]
            identity = self._plagiarism_identity(
                request,
                pipeline,
                first,
                second,
                basis,
                candidate.signal.value,
                candidate.jaccard,
            )
            plagiarism_task = PlagiarismReviewTask(
                identity=identity,
                owners=(first.submission.owner, second.submission.owner),
                submitted_at=(
                    first.submission.submitted_at,
                    second.submission.submitted_at,
                ),
                signal=candidate.signal,
                jaccard=candidate.jaccard,
                deltas=(first.delta, second.delta),
            )
            review_tasks[identity.key] = plagiarism_task
            audit_tasks.append(
                AuditTask(
                    key=identity.key,
                    kind=AuditTaskKind.PLAGIARISM_REVIEW,
                    lab_id=candidate.lab_id,
                    submission_ids=identity.submission_ids,
                    input_digest=self._task_key({"input_digests": identity.input_digests}),
                    input_digests=identity.input_digests,
                    source_delta_digests=identity.source_delta_digests,
                    similarity_signal=candidate.signal.value,
                    jaccard=candidate.jaccard,
                )
            )

        review_configuration = {
            "basis_revision": pipeline.basis_provider.upstream_commit,
            "basis_by_lab": {
                lab_id: {
                    "tree_digest": basis.tree_digest,
                    "document_digest": basis.document_digest,
                    "source_path": basis.source_path,
                    "document_path": basis.document_path,
                }
                for lab_id, basis in sorted(bases.items())
            },
            "compliance_prompt_version": pipeline.compliance_prompt_version,
            "compliance_schema_version": pipeline.compliance_schema_version,
            "plagiarism_prompt_version": pipeline.plagiarism_prompt_version,
            "plagiarism_schema_version": pipeline.plagiarism_schema_version,
            "source_policy_version": pipeline.source_policy_version,
            "delta_version": pipeline.delta_version,
            "similarity_version": pipeline.similarity_version,
            "similarity_policy": self._similarity_parameters(pipeline),
            "similarity_workers": pipeline.similarity_detector.max_workers,
            "similarity_exclusions": [
                {
                    "submission_id": exclusion.submission_id,
                    "lab_id": exclusion.lab_id,
                    "reason": exclusion.reason,
                    "skipped_layers": list(exclusion.skipped_layers),
                }
                for exclusion in candidates.exclusions
            ],
            "model_parameters": dict(pipeline.model_parameters),
            "prompt_evidence_chars": pipeline.prompt_evidence_chars,
            "near_identical_threshold": pipeline.near_identical_threshold,
        }
        if request.submission_id:
            review_configuration = {
                **review_configuration,
                "single_submission_id": request.submission_id,
            }
        return audit_tasks, review_tasks, review_configuration

    def _execute_reviews(
        self,
        manifest: RunManifest,
        manifest_path: Path,
        review_tasks: Mapping[str, ReviewTask],
    ) -> RunSummary:
        pipeline = self._review_pipeline
        if pipeline is None:
            raise RuntimeError("review pipeline disappeared during execution")
        existing_summary = self._report_store.read_summary(manifest.run_id)
        if existing_summary is not None:
            return RunSummary(
                manifest=manifest,
                manifest_path=manifest_path,
                cache_hit_count=_summary_count(existing_summary, "cache_hit_count"),
                llm_call_count=_summary_count(existing_summary, "llm_call_count"),
                completed_review_count=_summary_count(
                    existing_summary,
                    "completed_review_count",
                ),
                inconclusive_review_count=_summary_count(
                    existing_summary,
                    "inconclusive_review_count",
                ),
                failed_review_count=_summary_count(existing_summary, "failed_review_count"),
                similarity_exclusion_count=_summary_count(
                    existing_summary,
                    "similarity_exclusion_count",
                ),
            )
        cache_hits = 0
        llm_calls = 0
        completed = 0
        inconclusive = 0
        failed = 0
        result_paths = []
        for task in manifest.tasks:
            review_task = review_tasks[task.key]
            cached = pipeline.ledger.lookup(review_task.identity)
            resolved_review: CompletedReview | None = None
            if cached is not None:
                cache_hits += 1
                completed += 1
                resolved_review = cached
                payload = {
                    "task_key": task.key,
                    "kind": task.kind.value,
                    "review": cached.to_dict(),
                }
                result_path = self._report_store.write_task_result(
                    manifest.run_id,
                    task.key,
                    payload,
                )
            else:
                llm_calls += 1
                try:
                    review = pipeline.reviewer.review(review_task)
                except ReviewError as error:
                    failed += 1
                    payload = {
                        "task_key": task.key,
                        "kind": task.kind.value,
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "message": str(error),
                    }
                    result_path = self._report_store.write_task_attempt(
                        manifest.run_id,
                        task.key,
                        payload,
                    )
                else:
                    resolved_review = review
                    if review.conclusive:
                        pipeline.ledger.record(review)
                        completed += 1
                        payload = {
                            "task_key": task.key,
                            "kind": task.kind.value,
                            "review": review.to_dict(),
                        }
                        result_path = self._report_store.write_task_result(
                            manifest.run_id,
                            task.key,
                            payload,
                        )
                    else:
                        inconclusive += 1
                        payload = {
                            "task_key": task.key,
                            "kind": task.kind.value,
                            "status": "inconclusive",
                            "review": review.to_dict(),
                        }
                        result_path = self._report_store.write_task_attempt(
                            manifest.run_id,
                            task.key,
                            payload,
                        )
            result_paths.append(result_path)
            if resolved_review is not None:
                derived_payload = {
                    "task_key": task.key,
                    "kind": task.kind.value,
                    "review": resolved_review.to_dict(),
                }
                if isinstance(review_task, ComplianceReviewTask):
                    result_paths.append(
                        self._report_store.write_owner_review(
                            review_task.owner,
                            task.lab_id,
                            task.key,
                            resolved_review.model_response_digest,
                            {
                                **derived_payload,
                                "submission_id": task.submission_ids[0],
                                "score": review_task.score,
                            },
                        )
                    )
                else:
                    result_paths.append(
                        self._report_store.write_plagiarism_review(
                            task.lab_id,
                            task.key,
                            resolved_review.model_response_digest,
                            {
                                **derived_payload,
                                "submission_ids": list(task.submission_ids),
                                "owners": list(review_task.owners),
                                "submitted_at": [
                                    value.isoformat() for value in review_task.submitted_at
                                ],
                                "similarity_signal": review_task.signal.value,
                                "jaccard": review_task.jaccard,
                                "human_review_status": "pending",
                            },
                        )
                    )

        summary_payload = {
            "run_id": manifest.run_id,
            "task_count": len(manifest.tasks),
            "cache_hit_count": cache_hits,
            "llm_call_count": llm_calls,
            "completed_review_count": completed,
            "inconclusive_review_count": inconclusive,
            "failed_review_count": failed,
            "similarity_exclusion_count": _similarity_exclusion_count(manifest),
        }
        if failed or inconclusive:
            self._report_store.write_attempt_summary(manifest.run_id, summary_payload)
        else:
            self._report_store.write_summary(manifest.run_id, summary_payload)
        return RunSummary(
            manifest=manifest,
            manifest_path=manifest_path,
            cache_hit_count=cache_hits,
            llm_call_count=llm_calls,
            completed_review_count=completed,
            inconclusive_review_count=inconclusive,
            failed_review_count=failed,
            similarity_exclusion_count=_similarity_exclusion_count(manifest),
            result_paths=tuple(result_paths),
        )

    @classmethod
    def _compliance_identity(
        cls,
        request: AuditRequest,
        pipeline: ReviewPipeline,
        document: SimilarityDocument,
        basis: ReviewBasis,
    ) -> ReviewIdentity:
        submission = document.submission
        task_parameters: dict[str, ModelParameter] = {
            "delta_version": pipeline.delta_version,
            "source_policy_version": pipeline.source_policy_version,
            "max_file_bytes": pipeline.source_policy.max_file_bytes,
            "max_total_bytes": pipeline.source_policy.max_total_bytes,
            "prompt_evidence_chars": pipeline.prompt_evidence_chars,
        }
        return ReviewIdentity(
            task_type=ReviewTaskType.COMPLIANCE,
            submission_ids=(submission.id,),
            input_digests=(submission.input_digest,),
            source_delta_digests=(document.delta.digest,),
            lab_id=submission.lab_id,
            basis_commit=basis.upstream_commit,
            basis_tree_digest=basis.tree_digest,
            document_digest=basis.document_digest,
            lab_definition_digest=cls._json_digest(submission.lab_definition),
            rules_version=request.rules_version,
            prompt_version=pipeline.compliance_prompt_version,
            schema_version=pipeline.compliance_schema_version,
            model=request.model or "",
            model_parameters=pipeline.model_parameters,
            task_parameters=tuple(sorted(task_parameters.items())),
        )

    @classmethod
    def _plagiarism_identity(
        cls,
        request: AuditRequest,
        pipeline: ReviewPipeline,
        first: SimilarityDocument,
        second: SimilarityDocument,
        basis: ReviewBasis,
        signal: str,
        jaccard: float,
    ) -> ReviewIdentity:
        definition_digests = (
            cls._json_digest(first.submission.lab_definition),
            cls._json_digest(second.submission.lab_definition),
        )
        task_parameters: dict[str, ModelParameter] = {
            "band_size": pipeline.similarity_policy.band_size,
            "delta_version": pipeline.delta_version,
            "jaccard": jaccard,
            "jaccard_threshold": pipeline.similarity_policy.jaccard_threshold,
            "num_permutations": pipeline.similarity_policy.num_permutations,
            "near_identical_threshold": pipeline.near_identical_threshold,
            "prompt_evidence_chars": pipeline.prompt_evidence_chars,
            "shingle_size": pipeline.similarity_policy.shingle_size,
            "signal": signal,
            "similarity_version": pipeline.similarity_version,
            "tokenizer_version": pipeline.similarity_policy.tokenizer_version,
        }
        return ReviewIdentity(
            task_type=ReviewTaskType.PLAGIARISM,
            submission_ids=(first.submission.id, second.submission.id),
            input_digests=(
                first.submission.input_digest,
                second.submission.input_digest,
            ),
            source_delta_digests=(first.delta.digest, second.delta.digest),
            lab_id=first.submission.lab_id,
            basis_commit=basis.upstream_commit,
            basis_tree_digest=basis.tree_digest,
            document_digest=basis.document_digest,
            lab_definition_digest=cls._task_key(
                {"lab_definition_digests": definition_digests}
            ),
            rules_version=request.rules_version,
            prompt_version=pipeline.plagiarism_prompt_version,
            schema_version=pipeline.plagiarism_schema_version,
            model=request.model or "",
            model_parameters=pipeline.model_parameters,
            task_parameters=tuple(sorted(task_parameters.items())),
        )

    @staticmethod
    def _similarity_parameters(pipeline: ReviewPipeline) -> Mapping[str, Any]:
        policy = pipeline.similarity_policy
        return {
            "jaccard_threshold": policy.jaccard_threshold,
            "shingle_size": policy.shingle_size,
            "num_permutations": policy.num_permutations,
            "band_size": policy.band_size,
            "tokenizer_version": policy.tokenizer_version,
        }

    @staticmethod
    def _json_digest(value: Mapping[str, Any]) -> str:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
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
            representatives: dict[str, Submission] = {}
            for member in sorted(members, key=lambda item: (item.submitted_at, item.id)):
                representatives.setdefault(member.owner, member)
            if len(representatives) < 2:
                continue

            ordered = sorted(representatives.values(), key=lambda item: item.owner)
            for first, second in combinations(ordered, 2):
                pair = sorted((first, second), key=lambda item: (item.submitted_at, item.id))
                submission_ids = (pair[0].id, pair[1].id)
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


def _summary_count(summary: Mapping[str, Any], name: str) -> int:
    value = summary.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"run summary field {name!r} is malformed")
    return value


def _similarity_exclusion_count(manifest: RunManifest) -> int:
    value = manifest.review_configuration.get("similarity_exclusions", [])
    if not isinstance(value, list):
        raise RuntimeError("manifest similarity_exclusions is malformed")
    return len(value)
