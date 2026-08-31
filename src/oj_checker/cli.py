import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never, cast

from oj_checker.agent_execution import LocalAgentRunExecutor
from oj_checker.agent_reviewer import OpenAICompatibleToolChatClient, WorkspaceAgentReviewer
from oj_checker.agent_runs import FileAgentRunService
from oj_checker.agent_tools import AgentWorkspace, ReadOnlyToolBroker
from oj_checker.agent_workspace import AgentWorkspacePreparer
from oj_checker.catalog import best_per_owner_lab
from oj_checker.domain import AuditRequest, SelectionPolicy, SnapshotRequest
from oj_checker.nightly import REVIEW_LABS, HTTPComplianceClient, NightlyReviewRunner
from oj_checker.postgres_catalog import PostgresSubmissionCatalog
from oj_checker.report_api import (
    ComplianceApi,
    FileComplianceReportReader,
    ReviewLaunchResult,
    RunnerReviewLauncher,
    serve_report_api,
)
from oj_checker.report_store import FileReportStore
from oj_checker.review_basis import GitReviewBasisProvider
from oj_checker.review_bundle import submission_from_review_bundle, verify_review_bundle
from oj_checker.review_ledger import FileReviewLedger
from oj_checker.review_scope import (
    LAB4_REVIEW_POLICY,
    ComplianceReviewScopeBuilder,
    GitLab4ReferenceProvider,
    ReferenceSnapshot,
)
from oj_checker.reviewer import OpenAICompatibleReviewer, OpenAIStreamingChatClient
from oj_checker.runner import AuditRunner, ReviewPipeline
from oj_checker.similarity import BaselineDeltaBuilder, SimilarityDetector, SimilarityPolicy
from oj_checker.submission_store import NfsSubmissionStore, SourcePolicy


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    oj_root: Path
    report_root: Path
    git_commit: str
    hpc101_repository: Path
    hpc101_revision: str
    lab4_reference_repository: Path
    lab4_reference_revisions: tuple[str, ...]
    llm_base_url: str
    llm_token: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        database_url = os.environ.get("DB_URL") or os.environ.get("DBURL")
        if not database_url:
            raise RuntimeError("DB_URL is required")
        return cls(
            database_url=database_url,
            oj_root=Path(os.environ.get("OJ_ROOT", "/data/.oj")),
            report_root=Path(os.environ.get("REPORT_ROOT", "audit-reports")),
            git_commit=os.environ.get("OJ_CHECKER_GIT_COMMIT", "unknown"),
            hpc101_repository=Path(
                os.environ.get("HPC101_REPOSITORY", "/baseline/HPC101")
            ),
            hpc101_revision=os.environ.get("HPC101_REVISION", ""),
            lab4_reference_repository=Path(
                os.environ.get("LAB4_REFERENCE_REPOSITORY", "/baseline/NR-amssncku")
            ),
            lab4_reference_revisions=tuple(
                revision.strip()
                for revision in os.environ.get("LAB4_REFERENCE_REVISIONS", "").split(",")
                if revision.strip()
            ),
            llm_base_url=os.environ.get(
                "LLM_BASE_URL",
                "http://new-api.new-api.svc.cluster.local:3000/v1",
            ),
            llm_token=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "agent-report-api":
            _agent_report_api(args)
            return 0
        if args.command == "describe-review-bundle-config":
            result = _describe_review_bundle_config(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command in {"agent-review-workspace", "prepare-agent-workspace-bundle"}:
            result = (
                _agent_review_workspace(args)
                if args.command == "agent-review-workspace"
                else _prepare_agent_workspace_bundle(args)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        settings = Settings.from_environment()
        if args.command == "report-api":
            _report_api(settings, args)
            return 0
        exit_status = 0
        if args.command == "doctor":
            result = _doctor(settings, lab=args.lab)
        elif args.command == "plan":
            result = _plan(settings, args)
        elif args.command == "smoke":
            doctor = _doctor(settings, lab=args.lab)
            plan = _plan(settings, args)
            result = {"doctor": doctor, "plan": plan, "llm_checked": False}
        elif args.command == "nightly":
            result = _nightly(settings, args)
            if result["failed_count"]:
                exit_status = 1
        elif args.command == "prepare-agent-workspace":
            result = _prepare_agent_workspace(settings, args)
        else:
            result = _audit(settings, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_status
    except Exception as error:
        payload: dict[str, Any] = {
            "status": "error",
            "error_type": type(error).__name__,
            "message": str(error),
        }
        trace = getattr(error, "trace", None)
        if isinstance(trace, Mapping) and trace:
            payload["trace"] = dict(trace)
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


def entrypoint() -> Never:
    raise SystemExit(main())


def _doctor(settings: Settings, *, lab: str | None) -> dict[str, Any]:
    catalog = PostgresSubmissionCatalog(settings.database_url)
    cutoff = datetime.now(UTC)
    labs = (lab,) if lab else ()
    all_qualifying = catalog.snapshot(
        SnapshotRequest(SelectionPolicy.ALL_QUALIFYING, cutoff=cutoff, labs=labs)
    )
    best_submissions = best_per_owner_lab(all_qualifying.submissions)

    nfs_root_readable = settings.oj_root.is_dir() and os.access(settings.oj_root, os.R_OK)
    sample_input_readable = False
    source_file_count = 0
    source_bytes_read = 0
    truncated_source_file_count = 0
    if best_submissions:
        sample_input = settings.oj_root / "submissions" / best_submissions[0].id / "input"
        sample_input_readable = sample_input.is_dir() and os.access(sample_input, os.R_OK)
        bundle = NfsSubmissionStore(settings.oj_root).load_bundle(
            best_submissions[0],
            SourcePolicy(max_file_bytes=64_000, max_total_bytes=256_000),
        )
        source_file_count = len(bundle.files)
        source_bytes_read = bundle.total_bytes_read
        truncated_source_file_count = sum(file.truncated for file in bundle.files)

    if not nfs_root_readable or not sample_input_readable or source_file_count == 0:
        raise RuntimeError("submission NFS is not readable")

    return {
        "status": "ok",
        "database_transaction_read_only": True,
        "cutoff": cutoff.isoformat(),
        "lab_filter": lab,
        "single_review_corpus_size": len(best_submissions),
        "plagiarism_corpus_size": len(all_qualifying.submissions),
        "nfs_root_readable": nfs_root_readable,
        "sample_input_readable": sample_input_readable,
        "source_file_count": source_file_count,
        "source_bytes_read": source_bytes_read,
        "truncated_source_file_count": truncated_source_file_count,
        "llm_checked": False,
    }


def _plan(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    cutoff = _parse_cutoff(args.cutoff)
    run_id = args.run_id or f"manual-{cutoff.strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(args.output_root) if args.output_root else settings.report_root
    runner = AuditRunner(
        PostgresSubmissionCatalog(settings.database_url),
        FileReportStore(output_root),
        clock=lambda: datetime.now(UTC),
        git_commit=settings.git_commit,
    )
    summary = runner.run(
        AuditRequest(
            run_id=run_id,
            cutoff=cutoff,
            min_score=args.min_score,
            labs=(args.lab,) if args.lab else (),
            owners=(args.owner,) if args.owner else (),
            limit=args.limit,
        )
    )
    return {
        "status": "ok",
        "run_id": summary.manifest.run_id,
        "manifest_path": str(summary.manifest_path),
        "single_review_corpus_size": summary.manifest.single_review_corpus_size,
        "plagiarism_corpus_size": summary.manifest.plagiarism_corpus_size,
        "task_counts": {
            kind.value: count for kind, count in summary.manifest.task_counts.items()
        },
    }


def _audit(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    if not settings.llm_token:
        raise RuntimeError("LLM_API_KEY is required for an audit run")
    if not settings.hpc101_revision:
        raise RuntimeError("HPC101_REVISION must pin the authoritative upstream commit")
    cutoff = _parse_cutoff(args.cutoff)
    run_id = args.run_id or f"audit-{args.lab}-{cutoff.strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(args.output_root) if args.output_root else settings.report_root
    runner, basis_provider = _build_review_runner(
        settings,
        output_root=output_root,
        similarity_threshold=args.similarity_threshold,
        shingle_size=args.shingle_size,
        num_permutations=args.num_permutations,
        band_size=args.band_size,
        similarity_workers=args.similarity_workers,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
        llm_timeout=args.llm_timeout,
        max_attempts=args.max_attempts,
        max_evidence_chars=args.max_evidence_chars,
    )
    summary = runner.run(
        AuditRequest(
            run_id=run_id,
            cutoff=cutoff,
            min_score=args.min_score,
            labs=(args.lab,),
            rules_version=args.rules_version,
            prompt_version="compliance-v5+plagiarism-v1",
            model=args.model,
            similarity_threshold=args.similarity_threshold,
            execute_reviews=True,
        )
    )
    return {
        "status": "ok",
        "run_id": summary.manifest.run_id,
        "manifest_path": str(summary.manifest_path),
        "authoritative_hpc101_commit": basis_provider.upstream_commit,
        "single_review_corpus_size": summary.manifest.single_review_corpus_size,
        "plagiarism_corpus_size": summary.manifest.plagiarism_corpus_size,
        "task_counts": {
            kind.value: count for kind, count in summary.manifest.task_counts.items()
        },
        "cache_hit_count": summary.cache_hit_count,
        "llm_call_count": summary.llm_call_count,
        "completed_review_count": summary.completed_review_count,
        "inconclusive_review_count": summary.inconclusive_review_count,
        "failed_review_count": summary.failed_review_count,
        "similarity_exclusion_count": summary.similarity_exclusion_count,
    }


def _build_review_runner(
    settings: Settings,
    *,
    output_root: Path,
    similarity_threshold: float,
    shingle_size: int,
    num_permutations: int,
    band_size: int,
    similarity_workers: int,
    max_file_bytes: int,
    max_total_bytes: int,
    llm_timeout: float,
    max_attempts: int,
    max_evidence_chars: int,
) -> tuple[AuditRunner, GitReviewBasisProvider]:
    if not settings.llm_token:
        raise RuntimeError("LLM_API_KEY is required for an audit run")
    if not settings.hpc101_revision:
        raise RuntimeError("HPC101_REVISION must pin the authoritative upstream commit")
    if max_attempts > 2:
        raise ValueError("max_attempts must not exceed two")
    basis_provider = GitReviewBasisProvider(
        settings.hpc101_repository,
        settings.hpc101_revision,
    )
    pipeline = ReviewPipeline(
        submission_store=NfsSubmissionStore(settings.oj_root),
        basis_provider=basis_provider,
        delta_builder=BaselineDeltaBuilder(),
        similarity_detector=SimilarityDetector(max_workers=similarity_workers),
        similarity_policy=SimilarityPolicy(
            jaccard_threshold=similarity_threshold,
            shingle_size=shingle_size,
            num_permutations=num_permutations,
            band_size=band_size,
        ),
        reviewer=OpenAICompatibleReviewer(
            OpenAIStreamingChatClient(
                settings.llm_base_url,
                settings.llm_token,
                timeout_seconds=llm_timeout,
            ),
            clock=lambda: datetime.now(UTC),
            max_attempts=max_attempts,
        ),
        ledger=FileReviewLedger(output_root),
        source_policy=SourcePolicy(
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        ),
        model_parameters=(),
        compliance_scope_builder=ComplianceReviewScopeBuilder(
            BaselineDeltaBuilder(),
            lab4_references=(
                GitLab4ReferenceProvider(
                    settings.lab4_reference_repository,
                    settings.lab4_reference_revisions,
                )
                if settings.lab4_reference_revisions
                else None
            ),
        ),
        prompt_evidence_chars=max_evidence_chars,
    )
    return (
        AuditRunner(
            PostgresSubmissionCatalog(settings.database_url),
            FileReportStore(output_root),
            clock=lambda: datetime.now(UTC),
            git_commit=settings.git_commit,
            review_pipeline=pipeline,
        ),
        basis_provider,
    )


def _report_api(settings: Settings, args: argparse.Namespace) -> None:
    runner, _ = _build_review_runner(
        settings,
        output_root=settings.report_root,
        similarity_threshold=0.7,
        shingle_size=5,
        num_permutations=64,
        band_size=4,
        similarity_workers=1,
        max_file_bytes=10 << 20,
        max_total_bytes=10 << 20,
        llm_timeout=180,
        max_attempts=2,
        max_evidence_chars=240_000,
    )
    api = ComplianceApi(
        FileComplianceReportReader(settings.report_root),
        RunnerReviewLauncher(
            runner,
            min_score=args.min_score,
            clock=lambda: datetime.now(UTC),
        ),
        allowed_models=_parse_models(args.allowed_models),
        auth_token=args.api_token,
    )
    serve_report_api(api, args.listen)


def _nightly(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    if not settings.hpc101_revision:
        raise RuntimeError("HPC101_REVISION must pin the authoritative upstream commit")
    if not args.api_token:
        raise RuntimeError("REPORT_API_TOKEN is required for a nightly run")
    summary = NightlyReviewRunner(
        PostgresSubmissionCatalog(settings.database_url),
        HTTPComplianceClient(args.api_url, args.api_token),
        basis_commit=settings.hpc101_revision,
        clock=lambda: datetime.now(UTC),
    ).run()
    return summary.to_dict()


def _parse_models(value: str) -> tuple[str, ...]:
    models = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not models:
        raise ValueError("at least one allowed report API model is required")
    return models


def _agent_review_workspace(args: argparse.Namespace) -> dict[str, Any]:
    token = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not token:
        raise RuntimeError("LLM_API_KEY is required for an agent workspace review")
    root = Path(args.workspace)
    workspace = AgentWorkspace(
        root / "submission",
        root / "baseline",
        root / "context",
    )
    policy = _read_context_text(workspace.context, args.policy_file, required=True)
    if policy is None:
        raise RuntimeError("required workspace policy disappeared")
    raw_request = _read_context_text(workspace.context, args.request_file, required=False)
    model = args.model or "glm-5.3"
    if raw_request is None:
        submission: Mapping[str, Any] = {
            "id": root.name,
            "lab_id": args.lab,
        }
    else:
        parsed = json.loads(raw_request)
        if not isinstance(parsed, Mapping):
            raise ValueError("workspace request file must contain one JSON object")
        candidate = parsed.get("submission", parsed)
        if not isinstance(candidate, Mapping):
            raise ValueError("workspace request submission must be an object")
        submission = candidate
        request_model = parsed.get("model")
        if request_model is not None:
            if not isinstance(request_model, str) or not request_model.strip():
                raise ValueError("workspace request model is invalid")
            if args.model is not None and args.model != request_model:
                raise ValueError("requested model does not match the verified workspace request")
            model = request_model
    reviewer = WorkspaceAgentReviewer(
        OpenAICompatibleToolChatClient(
            os.environ.get(
                "LLM_BASE_URL",
                "http://new-api.new-api.svc.cluster.local:3000/v1",
            ),
            token,
            timeout_seconds=args.llm_timeout,
        ),
        max_turns=args.max_turns,
        max_attempts=args.max_attempts,
    )
    result = reviewer.review(
        model=model,
        broker=ReadOnlyToolBroker(workspace),
        policy=policy,
        submission=submission,
    )
    return {"model": model, "report": dict(result.report), "trace": dict(result.trace)}


def _prepare_agent_workspace_bundle(args: argparse.Namespace) -> dict[str, Any]:
    envelope = _read_bounded_regular_file(Path(args.bundle), max_bytes=1 << 20)
    key = _read_review_bundle_public_key(Path(args.public_key_file))
    verified = verify_review_bundle(
        envelope,
        {args.key_id: key},
        now=datetime.now(UTC),
    )
    submission = submission_from_review_bundle(verified)
    raw_basis = cast(Mapping[str, Any], verified.payload["basis"])
    revision = cast(str, raw_basis["commit"])
    provider = GitReviewBasisProvider(
        Path(os.environ.get("HPC101_REPOSITORY", "/baseline/HPC101")),
        revision,
    )
    basis = provider.load(submission.lab_id)

    raw_references = cast(list[Mapping[str, Any]], raw_basis["public_references"])
    references: tuple[ReferenceSnapshot, ...] = ()
    if raw_references:
        if submission.lab_id not in {"lab4-cpu", "lab4-gpu"}:
            raise ValueError("public reference snapshots are only configured for Lab 4")
        references = GitLab4ReferenceProvider(
            Path(
                os.environ.get(
                    "LAB4_REFERENCE_REPOSITORY",
                    "/baseline/NR-amssncku",
                )
            ),
            tuple(cast(str, item["revision"]) for item in raw_references),
            label=os.environ.get(
                "LAB4_REFERENCE_LABEL",
                "xiaoqu0000/NR-amssncku",
            ),
        ).load()

    policy = basis.document
    if submission.lab_id in {"lab4-cpu", "lab4-gpu"}:
        policy = f"{basis.document}\n\n{LAB4_REVIEW_POLICY}"
    prepared = AgentWorkspacePreparer(
        Path(os.environ.get("OJ_ROOT", "/data/.oj"))
    ).prepare(
        submission,
        basis,
        args.workspace,
        policy=policy,
        public_references=references,
        request_metadata=verified.payload,
        request_digest=verified.payload_digest,
    )
    return {
        **prepared.to_dict(),
        "bundle_digest": verified.payload_digest,
        "key_id": verified.key_id,
        "model": verified.payload["model"],
        "source": verified.payload["source"],
    }


def _agent_report_api(args: argparse.Namespace) -> None:
    if not args.api_token:
        raise RuntimeError("REPORT_API_TOKEN is required for the Agent report API")
    llm_token = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not llm_token:
        raise RuntimeError("LLM_API_KEY is required for the Agent report API")
    models = _parse_models(args.allowed_models)
    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    executor = LocalAgentRunExecutor(
        oj_root=Path(args.oj_root),
        hpc101_repository=Path(args.hpc101_repository),
        lab4_reference_repository=Path(args.lab4_reference_repository),
        lab4_reference_label=args.lab4_reference_label,
        work_root=work_root,
        client=OpenAICompatibleToolChatClient(
            os.environ.get(
                "LLM_BASE_URL",
                "http://new-api.new-api.svc.cluster.local:3000/v1",
            ),
            llm_token,
            timeout_seconds=args.llm_timeout,
        ),
        max_turns=args.max_turns,
        max_attempts=args.max_attempts,
    )
    runs = FileAgentRunService(
        Path(args.report_root),
        public_keys={
            args.key_id: _read_review_bundle_public_key(Path(args.public_key_file))
        },
        allowed_models=models,
        executor=executor,
        worker_count=args.worker_count,
        max_queued=args.max_queued,
    )
    api = ComplianceApi(
        FileComplianceReportReader(Path(args.report_root)),
        _DisabledReviewLauncher(),
        allowed_models=models,
        auth_token=args.api_token,
        agent_runs=runs,
    )
    runs.start()
    try:
        serve_report_api(api, args.listen)
    finally:
        runs.close()


class _DisabledReviewLauncher:
    def launch(self, submission_id: str, model: str) -> ReviewLaunchResult:
        return ReviewLaunchResult(
            run_id=f"disabled-{submission_id}",
            error="ASYNC_REVIEW_REQUIRED",
        )


def _describe_review_bundle_config(args: argparse.Namespace) -> dict[str, Any]:
    if not args.hpc101_revision:
        raise RuntimeError("HPC101_REVISION must pin the authoritative upstream commit")
    provider = GitReviewBasisProvider(args.hpc101_repository, args.hpc101_revision)
    references: tuple[ReferenceSnapshot, ...] = ()
    if args.lab4_reference_revisions:
        references = GitLab4ReferenceProvider(
            args.lab4_reference_repository,
            tuple(args.lab4_reference_revisions),
            label=args.lab4_reference_label,
        ).load()
    labs: dict[str, Any] = {}
    for lab_id in REVIEW_LABS:
        basis = provider.load(lab_id)
        lab_references = references if lab_id in {"lab4-cpu", "lab4-gpu"} else ()
        labs[lab_id] = {
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
                for index, reference in enumerate(lab_references)
            ],
        }
    return {
        "allowed_models": list(_parse_models(args.allowed_models)),
        "rules_version": args.rules_version,
        "prompt_version": args.prompt_version,
        "tool_version": args.tool_version,
        "result_schema_version": args.result_schema_version,
        "validity_seconds": args.validity_seconds,
        "labs": labs,
    }


def _prepare_agent_workspace(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    snapshot = PostgresSubmissionCatalog(settings.database_url).snapshot(
        SnapshotRequest(
            SelectionPolicy.ALL_QUALIFYING,
            cutoff=datetime.now(UTC),
            min_score=0,
            submission_ids=(args.submission_id,),
        )
    )
    if len(snapshot.submissions) != 1:
        raise LookupError("submission is not reviewable")
    submission = snapshot.submissions[0]
    if not settings.hpc101_revision:
        raise RuntimeError("HPC101_REVISION must pin the authoritative upstream commit")
    basis = GitReviewBasisProvider(
        settings.hpc101_repository,
        settings.hpc101_revision,
    ).load(submission.lab_id)
    references: tuple[ReferenceSnapshot, ...] = ()
    if submission.lab_id in {"lab4-cpu", "lab4-gpu"} and settings.lab4_reference_revisions:
        references = GitLab4ReferenceProvider(
            settings.lab4_reference_repository,
            settings.lab4_reference_revisions,
        ).load()
    policy = basis.document
    if submission.lab_id in {"lab4-cpu", "lab4-gpu"}:
        policy = f"{basis.document}\n\n{LAB4_REVIEW_POLICY}"
    prepared = AgentWorkspacePreparer(settings.oj_root).prepare(
        submission,
        basis,
        args.workspace,
        policy=policy,
        public_references=references,
    )
    return prepared.to_dict()


def _read_context_text(root: Path, name: str, *, required: bool) -> str | None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("context filename must be one basename")
    target = root / name
    if target.is_symlink():
        raise ValueError(f"context file {name!r} must not be a symlink")
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        if required:
            raise ValueError(f"required context file is missing: {name}") from None
        return None
    if not target.is_file() or size > 1 << 20:
        raise ValueError(f"context file is invalid or too large: {name}")
    return target.read_text(encoding="utf-8")


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise ValueError("file size limit must be positive")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ValueError(f"file is invalid or exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
        return content
    finally:
        os.close(descriptor)


def _read_review_bundle_public_key(path: Path) -> bytes | str:
    raw_key = _read_bounded_regular_file(path, max_bytes=4096)
    if len(raw_key) == 32:
        return raw_key
    try:
        return raw_key.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError("review bundle public key must be raw or base64url") from error


def _parse_cutoff(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cutoff must include a timezone")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oj-checker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check read-only DB and submission NFS")
    doctor.add_argument("--lab")

    plan = subparsers.add_parser("plan", help="create an audit task manifest without LLM calls")
    _add_plan_arguments(plan)

    smoke = subparsers.add_parser("smoke", help="run doctor and a limited plan without LLM calls")
    _add_plan_arguments(smoke)

    report_api = subparsers.add_parser(
        "report-api",
        help="serve single-submission compliance report and review endpoints",
    )
    report_api.add_argument(
        "--listen",
        default=os.environ.get("REPORT_API_LISTEN", "0.0.0.0:8080"),
    )
    report_api.add_argument(
        "--allowed-models",
        default=os.environ.get(
            "REPORT_API_ALLOWED_MODELS",
            "glm-5.3,gpt-5.6-luna",
        ),
    )
    report_api.add_argument("--min-score", type=int, default=0)
    report_api.add_argument("--api-token", default=os.environ.get("REPORT_API_TOKEN"))

    agent_api = subparsers.add_parser(
        "agent-report-api",
        help="serve signed asynchronous Agent review runs without database access",
    )
    agent_api.add_argument(
        "--listen",
        default=os.environ.get("REPORT_API_LISTEN", "0.0.0.0:8080"),
    )
    agent_api.add_argument(
        "--allowed-models",
        default=os.environ.get("REPORT_API_ALLOWED_MODELS", "glm-5.3,gpt-5.6-luna"),
    )
    agent_api.add_argument("--api-token", default=os.environ.get("REPORT_API_TOKEN"))
    agent_api.add_argument("--report-root", default=os.environ.get("REPORT_ROOT", "audit-reports"))
    agent_api.add_argument("--oj-root", default=os.environ.get("OJ_ROOT", "/data/.oj"))
    agent_api.add_argument(
        "--hpc101-repository",
        default=os.environ.get("HPC101_REPOSITORY", "/baseline/HPC101"),
    )
    agent_api.add_argument(
        "--lab4-reference-repository",
        default=os.environ.get("LAB4_REFERENCE_REPOSITORY", "/baseline/NR-amssncku"),
    )
    agent_api.add_argument(
        "--lab4-reference-label",
        default=os.environ.get("LAB4_REFERENCE_LABEL", "xiaoqu0000/NR-amssncku"),
    )
    agent_api.add_argument(
        "--work-root",
        default=os.environ.get("AGENT_WORK_ROOT", "/tmp/oj-agent"),
    )
    agent_api.add_argument("--worker-count", type=int, default=2)
    agent_api.add_argument("--max-queued", type=int, default=1000)
    agent_api.add_argument("--max-turns", type=int, default=32)
    agent_api.add_argument("--max-attempts", type=int, default=2)
    agent_api.add_argument("--llm-timeout", type=float, default=180)
    agent_api.add_argument(
        "--key-id",
        default=os.environ.get("REVIEW_BUNDLE_KEY_ID", "plat101-review-2026-01"),
    )
    agent_api.add_argument(
        "--public-key-file",
        default=os.environ.get("REVIEW_BUNDLE_PUBLIC_KEY_FILE"),
        required=os.environ.get("REVIEW_BUNDLE_PUBLIC_KEY_FILE") is None,
    )

    describe_bundle = subparsers.add_parser(
        "describe-review-bundle-config",
        help="derive Plat101's trusted signed review config from immutable local baselines",
    )
    describe_bundle.add_argument(
        "--hpc101-repository",
        default=os.environ.get("HPC101_REPOSITORY", "/baseline/HPC101"),
    )
    describe_bundle.add_argument(
        "--hpc101-revision",
        default=os.environ.get("HPC101_REVISION", ""),
    )
    describe_bundle.add_argument(
        "--lab4-reference-repository",
        default=os.environ.get("LAB4_REFERENCE_REPOSITORY", "/baseline/NR-amssncku"),
    )
    describe_bundle.add_argument(
        "--lab4-reference-revisions",
        nargs="*",
        default=tuple(
            item.strip()
            for item in os.environ.get("LAB4_REFERENCE_REVISIONS", "").split(",")
            if item.strip()
        ),
    )
    describe_bundle.add_argument(
        "--lab4-reference-label",
        default=os.environ.get("LAB4_REFERENCE_LABEL", "xiaoqu0000/NR-amssncku"),
    )
    describe_bundle.add_argument(
        "--allowed-models",
        default=os.environ.get("REPORT_API_ALLOWED_MODELS", "glm-5.3,gpt-5.6-luna"),
    )
    describe_bundle.add_argument("--rules-version", default="audit-rules-v2")
    describe_bundle.add_argument("--prompt-version", default="agent-compliance-v1")
    describe_bundle.add_argument("--tool-version", default="agent-tools-v1")
    describe_bundle.add_argument(
        "--result-schema-version",
        default="agent-review-result-v1",
    )
    describe_bundle.add_argument("--validity-seconds", type=int, default=900)

    nightly = subparsers.add_parser(
        "nightly",
        help="review authoritative best submissions through the report API",
    )
    nightly.add_argument(
        "--api-url",
        default=os.environ.get(
            "REPORT_API_URL",
            "http://oj-checker-report-api.csoj-judger.svc.cluster.local:8080",
        ),
    )
    nightly.add_argument("--api-token", default=os.environ.get("REPORT_API_TOKEN"))

    agent = subparsers.add_parser(
        "agent-review-workspace",
        help="review one prepared read-only workspace through native model tools",
    )
    agent.add_argument("--workspace", required=True)
    agent.add_argument("--model")
    agent.add_argument("--lab", default="unknown")
    agent.add_argument("--policy-file", default="lab-policy.md")
    agent.add_argument("--request-file", default="request.json")
    agent.add_argument("--max-turns", type=int, default=32)
    agent.add_argument("--max-attempts", type=int, default=2)
    agent.add_argument("--llm-timeout", type=float, default=180)

    prepare_agent = subparsers.add_parser(
        "prepare-agent-workspace",
        help="copy one complete validated submission into an isolated workspace",
    )
    prepare_agent.add_argument("--submission-id", required=True)
    prepare_agent.add_argument("--workspace", required=True)

    prepare_bundle = subparsers.add_parser(
        "prepare-agent-workspace-bundle",
        help="verify a Plat101 bundle and prepare one complete isolated workspace",
    )
    prepare_bundle.add_argument("--bundle", required=True)
    prepare_bundle.add_argument("--workspace", required=True)
    prepare_bundle.add_argument(
        "--key-id",
        default=os.environ.get("REVIEW_BUNDLE_KEY_ID", "plat101-review-2026-01"),
    )
    prepare_bundle.add_argument(
        "--public-key-file",
        default=os.environ.get("REVIEW_BUNDLE_PUBLIC_KEY_FILE"),
        required=os.environ.get("REVIEW_BUNDLE_PUBLIC_KEY_FILE") is None,
    )

    audit = subparsers.add_parser(
        "audit",
        help="review all students' best submissions and all historical plagiarism candidates",
    )
    audit.add_argument("--run-id")
    audit.add_argument("--cutoff")
    audit.add_argument("--min-score", type=int, default=60)
    audit.add_argument("--lab", required=True, help="canary and production runs process one lab")
    audit.add_argument("--output-root")
    audit.add_argument("--model", default="gpt-5.6-luna")
    audit.add_argument("--rules-version", default="audit-rules-v2")
    audit.add_argument("--similarity-threshold", type=float, default=0.7)
    audit.add_argument("--shingle-size", type=int, default=5)
    audit.add_argument("--num-permutations", type=int, default=64)
    audit.add_argument("--band-size", type=int, default=4)
    audit.add_argument("--similarity-workers", type=int, default=8)
    audit.add_argument("--max-file-bytes", type=int, default=10 << 20)
    audit.add_argument("--max-total-bytes", type=int, default=10 << 20)
    audit.add_argument("--llm-timeout", type=float, default=180)
    audit.add_argument("--max-attempts", type=int, default=2)
    audit.add_argument("--max-evidence-chars", type=int, default=240_000)
    return parser


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id")
    parser.add_argument("--cutoff")
    parser.add_argument("--min-score", type=int, default=60)
    parser.add_argument("--lab")
    parser.add_argument("--owner")
    parser.add_argument("--limit", type=int, help="limit single-review tasks only")
    parser.add_argument("--output-root")
