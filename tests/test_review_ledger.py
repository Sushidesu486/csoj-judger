from dataclasses import replace
from datetime import UTC, datetime

import pytest

from oj_checker.review_ledger import CompletedReview, FileReviewLedger, ReviewIdentity


def test_ledger_reuses_only_the_exact_completed_review_identity(tmp_path) -> None:
    identity = review_identity()
    review = CompletedReview(
        identity=identity,
        completed_at=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        verdict={"decision": "compliant", "confidence": 0.91},
        model_response_digest="response-digest",
        conclusive=True,
    )
    ledger = FileReviewLedger(tmp_path)

    path = ledger.record(review)

    assert path.is_file()
    assert ledger.lookup(identity) == review
    assert ledger.record(review) == path
    assert ledger.lookup(replace(identity, prompt_version="compliance-v2")) is None
    assert ledger.lookup(replace(identity, model="gpt-5.6-terra")) is None
    assert ledger.lookup(replace(identity, basis_commit="new-upstream-commit")) is None


def test_ledger_does_not_cache_an_inconclusive_review(tmp_path) -> None:
    review = CompletedReview(
        identity=review_identity(),
        completed_at=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        verdict={"decision": "inconclusive"},
        model_response_digest="response-digest",
        conclusive=False,
    )
    ledger = FileReviewLedger(tmp_path)

    with pytest.raises(ValueError, match="conclusive"):
        ledger.record(review)

    assert ledger.lookup(review.identity) is None


def review_identity() -> ReviewIdentity:
    return ReviewIdentity(
        task_type="compliance",
        submission_ids=("submission-a",),
        input_digests=("input-a",),
        source_delta_digests=("delta-a",),
        lab_id="lab4-cpu",
        basis_commit="upstream-commit",
        basis_tree_digest="tree-digest",
        document_digest="document-digest",
        lab_definition_digest="lab-definition-digest",
        rules_version="audit-rules-v1",
        prompt_version="compliance-v1",
        schema_version="compliance-result-v1",
        model="gpt-5.6-luna",
        model_parameters=(("temperature", 0),),
        task_parameters=(),
    )
