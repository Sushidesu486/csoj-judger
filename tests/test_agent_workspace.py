import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oj_checker.agent_workspace import AgentWorkspacePreparer
from oj_checker.domain import Submission
from oj_checker.review_basis import BaselineFile, ReviewBasis
from oj_checker.submission_store import UnsafeSubmissionPath

SUBMISSION_ID = "00000000-0000-4000-8000-000000000001"


def submission(oj_root: Path, content: bytes = b"student source\n") -> Submission:
    input_root = oj_root / "submissions" / SUBMISSION_ID / "input" / "src"
    input_root.mkdir(parents=True)
    (input_root / "main.cpp").write_bytes(content)
    return Submission(
        id=SUBMISSION_ID,
        owner="student",
        lab_id="lab2",
        score=100,
        input_digest="input-digest",
        submitted_at=datetime(2026, 8, 29, tzinfo=UTC),
        input_manifest={
            "files": [
                {
                    "path": "src/main.cpp",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
        },
        lab_definition={"spec": {"submissions": {"home": {"allow": ["src/**"]}}}},
    )


def basis() -> ReviewBasis:
    content = b"baseline source\n"
    return ReviewBasis(
        lab_id="lab2",
        upstream_commit="a" * 40,
        source_path="src/lab2",
        document_path="docs/lab2.md",
        files=(
            BaselineFile(
                "src/main.cpp",
                content,
                hashlib.sha256(content).hexdigest(),
            ),
        ),
        tree_digest="b" * 64,
        document="实验文档",
        document_digest="c" * 64,
    )


def test_preparer_copies_complete_submission_and_frozen_context(tmp_path: Path) -> None:
    oj_root = tmp_path / "oj"
    item = submission(oj_root)
    target = tmp_path / "workspace"

    prepared = AgentWorkspacePreparer(oj_root).prepare(
        item,
        basis(),
        target,
        policy="必须执行完整计算。",
    )

    assert (target / "submission/src/main.cpp").read_bytes() == b"student source\n"
    assert (target / "baseline/src/main.cpp").read_bytes() == b"baseline source\n"
    assert "必须执行完整计算" in (target / "context/lab-policy.md").read_text()
    assert prepared.submission_file_count == 1
    assert prepared.submission_bytes == len(b"student source\n")
    assert len(prepared.workspace_digest) == 64


def test_preparer_binds_verified_request_to_local_basis(tmp_path: Path) -> None:
    oj_root = tmp_path / "oj"
    item = submission(oj_root)
    review_basis = basis()
    request = {
        "submission": {"id": item.id},
        "basis": {
            "commit": review_basis.upstream_commit,
            "tree_digest": review_basis.tree_digest,
            "document_digest": review_basis.document_digest,
            "source_path": review_basis.source_path,
            "document_path": review_basis.document_path,
            "public_references": [],
        },
        "model": "gpt-5.6-luna",
    }
    target = tmp_path / "verified-workspace"

    AgentWorkspacePreparer(oj_root).prepare(
        item,
        review_basis,
        target,
        policy="必须执行完整计算。",
        request_metadata=request,
        request_digest="d" * 64,
    )

    assert json.loads((target / "context/request.json").read_text()) == request
    assert json.loads((target / "context/workspace.json").read_text())["request_digest"] == (
        "d" * 64
    )


def test_preparer_rejects_verified_request_for_another_basis_before_writing(
    tmp_path: Path,
) -> None:
    oj_root = tmp_path / "oj"
    item = submission(oj_root)
    review_basis = basis()
    request = {
        "submission": {"id": item.id},
        "basis": {
            "commit": review_basis.upstream_commit,
            "tree_digest": "0" * 64,
            "document_digest": review_basis.document_digest,
            "source_path": review_basis.source_path,
            "document_path": review_basis.document_path,
            "public_references": [],
        },
    }
    target = tmp_path / "mismatched-workspace"

    with pytest.raises(ValueError, match="immutable baseline"):
        AgentWorkspacePreparer(oj_root).prepare(
            item,
            review_basis,
            target,
            policy="必须执行完整计算。",
            request_metadata=request,
        )

    assert not target.exists()


def test_preparer_fails_instead_of_truncating_or_accepting_digest_mismatch(
    tmp_path: Path,
) -> None:
    oj_root = tmp_path / "oj"
    item = submission(oj_root, b"complete large source" * 1000)
    manifest = dict(item.input_manifest)
    manifest["files"] = [dict(manifest["files"][0], sha256="0" * 64)]
    invalid = Submission(
        id=item.id,
        owner=item.owner,
        lab_id=item.lab_id,
        score=item.score,
        input_digest=item.input_digest,
        submitted_at=item.submitted_at,
        input_manifest=manifest,
        lab_definition=item.lab_definition,
    )

    with pytest.raises(UnsafeSubmissionPath, match="sha256 mismatch"):
        AgentWorkspacePreparer(oj_root).prepare(
            invalid,
            basis(),
            tmp_path / "workspace",
            policy="必须执行完整计算。",
        )
