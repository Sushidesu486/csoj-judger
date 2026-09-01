import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oj_checker.agent_execution import LocalAgentRunExecutor
from oj_checker.agent_runs import AgentRunFailure
from oj_checker.plagiarism_execution import LocalPlagiarismRunExecutor
from oj_checker.review_basis import GitReviewBasisProvider
from oj_checker.review_bundle import VerifiedReviewBundle
from oj_checker.review_ledger import CompletedReview
from oj_checker.reviewer import PlagiarismReviewTask

SUBMISSION_ID = "00000000-0000-4000-8000-000000000001"


def test_local_executor_uses_complete_workspace_and_returns_normalized_report(
    tmp_path: Path,
) -> None:
    repository, revision = baseline_repository(tmp_path)
    review_basis = GitReviewBasisProvider(repository, revision).load("lab2")
    oj_root = tmp_path / "oj"
    source = b"int main() {\n  return compute();\n}\n"
    source_root = oj_root / "submissions" / SUBMISSION_ID / "input"
    source_root.mkdir(parents=True)
    (source_root / "main.cpp").write_bytes(source)
    work_root = tmp_path / "work"
    work_root.mkdir()
    client = FinishClient()
    executor = LocalAgentRunExecutor(
        oj_root=oj_root,
        hpc101_repository=repository,
        lab4_reference_repository=tmp_path / "unused-reference",
        lab4_reference_label="xiaoqu0000/NR-amssncku",
        work_root=work_root,
        client=client,
    )

    result = executor.execute(bundle(review_basis, source))

    assert result["decision"] == "compliant"
    assert result["compliant"] is True
    assert result["submission"]["id"] == SUBMISSION_ID
    assert result["evidence"]["workspace_complete"] is True
    assert result["evidence"]["workspace"]["submission_bytes"] == len(source)
    assert result["evidence"]["trace"]["tool_call_count"] == 1
    assert client.models == ["gpt-5.6-luna"]
    assert list(work_root.iterdir()) == []


def test_local_executor_rejects_signed_basis_that_is_not_present_locally(
    tmp_path: Path,
) -> None:
    repository, revision = baseline_repository(tmp_path)
    review_basis = GitReviewBasisProvider(repository, revision).load("lab2")
    oj_root = tmp_path / "oj"
    source = b"int main() { return compute(); }\n"
    source_root = oj_root / "submissions" / SUBMISSION_ID / "input"
    source_root.mkdir(parents=True)
    (source_root / "main.cpp").write_bytes(source)
    work_root = tmp_path / "work"
    work_root.mkdir()
    request = bundle(review_basis, source)
    raw_basis = request.payload["basis"]
    assert isinstance(raw_basis, dict)
    raw_basis["tree_digest"] = "0" * 64
    executor = LocalAgentRunExecutor(
        oj_root=oj_root,
        hpc101_repository=repository,
        lab4_reference_repository=tmp_path / "unused-reference",
        lab4_reference_label="xiaoqu0000/NR-amssncku",
        work_root=work_root,
        client=FinishClient(),
    )

    with pytest.raises(AgentRunFailure, match="BASELINE_MISSING"):
        executor.execute(request)

    assert list(work_root.iterdir()) == []


def test_plagiarism_executor_compares_only_target_and_persists_oriented_report(
    tmp_path: Path,
) -> None:
    repository, revision = baseline_repository(tmp_path)
    review_basis = GitReviewBasisProvider(repository, revision).load("lab2")
    target_id = SUBMISSION_ID
    counterpart_id = "00000000-0000-4000-8000-000000000002"
    source = b"int unusual_branch(int x) { return x == 17 ? x * 31 : x + 9; }\n"
    oj_root = tmp_path / "oj"
    for submission_id in (target_id, counterpart_id):
        source_root = oj_root / "submissions" / submission_id / "input"
        source_root.mkdir(parents=True)
        (source_root / "main.cpp").write_bytes(source)
    reviewer = IndependentPlagiarismReviewer()
    executor = LocalPlagiarismRunExecutor(
        oj_root=oj_root,
        report_root=tmp_path / "reports",
        hpc101_repository=repository,
        reviewer=reviewer,
    )

    result = executor.execute(plagiarism_bundle(review_basis, source))

    assert result["candidate_count"] == 1
    assert result["reviewed_count"] == 1
    assert result["failed_candidate_count"] == 0
    assert result["corpus_size"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["counterpart"]["submission_id"] == counterpart_id
    assert result["items"][0]["similarity"]["signal"] == "exact_delta"
    assert reviewer.calls == 1


def baseline_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "HPC101"
    (repository / "src/lab2").mkdir(parents=True)
    (repository / "docs/lab/Lab2-Vectorization").mkdir(parents=True)
    (repository / "src/lab2/main.cpp").write_text(
        "int main() { return required_compute(); }\n",
        encoding="utf-8",
    )
    (repository / "docs/lab/Lab2-Vectorization/index.md").write_text(
        "必须执行完整计算。\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, revision


def bundle(review_basis: Any, source: bytes) -> VerifiedReviewBundle:
    payload: dict[str, Any] = {
        "schema_version": "review-bundle-v1",
        "audience": "oj-checker",
        "submission": {
            "id": SUBMISSION_ID,
            "owner": "student",
            "lab_id": "lab2",
            "score": 100,
            "input_digest": "1" * 64,
            "submitted_at": "2026-08-30T01:02:03Z",
            "input_manifest": {
                "files": [
                    {
                        "path": "main.cpp",
                        "size": len(source),
                        "sha256": hashlib.sha256(source).hexdigest(),
                    }
                ]
            },
            "lab_definition": {"spec": {"submission": {"allow": ["*.cpp"]}}},
            "active_run": {
                "id": 42,
                "state": "Success",
                "result_info": {},
                "score": 100,
                "performance": 1.0,
                "finished_at": "2026-08-30T01:04:03Z",
            },
        },
        "basis": {
            "commit": review_basis.upstream_commit,
            "tree_digest": review_basis.tree_digest,
            "document_digest": review_basis.document_digest,
            "source_path": review_basis.source_path,
            "document_path": review_basis.document_path,
            "public_references": [],
        },
        "model": "gpt-5.6-luna",
        "source": "manual",
        "rules_version": "audit-rules-v2",
        "prompt_version": "agent-compliance-v1",
        "tool_version": "agent-tools-v1",
        "result_schema_version": "agent-review-result-v1",
        "issued_at": "2026-08-30T02:00:00Z",
        "expires_at": "2026-08-30T02:15:00Z",
        "nonce": "00000000-0000-4000-8000-000000000099",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return VerifiedReviewBundle(
        payload=payload,
        payload_bytes=raw,
        payload_digest=hashlib.sha256(raw).hexdigest(),
        key_id="plat101-review-2026-01",
    )


def plagiarism_bundle(review_basis: Any, source: bytes) -> VerifiedReviewBundle:
    submission_ids = (SUBMISSION_ID, "00000000-0000-4000-8000-000000000002")
    submissions = [
        {
            "id": submission_id,
            "owner": owner,
            "lab_id": "lab2",
            "score": 100,
            "input_digest": str(index + 1) * 64,
            "submitted_at": f"2026-08-30T01:0{index + 2}:03Z",
            "input_manifest": {
                "files": [
                    {
                        "path": "main.cpp",
                        "size": len(source),
                        "sha256": hashlib.sha256(source).hexdigest(),
                    }
                ]
            },
        }
        for index, (submission_id, owner) in enumerate(
            zip(submission_ids, ("student", "other-student"), strict=True)
        )
    ]
    payload: dict[str, Any] = {
        "schema_version": "plagiarism-review-bundle-v1",
        "audience": "oj-checker",
        "target_submission_id": SUBMISSION_ID,
        "submissions": submissions,
        "lab_definition": {"spec": {"submission": {"allow": ["*.cpp"]}}},
        "basis": {
            "commit": review_basis.upstream_commit,
            "tree_digest": review_basis.tree_digest,
            "document_digest": review_basis.document_digest,
            "source_path": review_basis.source_path,
            "document_path": review_basis.document_path,
            "public_references": [],
        },
        "model": "gpt-5.6-luna",
        "source": "manual",
        "rules_version": "audit-rules-v2",
        "prompt_version": "plagiarism-v2",
        "result_schema_version": "plagiarism-result-v1",
        "issued_at": "2026-08-30T02:00:00Z",
        "expires_at": "2026-08-30T02:15:00Z",
        "nonce": "00000000-0000-4000-8000-000000000099",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return VerifiedReviewBundle(
        payload=payload,
        payload_bytes=raw,
        payload_digest=hashlib.sha256(raw).hexdigest(),
        key_id="plat101-review-2026-01",
    )


class IndependentPlagiarismReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, task: Any) -> CompletedReview:
        assert isinstance(task, PlagiarismReviewTask)
        self.calls += 1
        return CompletedReview(
            identity=task.identity,
            completed_at=datetime(2026, 8, 30, 2, 6, tzinfo=UTC),
            verdict={
                "decision": "independent",
                "relationship": "exact",
                "confidence": 0.9,
                "summary": "结构相似; 人工复核后未发现直接复制证据。",
                "evidence": [],
                "requires_human_review": True,
            },
            model_response_digest="f" * 64,
            conclusive=True,
        )


class FinishClient:
    def __init__(self) -> None:
        self.models: list[str] = []

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, str | int | float | bool | None],
    ) -> Mapping[str, Any]:
        self.models.append(model)
        report = {
            "decision": "compliant",
            "confidence": 0.9,
            "summary": "未发现明确违规,但仍需人工复核。",
            "violations": [],
            "limitations": [],
            "requires_human_review": True,
        }
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "finish-1",
                                "type": "function",
                                "function": {
                                    "name": "finish_review",
                                    "arguments": json.dumps(report, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                }
            ]
        }
