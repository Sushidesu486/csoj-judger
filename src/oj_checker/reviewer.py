import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from oj_checker.review_basis import ReviewBasis
from oj_checker.review_ledger import (
    CompletedReview,
    ModelParameter,
    ReviewIdentity,
    ReviewTaskType,
)
from oj_checker.similarity import BaselineDelta, SimilaritySignal
from oj_checker.submission_store import SourceBundle


class ReviewError(RuntimeError):
    """An LLM review could not be completed."""


class TransientReviewError(ReviewError):
    """The upstream may succeed if the task is retried."""


class ReviewParseError(ReviewError):
    """The upstream response does not satisfy the task schema."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str | None
    reasoning_content: str | None


class ChatClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: tuple[ChatMessage, ...],
        parameters: Mapping[str, ModelParameter],
    ) -> ModelReply: ...


@dataclass(frozen=True, slots=True)
class ComplianceReviewTask:
    identity: ReviewIdentity
    owner: str
    score: int
    lab_definition: Mapping[str, Any]
    basis: ReviewBasis
    source_bundle: SourceBundle
    delta: BaselineDelta

    def __post_init__(self) -> None:
        if self.identity.task_type is not ReviewTaskType.COMPLIANCE:
            raise ValueError("compliance task requires a compliance review identity")
        if self.identity.submission_ids != (self.delta.submission_id,):
            raise ValueError("compliance task submission does not match its review identity")
        _validate_basis_identity(self.identity, self.basis)


@dataclass(frozen=True, slots=True)
class PlagiarismReviewTask:
    identity: ReviewIdentity
    owners: tuple[str, str]
    submitted_at: tuple[datetime, datetime]
    signal: SimilaritySignal
    jaccard: float
    deltas: tuple[BaselineDelta, BaselineDelta]

    def __post_init__(self) -> None:
        if self.identity.task_type is not ReviewTaskType.PLAGIARISM:
            raise ValueError("plagiarism task requires a plagiarism review identity")
        if self.identity.submission_ids != tuple(delta.submission_id for delta in self.deltas):
            raise ValueError("plagiarism task submissions do not match its review identity")
        if not 0 <= self.jaccard <= 1:
            raise ValueError("plagiarism Jaccard score must be between zero and one")


type ReviewTask = ComplianceReviewTask | PlagiarismReviewTask


class Reviewer(Protocol):
    def review(self, task: ReviewTask) -> CompletedReview: ...


class OpenAICompatibleReviewer:
    def __init__(
        self,
        client: ChatClient,
        *,
        clock: Callable[[], datetime],
        max_attempts: int = 2,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._client = client
        self._clock = clock
        self._max_attempts = max_attempts

    def review(self, task: ReviewTask) -> CompletedReview:
        messages, prompt_evidence_incomplete = _messages(
            task,
            _prompt_evidence_chars(task.identity),
        )
        error: ReviewError | None = None
        for _ in range(self._max_attempts):
            try:
                reply = self._client.complete(
                    model=task.identity.model,
                    messages=messages,
                    parameters=dict(task.identity.model_parameters),
                )
                raw_response = reply.content or reply.reasoning_content
                if not raw_response:
                    raise ReviewParseError("model returned no content or reasoning_content")
                verdict = _parse_verdict(
                    raw_response,
                    task,
                    prompt_evidence_incomplete=prompt_evidence_incomplete,
                )
                return CompletedReview(
                    identity=task.identity,
                    completed_at=self._clock(),
                    verdict=verdict,
                    model_response_digest=hashlib.sha256(raw_response.encode()).hexdigest(),
                    conclusive=verdict["decision"] != "inconclusive",
                )
            except (TransientReviewError, ReviewParseError) as current_error:
                error = current_error
        if error is None:
            raise ReviewError("review failed without an error")
        raise error


class OpenAIStreamingChatClient:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 180) -> None:
        if not token:
            raise ValueError("LLM token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._token = token
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        *,
        model: str,
        messages: tuple[ChatMessage, ...],
        parameters: Mapping[str, ModelParameter],
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": True,
            **parameters,
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return _read_stream(response)
        except urllib.error.HTTPError as error:
            if error.code in {408, 409, 425, 429} or error.code >= 500:
                raise TransientReviewError(f"LLM upstream returned HTTP {error.code}") from None
            raise ReviewError(f"LLM request returned HTTP {error.code}") from None
        except (TimeoutError, urllib.error.URLError) as error:
            raise TransientReviewError(f"LLM upstream connection failed: {error}") from None


def _read_stream(response: Any) -> ModelReply:
    content: list[str] = []
    reasoning: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError as error:
            raise ReviewParseError("LLM stream contained invalid JSON") from error
        if not isinstance(event, dict):
            continue
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        message = choice.get("delta") or choice.get("message")
        if not isinstance(message, dict):
            continue
        _append_text(content, message.get("content"))
        _append_text(reasoning, message.get("reasoning_content"))
    return ModelReply("".join(content) or None, "".join(reasoning) or None)


def _append_text(output: list[str], value: object) -> None:
    if isinstance(value, str):
        output.append(value)


def _messages(
    task: ReviewTask,
    max_evidence_chars: int,
) -> tuple[tuple[ChatMessage, ...], bool]:
    if isinstance(task, ComplianceReviewTask):
        return _compliance_messages(task, max_evidence_chars)
    return _plagiarism_messages(task, max_evidence_chars)


def _prompt_evidence_chars(identity: ReviewIdentity) -> int:
    value = dict(identity.task_parameters).get("prompt_evidence_chars")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("review identity requires a positive prompt_evidence_chars parameter")
    return value


def _compliance_messages(
    task: ComplianceReviewTask,
    max_evidence_chars: int,
) -> tuple[tuple[ChatMessage, ...], bool]:
    lab_definition_json = json.dumps(
        task.lab_definition,
        ensure_ascii=False,
        sort_keys=True,
    )
    document_budget = max(
        1,
        min(len(task.basis.document), min(80_000, max_evidence_chars // 3)),
    )
    lab_definition_budget = max(
        1,
        min(len(lab_definition_json), min(30_000, max_evidence_chars // 8)),
    )
    remaining = max(2, max_evidence_chars - document_budget - lab_definition_budget)
    delta_budget = max(1, remaining * 2 // 3)
    source_context_budget = max(1, remaining - delta_budget)
    payload = {
        "task": "compliance_review",
        "prompt_evidence_budget_chars": max_evidence_chars,
        "submission": {
            "id": task.identity.submission_ids[0],
            "owner": task.owner,
            "lab_id": task.identity.lab_id,
            "score": task.score,
        },
        "review_basis": {
            "upstream_commit": task.basis.upstream_commit,
            "source_path": task.basis.source_path,
            "document_path": task.basis.document_path,
            "experiment_document": _bounded_text(task.basis.document, document_budget),
            "frozen_lab_definition_json": _bounded_text(
                lab_definition_json,
                lab_definition_budget,
            ),
        },
        "baseline_delta": _delta_payload(task.delta, delta_budget),
        "source_context": _source_payload(
            task.source_bundle,
            {file.path for file in task.delta.files},
            source_context_budget,
        ),
    }
    schema = {
        "decision": "compliant | violation | inconclusive",
        "confidence": "number from 0 to 1",
        "violations": [
            {
                "category": (
                    "hardcoded_or_checker_abuse | baseline_degradation | "
                    "constraint_violation | out_of_scope_modification"
                ),
                "summary": "short finding",
                "evidence": [{"path": "relative path", "description": "specific evidence"}],
            }
        ],
        "summary": "short overall explanation",
        "requires_human_review": True,
    }
    messages = (
        ChatMessage(
            "system",
            "You audit HPC lab submissions. Use only supplied evidence. Return one JSON object, "
            "never a disciplinary action. Mark inconclusive when evidence is insufficient or the "
            "baseline delta or prompt evidence is marked incomplete.",
        ),
        ChatMessage(
            "user",
            "Check whether the highest valid submission follows the experiment document, stays "
            "within allowed modification boundaries, preserves required baseline computation, and "
            "does not hardcode or bypass evaluation stages. Required JSON schema:\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True)
            + "\nEvidence:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    critical_evidence = {
        "review_basis": payload["review_basis"],
        "baseline_delta": payload["baseline_delta"],
    }
    return messages, _contains_truncation(critical_evidence)


def _plagiarism_messages(
    task: PlagiarismReviewTask,
    max_evidence_chars: int,
) -> tuple[tuple[ChatMessage, ...], bool]:
    delta_budget = max(1, max_evidence_chars // 2)
    payload = {
        "task": "plagiarism_adjudication",
        "prompt_evidence_budget_chars": max_evidence_chars,
        "lab_id": task.identity.lab_id,
        "program_computed_similarity": {
            "signal": task.signal.value,
            "exact_shingle_jaccard": task.jaccard,
            "relationship": _program_relationship(task),
        },
        "submissions": [
            {
                "id": submission_id,
                "owner": owner,
                "submitted_at": submitted_at.isoformat(),
                "baseline_delta": _delta_payload(delta, delta_budget),
            }
            for submission_id, owner, submitted_at, delta in zip(
                task.identity.submission_ids,
                task.owners,
                task.submitted_at,
                task.deltas,
                strict=True,
            )
        ],
    }
    schema = {
        "decision": "plagiarism | independent | inconclusive",
        "relationship": "must echo program_computed_similarity.relationship exactly",
        "confidence": "number from 0 to 1",
        "evidence": [
            {
                "first_path": "relative path",
                "second_path": "relative path",
                "description": "specific uncommon similarity",
            }
        ],
        "summary": "short overall explanation",
        "requires_human_review": True,
    }
    messages = (
        ChatMessage(
            "system",
            "You adjudicate code similarity using baseline-excluded deltas. Return one JSON "
            "object. "
            "Treat the program-computed exactness as authoritative and never issue discipline.",
        ),
        ChatMessage(
            "user",
            "Decide whether uncommon implementation details indicate copying, shared public "
            "template, "
            "or independent work. Required JSON schema:\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True)
            + "\nEvidence:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return messages, _contains_truncation(payload)


def _delta_payload(delta: BaselineDelta, max_chars: int) -> dict[str, Any]:
    files = []
    per_file_budget = max(1, max_chars // max(1, len(delta.files)))
    for file in delta.files:
        if file.added_text and file.removed_text:
            added_budget = max(1, per_file_budget // 2)
            removed_budget = max(1, per_file_budget - added_budget)
        elif file.added_text:
            added_budget = per_file_budget
            removed_budget = 1
        else:
            added_budget = 1
            removed_budget = per_file_budget
        added = _bounded_text(file.added_text, added_budget)
        removed = _bounded_text(file.removed_text, removed_budget)
        files.append(
            {
                "path": file.path,
                "added_text": added,
                "removed_text": removed,
            }
        )
    return {
        "digest": delta.digest,
        "incomplete": delta.incomplete,
        "files": files,
        "omitted_file_count": 0,
    }


def _source_payload(
    bundle: SourceBundle,
    changed_paths: set[str],
    max_chars: int,
) -> dict[str, Any]:
    execution_names = {"CMakeLists.txt", "Makefile", "makefile", "compile.sh", "run.sh"}
    selected = [
        file
        for file in bundle.files
        if file.path in changed_paths or file.path.rsplit("/", 1)[-1] in execution_names
    ]
    files = []
    per_file_budget = max(1, max_chars // max(1, len(selected)))
    for file in selected:
        content = _bounded_text(file.content, per_file_budget)
        files.append(
            {
                "path": file.path,
                "content": content,
                "input_truncated": file.truncated,
            }
        )
    return {
        "files": files,
        "omitted_file_count": 0,
        "declared_paths": list(bundle.declared_paths),
        "required_patterns": list(bundle.required_patterns),
        "allowed_patterns": list(bundle.allowed_patterns),
    }


def _bounded_text(value: str, max_chars: int) -> dict[str, Any]:
    truncated = len(value) > max_chars
    return {
        "text": value[:max_chars],
        "truncated": truncated,
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _contains_truncation(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("truncated") is True or value.get("input_truncated") is True:
            return True
        omitted = value.get("omitted_file_count")
        if isinstance(omitted, int) and omitted > 0:
            return True
        return any(_contains_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncation(item) for item in value)
    return False


def _parse_verdict(
    raw_response: str,
    task: ReviewTask,
    *,
    prompt_evidence_incomplete: bool,
) -> dict[str, Any]:
    start = raw_response.find("{")
    end = raw_response.rfind("}")
    if start < 0 or end < start:
        raise ReviewParseError("model response does not contain a JSON object")
    try:
        value = json.loads(raw_response[start : end + 1])
    except json.JSONDecodeError as error:
        raise ReviewParseError("model response contains invalid JSON") from error
    if not isinstance(value, dict):
        raise ReviewParseError("model verdict must be a JSON object")
    if isinstance(task, ComplianceReviewTask):
        _validate_compliance_verdict(
            value,
            task,
            prompt_evidence_incomplete=prompt_evidence_incomplete,
        )
    else:
        _validate_plagiarism_verdict(value, task)
    return value


def _validate_compliance_verdict(
    verdict: dict[str, Any],
    task: ComplianceReviewTask,
    *,
    prompt_evidence_incomplete: bool,
) -> None:
    decision = _enum_field(verdict, "decision", {"compliant", "violation", "inconclusive"})
    _common_verdict_fields(verdict)
    violations = verdict.get("violations")
    if not isinstance(violations, list):
        raise ReviewParseError("compliance violations must be a list")
    allowed_categories = {
        "hardcoded_or_checker_abuse",
        "baseline_degradation",
        "constraint_violation",
        "out_of_scope_modification",
    }
    for violation in violations:
        if not isinstance(violation, dict):
            raise ReviewParseError("each compliance violation must be an object")
        _enum_field(violation, "category", allowed_categories)
        _text_field(violation, "summary")
        _evidence(violation, ("path", "description"))
    if decision == "violation" and not violations:
        raise ReviewParseError("a violation verdict requires evidence")
    if task.delta.incomplete and decision != "inconclusive":
        raise ReviewParseError("an incomplete source bundle requires an inconclusive verdict")
    if prompt_evidence_incomplete and decision != "inconclusive":
        raise ReviewParseError("incomplete prompt evidence requires an inconclusive verdict")


def _validate_plagiarism_verdict(
    verdict: dict[str, Any],
    task: PlagiarismReviewTask,
) -> None:
    decision = _enum_field(
        verdict,
        "decision",
        {"plagiarism", "independent", "inconclusive"},
    )
    relationship = _enum_field(
        verdict,
        "relationship",
        {"exact", "near_identical", "minor_edit", "shared_template", "independent", "unclear"},
    )
    if relationship != _program_relationship(task):
        raise ReviewParseError("relationship does not match the program-computed relationship")
    _common_verdict_fields(verdict)
    _evidence(verdict, ("first_path", "second_path", "description"))
    if decision == "plagiarism" and not verdict["evidence"]:
        raise ReviewParseError("a plagiarism verdict requires evidence")


def _common_verdict_fields(verdict: dict[str, Any]) -> None:
    confidence = verdict.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise ReviewParseError("confidence must be a number")
    if not 0 <= confidence <= 1:
        raise ReviewParseError("confidence must be between zero and one")
    _text_field(verdict, "summary")
    if not isinstance(verdict.get("requires_human_review"), bool):
        raise ReviewParseError("requires_human_review must be a boolean")


def _evidence(container: dict[str, Any], fields: tuple[str, ...]) -> None:
    evidence = container.get("evidence")
    if not isinstance(evidence, list):
        raise ReviewParseError("evidence must be a list")
    for item in evidence:
        if not isinstance(item, dict):
            raise ReviewParseError("each evidence item must be an object")
        for field in fields:
            _text_field(item, field)


def _enum_field(container: dict[str, Any], name: str, allowed: set[str]) -> str:
    value = container.get(name)
    if not isinstance(value, str) or value not in allowed:
        raise ReviewParseError(f"{name} is missing or unsupported")
    return value


def _text_field(container: dict[str, Any], name: str) -> str:
    value = container.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ReviewParseError(f"{name} must be a non-empty string")
    return value


def _validate_basis_identity(identity: ReviewIdentity, basis: ReviewBasis) -> None:
    if (
        identity.lab_id != basis.lab_id
        or identity.basis_commit != basis.upstream_commit
        or identity.basis_tree_digest != basis.tree_digest
        or identity.document_digest != basis.document_digest
    ):
        raise ValueError("review basis does not match the review identity")


def _program_relationship(task: PlagiarismReviewTask) -> str:
    if task.signal in {
        SimilaritySignal.EXACT_SUBMISSION,
        SimilaritySignal.EXACT_DELTA,
    }:
        return "exact"
    threshold = dict(task.identity.task_parameters).get("near_identical_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise ValueError("plagiarism identity requires near_identical_threshold")
    if not 0 <= threshold <= 1:
        raise ValueError("near_identical_threshold must be between zero and one")
    if task.jaccard >= threshold:
        return "near_identical"
    return "minor_edit"
