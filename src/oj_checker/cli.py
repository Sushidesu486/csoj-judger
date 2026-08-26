import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never

from oj_checker.catalog import best_per_owner_lab
from oj_checker.domain import AuditRequest, SelectionPolicy, SnapshotRequest
from oj_checker.postgres_catalog import PostgresSubmissionCatalog
from oj_checker.report_api import (
    ComplianceApi,
    FileComplianceReportReader,
    RunnerReviewLauncher,
    serve_report_api,
)
from oj_checker.report_store import FileReportStore
from oj_checker.review_basis import GitReviewBasisProvider
from oj_checker.review_ledger import FileReviewLedger
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
        settings = Settings.from_environment()
        if args.command == "report-api":
            _report_api(settings, args)
            return 0
        if args.command == "doctor":
            result = _doctor(settings, lab=args.lab)
        elif args.command == "plan":
            result = _plan(settings, args)
        elif args.command == "smoke":
            doctor = _doctor(settings, lab=args.lab)
            plan = _plan(settings, args)
            result = {"doctor": doctor, "plan": plan, "llm_checked": False}
        else:
            result = _audit(settings, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"status": "error", "error_type": type(error).__name__, "message": str(error)},
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
        model=args.model,
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
            prompt_version="compliance-v1+plagiarism-v1",
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
    model: str,
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
        model=args.model,
        similarity_threshold=0.7,
        shingle_size=5,
        num_permutations=64,
        band_size=4,
        similarity_workers=1,
        max_file_bytes=1_000_000,
        max_total_bytes=4_000_000,
        llm_timeout=180,
        max_attempts=2,
        max_evidence_chars=240_000,
    )
    api = ComplianceApi(
        FileComplianceReportReader(settings.report_root),
        RunnerReviewLauncher(
            runner,
            model=args.model,
            min_score=args.min_score,
            clock=lambda: datetime.now(UTC),
        ),
        auth_token=args.api_token,
    )
    serve_report_api(api, args.listen)


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
        "--model",
        default=os.environ.get("REPORT_API_MODEL", "gpt-5.6-luna"),
    )
    report_api.add_argument("--min-score", type=int, default=0)
    report_api.add_argument("--api-token", default=os.environ.get("REPORT_API_TOKEN"))

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
    audit.add_argument("--rules-version", default="audit-rules-v1")
    audit.add_argument("--similarity-threshold", type=float, default=0.7)
    audit.add_argument("--shingle-size", type=int, default=5)
    audit.add_argument("--num-permutations", type=int, default=64)
    audit.add_argument("--band-size", type=int, default=4)
    audit.add_argument("--similarity-workers", type=int, default=8)
    audit.add_argument("--max-file-bytes", type=int, default=1_000_000)
    audit.add_argument("--max-total-bytes", type=int, default=4_000_000)
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
