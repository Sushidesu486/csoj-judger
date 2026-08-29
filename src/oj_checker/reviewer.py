import dataclasses
import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
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
from oj_checker.similarity import (
    BaselineDelta,
    BaselineDeltaFile,
    BaselineDeltaHunk,
    SimilaritySignal,
)
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
    review_policy: str | None = None
    scope_diagnostics: Mapping[str, Any] = dataclasses.field(default_factory=dict)

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
        if (
            isinstance(task, ComplianceReviewTask)
            and _review_strategy(task.identity) == "chunked-v1"
        ):
            return self._review_compliance_chunked(task)
        messages, prompt_evidence_incomplete, evidence = _messages(
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
                raw_response, verdict = _parse_reply_verdict(
                    reply,
                    task,
                    prompt_evidence_incomplete=prompt_evidence_incomplete,
                )
                return CompletedReview(
                    identity=task.identity,
                    completed_at=self._clock(),
                    verdict=verdict,
                    model_response_digest=hashlib.sha256(raw_response.encode()).hexdigest(),
                    conclusive=verdict["decision"] != "inconclusive",
                    evidence=evidence,
                )
            except TransientReviewError as current_error:
                error = current_error
        if error is None:
            raise ReviewError("review failed without an error")
        raise error

    def _review_compliance_chunked(self, task: ComplianceReviewTask) -> CompletedReview:
        chunks = _compliance_chunks(task, _review_chunk_chars(task.identity))
        verdicts: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        failures: list[dict[str, Any]] = []
        request_chars: list[int] = []
        for index, messages in enumerate(chunks):
            request_chars.append(sum(len(message.content) for message in messages))
            try:
                reply = self._complete_with_retries(task.identity, messages)
                raw_response, verdict = _parse_reply_verdict(
                    reply,
                    task,
                    prompt_evidence_incomplete=task.delta.incomplete,
                )
                raw_responses.append(raw_response)
                verdicts.append(verdict)
            except ReviewError as error:
                failures.append(
                    {
                        "chunk_index": index,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )

        verdict = _merge_compliance_verdicts(verdicts, len(chunks), len(failures))
        evidence = _chunked_evidence_diagnostics(
            task,
            chunks,
            request_chars,
            len(verdicts),
            failures,
        )
        if failures:
            evidence["failed_chunks"] = failures
        digest_input = "\n\n".join(raw_responses)
        if failures:
            digest_input += "\n" + json.dumps(failures, sort_keys=True)
        return CompletedReview(
            identity=task.identity,
            completed_at=self._clock(),
            verdict=verdict,
            model_response_digest=hashlib.sha256(digest_input.encode()).hexdigest(),
            conclusive=verdict["decision"] != "inconclusive",
            evidence=evidence,
        )

    def _complete_with_retries(
        self,
        identity: ReviewIdentity,
        messages: tuple[ChatMessage, ...],
    ) -> ModelReply:
        error: TransientReviewError | None = None
        for _ in range(self._max_attempts):
            try:
                return self._client.complete(
                    model=identity.model,
                    messages=messages,
                    parameters=dict(identity.model_parameters),
                )
            except TransientReviewError as current_error:
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
            if error.code in {408, 429} or error.code >= 500:
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
) -> tuple[tuple[ChatMessage, ...], bool, dict[str, Any]]:
    if isinstance(task, ComplianceReviewTask):
        return _compliance_messages(task, max_evidence_chars)
    return _plagiarism_messages(task, max_evidence_chars)


def _review_strategy(identity: ReviewIdentity) -> str:
    value = dict(identity.task_parameters).get("review_strategy")
    return value if isinstance(value, str) else "bounded-v2"


def _review_chunk_chars(identity: ReviewIdentity) -> int:
    value = dict(identity.task_parameters).get("review_chunk_chars", 180_000)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("review identity requires a positive review_chunk_chars parameter")
    return value


def _compliance_chunks(
    task: ComplianceReviewTask,
    chunk_chars: int,
) -> tuple[tuple[ChatMessage, ...], ...]:
    items = _compliance_evidence_items(task)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        for part in _split_evidence_item(item, max(16_000, chunk_chars // 2)):
            candidate = [*current, part]
            if current and _chunk_prompt_chars(task, candidate, 1) > chunk_chars:
                chunks.append(current)
                current = [part]
            else:
                current = candidate
    if current or not chunks:
        chunks.append(current)

    count = len(chunks)
    return tuple(
        _build_compliance_chunk_messages(task, evidence, index, count)
        for index, evidence in enumerate(chunks)
    )


def _compliance_evidence_items(task: ComplianceReviewTask) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    macro_definitions = _unchanged_macro_definitions(task)
    for file in task.delta.files:
        for index, hunk in enumerate(_hunks_for_file(file)):
            items.append(
                {
                    "kind": "delta_hunk",
                    "path": file.path,
                    "artifact": _artifact_kind(file.path),
                    "baseline_kind": file.baseline_kind,
                    "baseline_revision": file.baseline_revision,
                    "hunk_index": index,
                    "old_start": hunk.old_start,
                    "old_count": hunk.old_count,
                    "new_start": hunk.new_start,
                    "new_count": hunk.new_count,
                    "text": hunk.lines,
                    "referenced_unchanged_definitions": _referenced_macro_definitions(
                        hunk.lines,
                        macro_definitions,
                        exclude_path=file.path,
                    ),
                }
            )
    execution_names = {"CMakeLists.txt", "Makefile", "makefile", "compile.sh", "run.sh"}
    for source_file in task.source_bundle.files:
        if source_file.path.rsplit("/", 1)[-1] not in execution_names:
            continue
        items.append(
            {
                "kind": "execution_context",
                "path": source_file.path,
                "content": source_file.content,
                "input_truncated": source_file.truncated,
                "declared_bytes": source_file.declared_bytes,
                "bytes_read": source_file.bytes_read,
            }
        )
    return items


def _unchanged_macro_definitions(
    task: ComplianceReviewTask,
) -> Mapping[str, tuple[Mapping[str, str], ...]]:
    changed_paths = {file.path for file in task.delta.files}
    definitions: dict[str, list[Mapping[str, str]]] = {}
    pattern = re.compile(
        r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)"
        r"(?:[ \t]+([^/\r\n]+?))?[ \t]*(?://.*)?$"
    )
    for source_file in task.source_bundle.files:
        if source_file.truncated or source_file.path in changed_paths:
            continue
        normalized = source_file.content.replace("\r\n", "\n").replace("\r", "\n")
        for match in pattern.finditer(normalized):
            symbol = match.group(1)
            definitions.setdefault(symbol, []).append(
                {
                    "path": source_file.path,
                    "symbol": symbol,
                    "definition": match.group(0).strip(),
                }
            )
    return {symbol: tuple(values) for symbol, values in definitions.items()}


def _referenced_macro_definitions(
    text: str,
    definitions: Mapping[str, tuple[Mapping[str, str], ...]],
    *,
    exclude_path: str,
) -> list[Mapping[str, str]]:
    identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", text))
    return [
        definition
        for symbol in sorted(identifiers & definitions.keys())
        for definition in definitions[symbol]
        if definition["path"] != exclude_path
    ]


def _split_evidence_item(item: dict[str, Any], max_text_chars: int) -> tuple[dict[str, Any], ...]:
    text_key = "text" if "text" in item else "content"
    value = item.get(text_key)
    if not isinstance(value, str) or len(value) <= max_text_chars:
        return (item,)
    context_chars = min(6_000, max_text_chars // 8)
    core_chars = max(1, max_text_chars - 2 * context_chars)
    parts: list[dict[str, Any]] = []
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(value):
        proposed_end = min(len(value), start + core_chars)
        end = proposed_end
        if proposed_end < len(value):
            newline = value.rfind("\n", start, proposed_end)
            if newline >= start:
                end = newline + 1
        if end <= start:
            end = proposed_end
        ranges.append((start, end))
        start = end
    total = len(ranges)
    for index, (start, end) in enumerate(ranges):
        prefix_start = max(0, start - context_chars)
        if prefix_start > 0:
            newline = value.find("\n", prefix_start, start)
            if newline >= 0:
                prefix_start = newline + 1
        suffix_end = min(len(value), end + context_chars)
        if suffix_end < len(value):
            newline = value.rfind("\n", end, suffix_end)
            if newline >= end:
                suffix_end = newline + 1
        prefix = value[prefix_start:start]
        core = value[start:end]
        suffix = value[end:suffix_end]
        part = dict(item)
        part[text_key] = prefix + core + suffix
        part["part_index"] = index
        part["part_count"] = total
        part["core_text_start"] = len(prefix)
        part["core_text_chars"] = len(core)
        part["context_prefix_chars"] = len(prefix)
        part["context_suffix_chars"] = len(suffix)
        parts.append(part)
    return tuple(parts)


def _chunk_prompt_chars(
    task: ComplianceReviewTask,
    evidence: list[dict[str, Any]],
    index: int,
) -> int:
    return sum(len(message.content) for message in _build_compliance_chunk_messages(
        task,
        evidence,
        index,
        max(index + 1, 1),
    ))


def _build_compliance_chunk_messages(
    task: ComplianceReviewTask,
    evidence: list[dict[str, Any]],
    index: int,
    count: int,
) -> tuple[ChatMessage, ...]:
    lab_definition_json = (
        json.dumps(task.lab_definition, ensure_ascii=False, sort_keys=True)
        if task.review_policy is None
        else "trusted judge constraints are summarized in review_policy"
    )
    payload = {
        "task": "compliance_review_chunk",
        "chunk": {"index": index, "count": count},
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
            "experiment_document": task.review_policy or task.basis.document,
            "frozen_lab_definition_json": lab_definition_json,
        },
        "review_scope": _scope_prompt_summary(task.scope_diagnostics),
        "delta_inventory": {
            "digest": task.delta.digest,
            "file_count": len(task.delta.files),
            "hunk_count": sum(len(_hunks_for_file(file)) for file in task.delta.files),
            "incomplete": task.delta.incomplete,
        },
        "evidence": evidence,
        "source_inventory": {
            "file_count": len(task.source_bundle.files),
            "declared_paths": list(task.source_bundle.declared_paths),
            "required_patterns": list(task.source_bundle.required_patterns),
            "allowed_patterns": list(task.source_bundle.allowed_patterns),
        },
    }
    schema = {
        "decision": "compliant | violation | inconclusive",
        "confidence": "number from 0 to 1",
        "violations": [
            {
                "category": (
                    "hardcoded_or_checker_abuse | required_computation_reduction | "
                    "fixed_problem_constraint_change | fabricated_or_missing_output"
                ),
                "summary": "short finding",
                "evidence": [{"path": "relative path", "description": "specific evidence"}],
            }
        ],
        "summary": "short overall explanation for this evidence chunk",
        "requires_human_review": True,
    }
    return (
        ChatMessage(
            "system",
            "You audit one complete evidence chunk of an HPC lab submission. Use only supplied "
            "evidence. Return one JSON object and never issue discipline. Report a violation "
            "only when this chunk contains concrete evidence; report compliant when this chunk "
            "contains no concrete violation. Do not report a violation merely because a refactor "
            "is large, equivalence is not proven, a generic race is possible, performance may "
            "regress, or a tuning parameter is ignored. Only report concrete reduction of the "
            "required physics workload, fixed-problem changes, fabricated output, precomputed "
            "answers, or checker abuse. Do not report inconclusive merely because "
            "this is one chunk; use inconclusive only when supplied evidence is explicitly "
            "cut short, incomplete, or internally unparsable. Split evidence items include "
            "overlapping prefix/suffix context and identify the uniquely covered core range. "
            "Treat referenced_unchanged_definitions as authoritative submitted dependency "
            "context when interpreting loop bounds and compile-time branches. "
            "Write human-readable text in Simplified Chinese. Keep paths and enum values exact. "
            "The full review is split across chunks, so do not claim that unseen chunks were "
            "reviewed.",
        ),
        ChatMessage(
            "user",
            "Review chunk "
            + str(index + 1)
            + " of "
            + str(count)
            + ". Required JSON schema:\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\nEvidence:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )


def _merge_compliance_verdicts(
    verdicts: list[dict[str, Any]],
    chunk_count: int,
    failed_count: int,
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for verdict in verdicts:
        for violation in verdict.get("violations", []):
            key = json.dumps(violation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key not in seen:
                seen.add(key)
                violations.append(violation)
    decisions = [verdict.get("decision") for verdict in verdicts]
    if failed_count or len(verdicts) != chunk_count or "inconclusive" in decisions:
        decision = "inconclusive"
        summary = f"已完成 {len(verdicts)}/{chunk_count} 个证据分片, 仍有证据无法确认。"
    elif "violation" in decisions:
        decision = "violation"
        summary = f"已完成全部 {chunk_count} 个证据分片, 发现 {len(violations)} 条违规线索。"
    else:
        decision = "compliant"
        summary = f"已完成全部 {chunk_count} 个证据分片, 未发现违规优化或绕过评测的证据。"
    confidence_values = [
        value
        for value in (verdict.get("confidence") for verdict in verdicts)
        if isinstance(value, int | float) and not isinstance(value, bool)
    ]
    return {
        "decision": decision,
        "confidence": min(confidence_values, default=0.0),
        "violations": violations,
        "summary": summary,
        "requires_human_review": True,
    }


def _chunked_evidence_diagnostics(
    task: ComplianceReviewTask,
    chunks: tuple[tuple[ChatMessage, ...], ...],
    request_chars: list[int],
    completed_count: int,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    execution_names = {"CMakeLists.txt", "Makefile", "makefile", "compile.sh", "run.sh"}
    source_context_count = sum(
        file.path.rsplit("/", 1)[-1] in execution_names for file in task.source_bundle.files
    )
    return {
        "kind": "compliance",
        "review_strategy": "chunked-v1",
        "prompt_evidence_chars": None,
        "prompt_chars": sum(request_chars),
        "request_chars": request_chars,
        "incomplete": bool(failures) or task.delta.incomplete,
        "chunk_count": len(chunks),
        "completed_chunk_count": completed_count,
        "failed_chunk_count": len(failures),
        "model_context_mode": "adaptive_chunks",
        "source_bundle": {
            "file_count": len(task.source_bundle.files),
            "bytes_read": task.source_bundle.total_bytes_read,
            "truncated_file_count": sum(file.truncated for file in task.source_bundle.files),
        },
        "baseline_delta": {
            "file_count": len(task.delta.files),
            "hunk_count": sum(len(_hunks_for_file(file)) for file in task.delta.files),
            "covered_file_count": len(task.delta.files) if not failures else 0,
            "covered_hunk_count": (
                sum(len(_hunks_for_file(file)) for file in task.delta.files)
                if not failures
                else 0
            ),
            "truncated_file_count": 0,
            "truncated_hunk_count": 0,
        },
        "source_context": {
            "file_count": source_context_count,
            "truncated_file_count": sum(
                file.truncated
                for file in task.source_bundle.files
                if file.path.rsplit("/", 1)[-1] in execution_names
            ),
            "omitted_unchanged_file_count": max(
                0,
                len(task.source_bundle.files) - source_context_count,
            ),
        },
        "review_scope": dict(task.scope_diagnostics),
    }


def _prompt_evidence_chars(identity: ReviewIdentity) -> int:
    value = dict(identity.task_parameters).get("prompt_evidence_chars")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("review identity requires a positive prompt_evidence_chars parameter")
    return value


def _compliance_messages(
    task: ComplianceReviewTask,
    max_evidence_chars: int,
) -> tuple[tuple[ChatMessage, ...], bool, dict[str, Any]]:
    lab_definition_json = (
        json.dumps(task.lab_definition, ensure_ascii=False, sort_keys=True)
        if task.review_policy is None
        else "trusted judge constraints are summarized in review_policy"
    )
    experiment_document = task.review_policy or task.basis.document
    document_budget = max(
        1,
        min(len(experiment_document), min(80_000, max_evidence_chars // 3)),
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
            "experiment_document": _bounded_text(
                experiment_document,
                document_budget,
                reason="document_budget",
            ),
            "frozen_lab_definition_json": _bounded_text(
                lab_definition_json,
                lab_definition_budget,
                reason="lab_definition_budget",
            ),
        },
        "review_scope": _scope_prompt_summary(task.scope_diagnostics),
        "baseline_delta": _delta_payload(task.delta, delta_budget, include_hunks=True),
        "source_context": _source_payload(
            task.source_bundle,
            set(),
            source_context_budget,
        ),
    }
    schema = {
        "decision": "compliant | violation | inconclusive",
        "confidence": "number from 0 to 1",
        "violations": [
            {
                "category": (
                    "hardcoded_or_checker_abuse | required_computation_reduction | "
                    "fixed_problem_constraint_change | fabricated_or_missing_output"
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
            "baseline delta or prompt evidence is marked incomplete. Write every human-readable "
            "summary and evidence description in Simplified Chinese. Keep JSON keys, enum values, "
            "identifiers, and file paths exactly as specified. The baseline_delta hunks use "
            "unified-diff lines: a leading '-' is baseline-only, '+' is submission-only, and "
            "' ' is unchanged context. source_context intentionally contains only execution "
            "files when changed hunks already cover the other files. If any required hunk text "
            "is truncated, mark the decision inconclusive. Files marked as artifacts (for example "
            ".orig or .bak) are not automatically part of the build, but their presence and path "
            "must still be considered.",
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
    incomplete = _contains_truncation(critical_evidence)
    return messages, incomplete, _compliance_evidence_diagnostics(
        task,
        payload,
        messages,
        incomplete,
    )


def _plagiarism_messages(
    task: PlagiarismReviewTask,
    max_evidence_chars: int,
) -> tuple[tuple[ChatMessage, ...], bool, dict[str, Any]]:
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
    incomplete = _contains_truncation(payload)
    return messages, incomplete, {
        "kind": "plagiarism",
        "prompt_evidence_chars": max_evidence_chars,
        "prompt_chars": sum(len(message.content) for message in messages),
        "incomplete": incomplete,
    }


def _delta_payload(
    delta: BaselineDelta,
    max_chars: int,
    *,
    include_hunks: bool = False,
) -> dict[str, Any]:
    budgets = _allocate_budgets(
        [
            (
                file.path,
                max(
                    sum(len(hunk.lines) for hunk in _hunks_for_file(file)),
                    1,
                ),
                _evidence_priority(file.path),
            )
            for file in delta.files
        ],
        max_chars,
    )
    files: list[dict[str, Any]] = []
    for file in delta.files:
        file_budget = budgets.get(file.path, 1)
        if include_hunks:
            hunks = _hunks_for_file(file)
            hunk_budgets = _allocate_budgets(
                [
                    (str(index), max(len(hunk.lines), 1), 1)
                    for index, hunk in enumerate(hunks)
                ],
                file_budget,
            )
            files.append(
                {
                    "path": file.path,
                    "artifact": _artifact_kind(file.path),
                    "baseline_kind": file.baseline_kind,
                    "baseline_revision": file.baseline_revision,
                    "added_chars": len(file.added_text),
                    "removed_chars": len(file.removed_text),
                    "hunks": [
                        {
                            "old_start": hunk.old_start,
                            "old_count": hunk.old_count,
                            "new_start": hunk.new_start,
                            "new_count": hunk.new_count,
                            "text": _bounded_text(
                                hunk.lines,
                                hunk_budgets.get(str(index), 1),
                                reason="delta_hunk_budget",
                            ),
                        }
                        for index, hunk in enumerate(hunks)
                    ],
                }
            )
            continue

        if file.added_text and file.removed_text:
            added_budget = max(1, file_budget // 2)
            removed_budget = max(1, file_budget - added_budget)
        elif file.added_text:
            added_budget = file_budget
            removed_budget = 1
        else:
            added_budget = 1
            removed_budget = file_budget
        added = _bounded_text(file.added_text, added_budget, reason="delta_budget")
        removed = _bounded_text(file.removed_text, removed_budget, reason="delta_budget")
        files.append(
            {
                "path": file.path,
                "artifact": _artifact_kind(file.path),
                "baseline_kind": file.baseline_kind,
                "baseline_revision": file.baseline_revision,
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
    budgets = _allocate_budgets(
        [
            (file.path, max(len(file.content), 1), _evidence_priority(file.path))
            for file in selected
        ],
        max_chars,
    )
    files: list[dict[str, Any]] = []
    for file in selected:
        content = _bounded_text(
            file.content,
            budgets.get(file.path, 1),
            reason="source_context_budget",
        )
        files.append(
            {
                "path": file.path,
                "artifact": _artifact_kind(file.path),
                "content": content,
                "input_truncated": file.truncated,
                "declared_bytes": file.declared_bytes,
                "bytes_read": file.bytes_read,
                "omission_reason": file.omission_reason,
            }
        )
    return {
        "files": files,
        "omitted_file_count": 0,
        "omitted_unchanged_file_count": max(0, len(bundle.files) - len(selected)),
        "selection_mode": "execution_files_only_when_delta_is_present",
        "declared_paths": list(bundle.declared_paths),
        "required_patterns": list(bundle.required_patterns),
        "allowed_patterns": list(bundle.allowed_patterns),
    }


def _compliance_evidence_diagnostics(
    task: ComplianceReviewTask,
    payload: Mapping[str, Any],
    messages: tuple[ChatMessage, ...],
    incomplete: bool,
) -> dict[str, Any]:
    delta_files = payload["baseline_delta"]["files"]
    source_files = payload["source_context"]["files"]
    return {
        "kind": "compliance",
        "prompt_evidence_chars": dict(task.identity.task_parameters)["prompt_evidence_chars"],
        "prompt_chars": sum(len(message.content) for message in messages),
        "incomplete": incomplete,
        "source_bundle": {
            "file_count": len(task.source_bundle.files),
            "bytes_read": task.source_bundle.total_bytes_read,
            "truncated_file_count": sum(file.truncated for file in task.source_bundle.files),
        },
        "baseline_delta": {
            "file_count": len(task.delta.files),
            "hunk_count": sum(len(_hunks_for_file(file)) for file in task.delta.files),
            "truncated_file_count": sum(
                any(hunk["text"].get("truncated") is True for hunk in file["hunks"])
                for file in delta_files
            ),
        },
        "source_context": {
            "file_count": len(source_files),
            "truncated_file_count": sum(
                file["content"].get("truncated") is True for file in source_files
            ),
            "omitted_unchanged_file_count": payload["source_context"].get(
                "omitted_unchanged_file_count", 0
            ),
        },
        "review_scope": dict(task.scope_diagnostics),
    }


def _scope_prompt_summary(diagnostics: Mapping[str, Any]) -> Mapping[str, Any]:
    if not diagnostics:
        return {}
    summary: dict[str, Any] = {
        key: diagnostics.get(key)
        for key in (
            "strategy",
            "track",
            "complete",
            "fallback_reason",
            "execution_targets",
            "input_parameters",
            "input_file_error",
            "critical_configuration",
        )
        if key in diagnostics
    }
    for key in (
        "original",
        "execution_scope",
        "excluded",
        "unchanged_against_selected_baseline",
        "review_delta",
    ):
        value = diagnostics.get(key)
        if isinstance(value, Mapping):
            summary[key] = {
                item_key: item_value
                for item_key, item_value in value.items()
                if item_key not in {"files", "reasons"}
            }
    reference = diagnostics.get("reference_selection")
    if isinstance(reference, Mapping):
        summary["reference_selection"] = {
            key: reference.get(key)
            for key in ("selected_repository", "selected_revision", "course_match")
            if key in reference
        }
    return summary


def _bounded_text(value: str, max_chars: int, *, reason: str) -> dict[str, Any]:
    max_chars = max(1, max_chars)
    truncated = len(value) > max_chars
    return {
        "text": value[:max_chars],
        "truncated": truncated,
        "original_chars": len(value),
        "included_chars": min(len(value), max_chars),
        "truncation_reason": reason if truncated else None,
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _hunks_for_file(file: BaselineDeltaFile) -> tuple[BaselineDeltaHunk, ...]:
    return file.hunks or _fallback_hunks(file)


def _fallback_hunks(file: BaselineDeltaFile) -> tuple[BaselineDeltaHunk, ...]:
    lines: list[str] = []
    lines.extend(f"-{line}" for line in file.removed_text.splitlines(keepends=True))
    lines.extend(f"+{line}" for line in file.added_text.splitlines(keepends=True))
    return (
        BaselineDeltaHunk(
            old_start=1,
            old_count=len(file.removed_text.splitlines()),
            new_start=1,
            new_count=len(file.added_text.splitlines()),
            lines="".join(lines),
        ),
    )


def _evidence_priority(path: str) -> int:
    name = path.rsplit("/", 1)[-1]
    if name in {"CMakeLists.txt", "Makefile", "makefile", "compile.sh", "run.sh"}:
        return 4
    if _artifact_kind(path) is not None:
        return 1
    return 2


def _artifact_kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    for suffix in (".orig", ".bak", ".rej", ".swp"):
        if name.endswith(suffix):
            return suffix.removeprefix(".")
    return None


def _allocate_budgets(
    items: Sequence[tuple[str, int, int]],
    max_chars: int,
) -> dict[str, int]:
    if not items:
        return {}
    max_chars = max(1, max_chars)
    weights = {
        key: max(size, 1) * max(priority, 1)
        for key, size, priority in items
    }
    total_weight = sum(weights.values())
    budgets = {
        key: min(size, max(1, max_chars * weights[key] // total_weight))
        for key, size, _ in items
    }
    remaining = max_chars - sum(budgets.values())
    ordered = sorted(items, key=lambda item: (-item[2], -item[1], item[0]))
    while remaining > 0:
        progressed = False
        for key, size, _ in ordered:
            if budgets[key] >= size:
                continue
            budgets[key] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    return budgets


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
    decoder = json.JSONDecoder()
    found_object = False
    first_validation_error: ReviewParseError | None = None
    for start, character in enumerate(raw_response):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw_response[start:])
        except json.JSONDecodeError:
            continue
        found_object = True
        if not isinstance(value, dict):
            continue
        try:
            if isinstance(task, ComplianceReviewTask):
                _validate_compliance_verdict(
                    value,
                    task,
                    prompt_evidence_incomplete=prompt_evidence_incomplete,
                )
            else:
                _validate_plagiarism_verdict(value, task)
        except ReviewParseError as error:
            if first_validation_error is None:
                first_validation_error = error
            continue
        return value
    if first_validation_error is not None:
        raise first_validation_error
    if found_object:
        raise ReviewParseError("model verdict must be a JSON object")
    raise ReviewParseError("model response does not contain valid JSON")


def _parse_reply_verdict(
    reply: ModelReply,
    task: ReviewTask,
    *,
    prompt_evidence_incomplete: bool,
) -> tuple[str, dict[str, Any]]:
    candidates = tuple(
        value
        for value in (reply.content, reply.reasoning_content)
        if isinstance(value, str) and value
    )
    if not candidates:
        raise ReviewParseError("model returned no content or reasoning_content")
    last_error: ReviewParseError | None = None
    for raw_response in candidates:
        try:
            return raw_response, _parse_verdict(
                raw_response,
                task,
                prompt_evidence_incomplete=prompt_evidence_incomplete,
            )
        except ReviewParseError as error:
            last_error = error
    assert last_error is not None
    raise last_error


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
        "required_computation_reduction",
        "fixed_problem_constraint_change",
        "fabricated_or_missing_output",
        # Accepted for reading older result schemas.
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
