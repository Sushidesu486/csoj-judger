from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oj_checker.agent_runs import AgentRunFailure
from oj_checker.plagiarism_bundle import submissions_from_plagiarism_bundle
from oj_checker.report_api import FilePlagiarismReportReader
from oj_checker.report_store import FileReportStore
from oj_checker.review_basis import GitReviewBasisProvider, UnsupportedLabError
from oj_checker.review_bundle import VerifiedReviewBundle
from oj_checker.review_ledger import (
    FileReviewLedger,
    ModelParameter,
    ReviewIdentity,
    ReviewTaskType,
)
from oj_checker.reviewer import PlagiarismReviewTask, Reviewer, ReviewError
from oj_checker.similarity import (
    BaselineDeltaBuilder,
    SimilarityDetector,
    SimilarityDocument,
    SimilarityPolicy,
)
from oj_checker.submission_store import NfsSubmissionStore, SourcePolicy, UnsafeSubmissionPath


class LocalPlagiarismRunExecutor:
    """Run one signed target against a signed same-Lab corpus without database access."""

    def __init__(
        self,
        *,
        oj_root: str | Path,
        report_root: str | Path,
        hpc101_repository: str | Path,
        reviewer: Reviewer,
        max_candidates: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self._store = NfsSubmissionStore(oj_root)
        self._report_store = FileReportStore(report_root)
        self._report_reader = FilePlagiarismReportReader(report_root, refresh_seconds=0)
        self._ledger = FileReviewLedger(report_root)
        self._hpc101_repository = Path(hpc101_repository)
        self._reviewer = reviewer
        self._max_candidates = max_candidates
        self._clock = clock or (lambda: datetime.now(UTC))
        self._source_policy = SourcePolicy(max_file_bytes=10 << 20, max_total_bytes=10 << 20)
        self._similarity_policy = SimilarityPolicy(
            jaccard_threshold=0.7,
            shingle_size=5,
            num_permutations=64,
            band_size=4,
        )

    def execute(self, bundle: VerifiedReviewBundle) -> Mapping[str, Any]:
        target_id, submissions = submissions_from_plagiarism_bundle(bundle)
        lab_id = submissions[0].lab_id
        raw_basis = bundle.payload.get("basis")
        if not isinstance(raw_basis, Mapping):
            raise AgentRunFailure("BASELINE_MISSING")
        revision = raw_basis.get("commit")
        if not isinstance(revision, str):
            raise AgentRunFailure("BASELINE_MISSING")
        try:
            basis = GitReviewBasisProvider(self._hpc101_repository, revision).load(lab_id)
        except (FileNotFoundError, RuntimeError, UnsupportedLabError, ValueError) as error:
            raise AgentRunFailure("BASELINE_MISSING") from error
        expected_basis = {
            "commit": basis.upstream_commit,
            "tree_digest": basis.tree_digest,
            "document_digest": basis.document_digest,
            "source_path": basis.source_path,
            "document_path": basis.document_path,
        }
        if any(raw_basis.get(key) != value for key, value in expected_basis.items()):
            raise AgentRunFailure("BASELINE_MISSING")

        delta_builder = BaselineDeltaBuilder()
        documents: dict[str, SimilarityDocument] = {}
        try:
            for submission in submissions:
                source = self._store.load_bundle(submission, self._source_policy)
                delta = delta_builder.build(source, basis)
                documents[submission.id] = SimilarityDocument(submission, delta)
        except (FileNotFoundError, OSError, UnsafeSubmissionPath, ValueError) as error:
            raise AgentRunFailure("SOURCE_BUNDLE_INVALID") from error
        candidates = SimilarityDetector(max_workers=1).detect_for_submission(
            target_id,
            documents.values(),
            self._similarity_policy,
        )
        if len(candidates.candidates) > self._max_candidates:
            raise AgentRunFailure("TOO_MANY_CANDIDATES")

        model = bundle.payload.get("model")
        rules_version = bundle.payload.get("rules_version")
        prompt_version = bundle.payload.get("prompt_version")
        result_schema_version = bundle.payload.get("result_schema_version")
        if not all(
            isinstance(value, str)
            for value in (model, rules_version, prompt_version, result_schema_version)
        ):
            raise AgentRunFailure("INVALID_PLAGIARISM_REQUEST")
        reviewed_count = 0
        failed_count = 0
        for candidate in candidates.candidates:
            first = documents[candidate.submission_ids[0]]
            second = documents[candidate.submission_ids[1]]
            identity = self._identity(
                first,
                second,
                basis.upstream_commit,
                basis.tree_digest,
                basis.document_digest,
                candidate.signal.value,
                candidate.jaccard,
                str(model),
                str(rules_version),
                str(prompt_version),
                str(result_schema_version),
            )
            task = PlagiarismReviewTask(
                identity=identity,
                owners=(first.submission.owner, second.submission.owner),
                submitted_at=(first.submission.submitted_at, second.submission.submitted_at),
                signal=candidate.signal,
                jaccard=candidate.jaccard,
                deltas=(first.delta, second.delta),
            )
            review = self._ledger.lookup(identity)
            if review is None:
                try:
                    review = self._reviewer.review(task)
                except ReviewError:
                    failed_count += 1
                    continue
                if review.conclusive:
                    self._ledger.record(review)
            reviewed_count += 1
            self._report_store.write_plagiarism_review(
                identity.lab_id,
                identity.key,
                review.model_response_digest,
                {
                    "task_key": identity.key,
                    "kind": "plagiarism_review",
                    "review": review.to_dict(),
                    "submission_ids": list(identity.submission_ids),
                    "owners": list(task.owners),
                    "submitted_at": [value.isoformat() for value in task.submitted_at],
                    "similarity_signal": task.signal.value,
                    "jaccard": task.jaccard,
                    "human_review_status": "pending",
                },
            )
        self._report_reader.refresh()
        return {
            "schema_version": 1,
            "submission_id": target_id,
            "items": self._report_reader.get_submission_reports(target_id),
            "candidate_count": len(candidates.candidates),
            "reviewed_count": reviewed_count,
            "failed_candidate_count": failed_count,
            "corpus_size": len(submissions),
            "excluded_submission_count": len(candidates.exclusions),
        }

    def _identity(
        self,
        first: SimilarityDocument,
        second: SimilarityDocument,
        basis_commit: str,
        basis_tree_digest: str,
        document_digest: str,
        signal: str,
        jaccard: float,
        model: str,
        rules_version: str,
        prompt_version: str,
        result_schema_version: str,
    ) -> ReviewIdentity:
        task_parameters: dict[str, ModelParameter] = {
            "band_size": self._similarity_policy.band_size,
            "delta_version": "scoped-line-delta-v3",
            "jaccard": jaccard,
            "jaccard_threshold": self._similarity_policy.jaccard_threshold,
            "near_identical_threshold": 0.95,
            "num_permutations": self._similarity_policy.num_permutations,
            "prompt_evidence_chars": 240_000,
            "shingle_size": self._similarity_policy.shingle_size,
            "signal": signal,
            "similarity_version": "single-target-exhaustive-v1",
            "tokenizer_version": self._similarity_policy.tokenizer_version,
        }
        return ReviewIdentity(
            task_type=ReviewTaskType.PLAGIARISM,
            submission_ids=(first.submission.id, second.submission.id),
            input_digests=(first.submission.input_digest, second.submission.input_digest),
            source_delta_digests=(first.delta.digest, second.delta.digest),
            lab_id=first.submission.lab_id,
            basis_commit=basis_commit,
            basis_tree_digest=basis_tree_digest,
            document_digest=document_digest,
            lab_definition_digest=_json_digest(
                {
                    "lab_definition_digests": (
                        _json_digest(first.submission.lab_definition),
                        _json_digest(second.submission.lab_definition),
                    )
                }
            ),
            rules_version=rules_version,
            prompt_version=prompt_version,
            schema_version=result_schema_version,
            model=model,
            model_parameters=(),
            task_parameters=tuple(sorted(task_parameters.items())),
        )


def _json_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
