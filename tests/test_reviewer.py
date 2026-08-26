from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from oj_checker.review_basis import ReviewBasis
from oj_checker.review_ledger import ModelParameter, ReviewIdentity
from oj_checker.reviewer import (
    ChatMessage,
    ComplianceReviewTask,
    ModelReply,
    OpenAICompatibleReviewer,
    PlagiarismReviewTask,
    ReviewParseError,
    TransientReviewError,
)
from oj_checker.similarity import BaselineDelta, BaselineDeltaFile, SimilaritySignal
from oj_checker.submission_store import SourceBundle


def test_compliance_review_retries_and_parses_reasoning_content() -> None:
    client = ScriptedChatClient(
        [
            TransientReviewError("upstream unavailable"),
            ModelReply(
                content=None,
                reasoning_content=(
                    "```json\n"
                    '{"decision":"violation","confidence":0.97,'
                    '"violations":[{"category":"constraint_violation",'
                    '"summary":"cached TwoPuncture output",'
                    '"evidence":[{"path":"src/TwoPunctures.C",'
                    '"description":"loads precomputed evolution data"}]}],'
                    '"summary":"The required stage is bypassed.",'
                    '"requires_human_review":true}\n'
                    "```"
                ),
            ),
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        max_attempts=2,
    )

    result = reviewer.review(compliance_task())

    assert result.conclusive
    assert result.verdict["decision"] == "violation"
    assert result.verdict["violations"][0]["category"] == "constraint_violation"
    assert client.call_count == 2
    assert "Do not skip TwoPuncture" in client.messages[-1][1].content
    assert "cached_result.bin" in client.messages[-1][1].content


def test_plagiarism_review_accepts_minor_edit_relationship() -> None:
    client = ScriptedChatClient(
        [
            ModelReply(
                content=(
                    '{"decision":"plagiarism","relationship":"minor_edit",'
                    '"confidence":0.99,"evidence":[{"first_path":"student/a.cpp",'
                    '"second_path":"student/a.cpp",'
                    '"description":"same uncommon control flow"}],'
                    '"summary":"Only constants differ.","requires_human_review":true}'
                ),
                reasoning_content=None,
            )
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
    )

    result = reviewer.review(plagiarism_task())

    assert result.conclusive
    assert result.verdict["decision"] == "plagiarism"
    assert result.verdict["relationship"] == "minor_edit"


def test_plagiarism_review_rejects_model_claim_of_exactness_for_a_near_pair() -> None:
    client = ScriptedChatClient(
        [
            ModelReply(
                content=(
                    '{"decision":"plagiarism","relationship":"exact",'
                    '"confidence":0.99,"evidence":[{"first_path":"student/a.cpp",'
                    '"second_path":"student/a.cpp","description":"similar code"}],'
                    '"summary":"The pair looks copied.","requires_human_review":true}'
                ),
                reasoning_content=None,
            )
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        max_attempts=1,
    )

    with pytest.raises(ReviewParseError, match="program-computed relationship"):
        reviewer.review(plagiarism_task())


def test_compliance_review_cannot_pass_an_incomplete_source_bundle() -> None:
    task = compliance_task()
    task = replace(task, delta=replace(task.delta, incomplete=True))
    client = ScriptedChatClient(
        [
            ModelReply(
                content=(
                    '{"decision":"compliant","confidence":0.9,"violations":[],'
                    '"summary":"No violation found.","requires_human_review":false}'
                ),
                reasoning_content=None,
            )
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        max_attempts=1,
    )

    with pytest.raises(ReviewParseError, match="incomplete"):
        reviewer.review(task)


def test_compliance_review_cannot_pass_when_prompt_evidence_is_truncated() -> None:
    task = compliance_task()
    task = replace(
        task,
        identity=replace(
            task.identity,
            task_parameters=(("prompt_evidence_chars", 80),),
        ),
        delta=replace(
            task.delta,
            files=(
                BaselineDeltaFile(
                    path="src/TwoPunctures.C",
                    added_text="x" * 1_000,
                    removed_text="Solve();\n",
                ),
            ),
        ),
    )
    client = ScriptedChatClient(
        [
            ModelReply(
                content=(
                    '{"decision":"compliant","confidence":0.9,"violations":[],'
                    '"summary":"No violation found.","requires_human_review":false}'
                ),
                reasoning_content=None,
            )
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        max_attempts=1,
    )

    with pytest.raises(ReviewParseError, match="prompt evidence"):
        reviewer.review(task)


def test_compliance_review_keeps_the_complete_current_lab2_document() -> None:
    task = compliance_task()
    large_document = "D" * 39_388
    task = replace(
        task,
        identity=replace(task.identity, document_digest="large-document-digest"),
        basis=replace(
            task.basis,
            document=large_document,
            document_digest="large-document-digest",
        ),
    )
    client = ScriptedChatClient(
        [
            ModelReply(
                content=(
                    '{"decision":"compliant","confidence":0.9,"violations":[],'
                    '"summary":"No violation found.","requires_human_review":false}'
                ),
                reasoning_content=None,
            )
        ]
    )
    reviewer = OpenAICompatibleReviewer(
        client,
        clock=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        max_attempts=1,
    )

    result = reviewer.review(task)

    assert result.conclusive
    assert large_document in client.messages[0][1].content


class ScriptedChatClient:
    def __init__(self, responses: list[Exception | ModelReply]) -> None:
        self._responses = responses
        self.call_count = 0
        self.messages: list[tuple[ChatMessage, ...]] = []

    def complete(
        self,
        *,
        model: str,
        messages: tuple[ChatMessage, ...],
        parameters: Mapping[str, ModelParameter],
    ) -> ModelReply:
        self.messages.append(messages)
        response = self._responses[self.call_count]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response


def compliance_task() -> ComplianceReviewTask:
    return ComplianceReviewTask(
        identity=identity("compliance", "compliance-v1", "compliance-result-v1"),
        owner="alice",
        score=120,
        lab_definition={"spec": {"submissions": {"home": {"allow": ["src/**"]}}}},
        basis=review_basis(),
        source_bundle=SourceBundle("submission-a", (), 0),
        delta=BaselineDelta(
            submission_id="submission-a",
            files=(
                BaselineDeltaFile(
                    path="src/TwoPunctures.C",
                    added_text='load("cached_result.bin");\n',
                    removed_text="Solve();\n",
                ),
            ),
            incomplete=False,
            digest="delta-a",
        ),
    )


def plagiarism_task() -> PlagiarismReviewTask:
    first_delta = BaselineDelta(
        "submission-a",
        (BaselineDeltaFile("student/a.cpp", "return x * 17;\n", "return x;\n"),),
        False,
        "delta-a",
    )
    second_delta = BaselineDelta(
        "submission-b",
        (BaselineDeltaFile("student/a.cpp", "return x * 19;\n", "return x;\n"),),
        False,
        "delta-b",
    )
    return PlagiarismReviewTask(
        identity=ReviewIdentity(
            task_type="plagiarism",
            submission_ids=("submission-a", "submission-b"),
            input_digests=("input-a", "input-b"),
            source_delta_digests=("delta-a", "delta-b"),
            lab_id="lab4-cpu",
            basis_commit="upstream-commit",
            basis_tree_digest="tree-digest",
            document_digest="document-digest",
            lab_definition_digest="lab-definition-digest",
            rules_version="audit-rules-v1",
            prompt_version="plagiarism-v1",
            schema_version="plagiarism-result-v1",
            model="gpt-5.6-luna",
            model_parameters=(("temperature", 0),),
            task_parameters=(
                ("jaccard_threshold", 0.7),
                ("near_identical_threshold", 0.95),
                ("prompt_evidence_chars", 240_000),
            ),
        ),
        owners=("alice", "bob"),
        submitted_at=(
            datetime(2026, 8, 24, 8, 0, tzinfo=UTC),
            datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        ),
        signal=SimilaritySignal.MINHASH,
        jaccard=0.94,
        deltas=(first_delta, second_delta),
    )


def identity(task_type: str, prompt_version: str, schema_version: str) -> ReviewIdentity:
    return ReviewIdentity(
        task_type=task_type,
        submission_ids=("submission-a",),
        input_digests=("input-a",),
        source_delta_digests=("delta-a",),
        lab_id="lab4-cpu",
        basis_commit="upstream-commit",
        basis_tree_digest="tree-digest",
        document_digest="document-digest",
        lab_definition_digest="lab-definition-digest",
        rules_version="audit-rules-v1",
        prompt_version=prompt_version,
        schema_version=schema_version,
        model="gpt-5.6-luna",
        model_parameters=(("temperature", 0),),
        task_parameters=(("prompt_evidence_chars", 240_000),),
    )


def review_basis() -> ReviewBasis:
    return ReviewBasis(
        lab_id="lab4-cpu",
        upstream_commit="upstream-commit",
        source_path="src/lab4",
        document_path="docs/lab/Lab4-AMSS-NCKU/index.md",
        files=(),
        tree_digest="tree-digest",
        document="# Lab4\nDo not skip TwoPuncture.\n",
        document_digest="document-digest",
    )
