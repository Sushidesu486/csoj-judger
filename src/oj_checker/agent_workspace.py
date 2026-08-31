from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oj_checker.domain import Submission
from oj_checker.review_basis import ReviewBasis
from oj_checker.review_scope import ReferenceSnapshot
from oj_checker.submission_store import (
    UnsafeSubmissionPath,
    _open_regular_file_beneath,
    _safe_path_parts,
    _validate_submission_id,
)

_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass(frozen=True, slots=True)
class PreparedAgentWorkspace:
    root: Path
    submission_file_count: int
    submission_bytes: int
    workspace_digest: str
    basis_tree_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "submission_file_count": self.submission_file_count,
            "submission_bytes": self.submission_bytes,
            "workspace_digest": self.workspace_digest,
            "basis_tree_digest": self.basis_tree_digest,
        }


class AgentWorkspacePreparer:
    def __init__(self, oj_root: str | Path) -> None:
        self._oj_root = Path(oj_root)

    def prepare(
        self,
        submission: Submission,
        basis: ReviewBasis,
        target: str | Path,
        *,
        policy: str,
        public_references: Sequence[ReferenceSnapshot] = (),
        request_metadata: Mapping[str, Any] | None = None,
        request_digest: str | None = None,
    ) -> PreparedAgentWorkspace:
        if submission.lab_id != basis.lab_id:
            raise ValueError("submission and review basis lab IDs differ")
        if not policy.strip():
            raise ValueError("agent workspace policy is required")
        if request_digest is not None and _SHA256.fullmatch(request_digest) is None:
            raise ValueError("request digest must be SHA-256")
        _validate_submission_id(submission.id)
        request = (
            _verified_request_metadata(request_metadata, submission, basis, public_references)
            if request_metadata is not None
            else _workspace_request(submission, basis, public_references)
        )
        root = Path(target)
        root.mkdir(parents=True, exist_ok=False)
        submission_root = root / "submission"
        baseline_root = root / "baseline"
        context_root = root / "context"
        for directory in (submission_root, baseline_root, context_root):
            directory.mkdir()

        copied = self._copy_submission(submission, submission_root)
        self._write_baseline(basis, baseline_root)
        self._write_public_references(public_references, baseline_root / "public")
        workspace_digest = _workspace_digest(copied)
        workspace_manifest: dict[str, Any] = {
            "schema_version": 1,
            "submission_id": submission.id,
            "files": copied,
            "workspace_digest": workspace_digest,
            "complete": True,
        }
        if request_digest is not None:
            workspace_manifest["request_digest"] = request_digest
        _write_json(context_root / "request.json", request)
        _write_json(context_root / "workspace.json", workspace_manifest)
        (context_root / "lab-policy.md").write_text(policy, encoding="utf-8")
        return PreparedAgentWorkspace(
            root=root,
            submission_file_count=len(copied),
            submission_bytes=sum(item["bytes"] for item in copied),
            workspace_digest=workspace_digest,
            basis_tree_digest=basis.tree_digest,
        )

    def _copy_submission(
        self,
        submission: Submission,
        target: Path,
    ) -> list[dict[str, Any]]:
        manifest_files = submission.input_manifest.get("files")
        if not isinstance(manifest_files, list):
            raise UnsafeSubmissionPath("input manifest files must be a list")
        copied: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_entry in sorted(manifest_files, key=_manifest_sort_key):
            if not isinstance(raw_entry, Mapping):
                raise UnsafeSubmissionPath("input manifest file entry must be an object")
            path = raw_entry.get("path")
            if not isinstance(path, str):
                raise UnsafeSubmissionPath("input manifest path must be a string")
            if path in seen:
                raise UnsafeSubmissionPath(f"duplicate input manifest path: {path!r}")
            seen.add(path)
            parts = _safe_path_parts(path)
            declared_size = raw_entry.get("size")
            if (
                isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or declared_size < 0
            ):
                raise UnsafeSubmissionPath(f"invalid declared file size for {path!r}")
            declared_digest = raw_entry.get("sha256")
            if declared_digest is not None and (
                not isinstance(declared_digest, str) or not _SHA256.fullmatch(declared_digest)
            ):
                raise UnsafeSubmissionPath(f"invalid declared sha256 for {path!r}")
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            file_fd = _open_regular_file_beneath(self._oj_root, submission.id, parts)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(file_fd, "rb") as source, destination.open("xb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            actual_digest = digest.hexdigest()
            if size != declared_size:
                raise UnsafeSubmissionPath(f"declared size mismatch for {path!r}")
            if declared_digest is not None and actual_digest != declared_digest.lower():
                raise UnsafeSubmissionPath(f"declared sha256 mismatch for {path!r}")
            copied.append({"path": path, "bytes": size, "sha256": actual_digest})
        return copied

    @staticmethod
    def _write_baseline(basis: ReviewBasis, target: Path) -> None:
        for file in basis.files:
            parts = _safe_path_parts(file.path)
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file.content)

    @staticmethod
    def _write_public_references(
        references: Sequence[ReferenceSnapshot],
        target: Path,
    ) -> None:
        for index, reference in enumerate(references):
            reference_root = target / str(index)
            for path, content in sorted(reference.files.items()):
                parts = _safe_path_parts(path)
                destination = reference_root.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")


def _manifest_sort_key(value: object) -> str:
    if isinstance(value, Mapping):
        path = value.get("path")
        if isinstance(path, str):
            return path
    return ""


def _workspace_request(
    submission: Submission,
    basis: ReviewBasis,
    public_references: Sequence[ReferenceSnapshot],
) -> dict[str, Any]:
    return {
        "submission": {
            "id": submission.id,
            "owner": submission.owner,
            "lab_id": submission.lab_id,
            "score": submission.score,
            "input_digest": submission.input_digest,
            "submitted_at": submission.submitted_at.isoformat(),
            "lab_definition": dict(submission.lab_definition),
            "active_run": {
                "id": submission.active_run_id,
                "state": submission.run_state,
                "result_info": dict(submission.run_result_info),
                "score": submission.run_score,
                "performance": submission.run_performance,
            },
        },
        "basis": _basis_metadata(basis, public_references),
    }


def _verified_request_metadata(
    request: Mapping[str, Any],
    submission: Submission,
    basis: ReviewBasis,
    public_references: Sequence[ReferenceSnapshot],
) -> dict[str, Any]:
    raw_submission = request.get("submission")
    raw_basis = request.get("basis")
    if not isinstance(raw_submission, Mapping) or raw_submission.get("id") != submission.id:
        raise ValueError("verified request submission does not match the workspace")
    expected_basis = _basis_metadata(basis, public_references)
    if raw_basis != expected_basis:
        raise ValueError("verified request basis does not match the local immutable baseline")
    return dict(request)


def _basis_metadata(
    basis: ReviewBasis,
    public_references: Sequence[ReferenceSnapshot],
) -> dict[str, Any]:
    return {
        "commit": basis.upstream_commit,
        "tree_digest": basis.tree_digest,
        "document_digest": basis.document_digest,
        "source_path": basis.source_path,
        "document_path": basis.document_path,
        "public_references": [
            {
                "repository": reference.repository,
                "revision": reference.revision,
                "tree_digest": reference.tree_digest,
                "path_prefix": f"public/{index}",
            }
            for index, reference in enumerate(public_references)
        ],
    }


def _workspace_digest(files: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        list(files),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
