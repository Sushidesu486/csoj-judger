from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oj_checker.agent_reviewer import (
    AgentReviewError,
    AgentReviewLimitError,
    ToolChatClient,
    TransientAgentReviewError,
    WorkspaceAgentReviewer,
)
from oj_checker.agent_runs import AgentRunFailure
from oj_checker.agent_tools import AgentWorkspace, ReadOnlyToolBroker
from oj_checker.agent_workspace import AgentWorkspacePreparer
from oj_checker.review_basis import GitReviewBasisProvider, UnsupportedLabError
from oj_checker.review_bundle import VerifiedReviewBundle, submission_from_review_bundle
from oj_checker.review_scope import (
    LAB4_REVIEW_POLICY,
    GitLab4ReferenceProvider,
    ReferenceSnapshot,
)
from oj_checker.submission_store import UnsafeSubmissionPath

_LAB4_IDS = frozenset({"lab4-cpu", "lab4-gpu"})
_LOGGER = logging.getLogger(__name__)


class LocalAgentRunExecutor:
    """Execute the temporary single-node rollout through typed read-only tools.

    The model receives neither a shell tool nor database/NFS credentials. The
    preparer alone reads the submission NFS into a per-run temporary workspace;
    all subsequent model observations are mediated by ``ReadOnlyToolBroker``.
    Kubernetes Job isolation can replace this executor without changing the
    signed request or persistent queue contract.
    """

    def __init__(
        self,
        *,
        oj_root: str | Path,
        hpc101_repository: str | Path,
        lab4_reference_repository: str | Path,
        lab4_reference_label: str,
        work_root: str | Path,
        client: ToolChatClient,
        max_turns: int = 32,
        max_attempts: int = 2,
    ) -> None:
        self._oj_root = Path(oj_root)
        self._hpc101_repository = Path(hpc101_repository)
        self._lab4_reference_repository = Path(lab4_reference_repository)
        self._lab4_reference_label = lab4_reference_label
        self._work_root = Path(work_root)
        if not self._work_root.is_dir() or self._work_root.is_symlink():
            raise ValueError("Agent work root must be an existing regular directory")
        self._reviewer = WorkspaceAgentReviewer(
            client,
            max_turns=max_turns,
            max_attempts=max_attempts,
        )

    def execute(self, bundle: VerifiedReviewBundle) -> Mapping[str, Any]:
        submission = submission_from_review_bundle(bundle)
        submission_metadata = bundle.payload["submission"]
        if not isinstance(submission_metadata, Mapping):
            raise AgentRunFailure("WORKSPACE_INVALID")
        raw_basis = bundle.payload["basis"]
        if not isinstance(raw_basis, Mapping):
            raise AgentRunFailure("BASELINE_MISSING")
        revision = raw_basis.get("commit")
        raw_references = raw_basis.get("public_references")
        if not isinstance(revision, str) or not isinstance(raw_references, list):
            raise AgentRunFailure("BASELINE_MISSING")
        try:
            basis = GitReviewBasisProvider(
                self._hpc101_repository,
                revision,
            ).load(submission.lab_id)
            references = self._load_references(submission.lab_id, raw_references)
        except (FileNotFoundError, RuntimeError, UnsupportedLabError, ValueError) as error:
            raise AgentRunFailure("BASELINE_MISSING") from error
        expected_basis = {
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
                for index, reference in enumerate(references)
            ],
        }
        if raw_basis != expected_basis:
            raise AgentRunFailure("BASELINE_MISSING")

        policy = basis.document
        if submission.lab_id in _LAB4_IDS:
            policy = f"{basis.document}\n\n{LAB4_REVIEW_POLICY}"
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"{bundle.payload_digest[:16]}-",
                dir=self._work_root,
            ) as temporary:
                workspace_root = Path(temporary) / "workspace"
                prepared = AgentWorkspacePreparer(self._oj_root).prepare(
                    submission,
                    basis,
                    workspace_root,
                    policy=policy,
                    public_references=references,
                    request_metadata=bundle.payload,
                    request_digest=bundle.payload_digest,
                )
                workspace = AgentWorkspace(
                    workspace_root / "submission",
                    workspace_root / "baseline",
                    workspace_root / "context",
                )
                result = self._reviewer.review(
                    model=str(bundle.payload["model"]),
                    broker=ReadOnlyToolBroker(workspace),
                    policy=policy,
                    submission=submission_metadata,
                )
        except UnsafeSubmissionPath as error:
            raise AgentRunFailure("WORKSPACE_INVALID") from error
        except TransientAgentReviewError as error:
            raise AgentRunFailure("MODEL_UNAVAILABLE") from error
        except AgentReviewLimitError as error:
            raise AgentRunFailure(error.code) from error
        except AgentReviewError as error:
            _LOGGER.warning(
                "Agent review protocol failure: %s; trace=%s",
                error,
                error.trace,
            )
            raise AgentRunFailure("RESULT_INVALID") from error
        except (OSError, ValueError) as error:
            raise AgentRunFailure("WORKSPACE_INVALID") from error

        return _normalized_report(
            bundle,
            dict(result.report),
            dict(result.trace),
            prepared.to_dict(),
        )

    def _load_references(
        self,
        lab_id: str,
        raw_references: list[object],
    ) -> tuple[ReferenceSnapshot, ...]:
        if not raw_references:
            return ()
        if lab_id not in _LAB4_IDS:
            raise ValueError("public references are only configured for Lab 4")
        revisions: list[str] = []
        for value in raw_references:
            if not isinstance(value, Mapping) or not isinstance(value.get("revision"), str):
                raise ValueError("public reference request is invalid")
            revisions.append(value["revision"])
        return GitLab4ReferenceProvider(
            self._lab4_reference_repository,
            revisions,
            label=self._lab4_reference_label,
        ).load()


def _normalized_report(
    bundle: VerifiedReviewBundle,
    report: Mapping[str, Any],
    trace: Mapping[str, Any],
    workspace: Mapping[str, Any],
) -> dict[str, Any]:
    submission = bundle.payload["submission"]
    basis = bundle.payload["basis"]
    if not isinstance(submission, Mapping) or not isinstance(basis, Mapping):
        raise AgentRunFailure("RESULT_INVALID")
    decision = report.get("decision")
    if decision not in {"compliant", "violation", "inconclusive"}:
        raise AgentRunFailure("RESULT_INVALID")
    compliant: bool | None
    if decision == "compliant":
        compliant = True
    elif decision == "violation":
        compliant = False
    else:
        compliant = None
    state = "inconclusive" if decision == "inconclusive" else "completed"
    return {
        "schema_version": 1,
        "submission": {
            "id": submission["id"],
            "owner": submission["owner"],
            "lab_id": submission["lab_id"],
            "score": submission["score"],
        },
        "state": state,
        "decision": decision,
        "compliant": compliant,
        "confidence": report.get("confidence"),
        "summary": report.get("summary"),
        "violations": report.get("violations", []),
        "evidence": {
            "review_strategy": "agent-tools-v1",
            "workspace_complete": True,
            "workspace": dict(workspace),
            "trace": dict(trace),
            "limitations": report.get("limitations", []),
            "requires_human_review": True,
            "bundle_digest": bundle.payload_digest,
            "bundle_key_id": bundle.key_id,
        },
        "provenance": {
            "review_key": bundle.payload_digest,
            "completed_at": datetime.now(UTC).isoformat(),
            "basis_commit": basis["commit"],
            "rules_version": bundle.payload["rules_version"],
            "prompt_version": bundle.payload["prompt_version"],
            "schema_version": bundle.payload["result_schema_version"],
            "model": bundle.payload["model"],
        },
    }
