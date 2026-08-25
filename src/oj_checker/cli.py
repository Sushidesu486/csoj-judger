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
from oj_checker.report_store import FileReportStore
from oj_checker.runner import AuditRunner
from oj_checker.submission_store import NfsSubmissionStore, SourcePolicy


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    oj_root: Path
    report_root: Path
    git_commit: str

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
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_environment()
        if args.command == "doctor":
            result = _doctor(settings, lab=args.lab)
        elif args.command == "plan":
            result = _plan(settings, args)
        else:
            doctor = _doctor(settings, lab=args.lab)
            plan = _plan(settings, args)
            result = {"doctor": doctor, "plan": plan, "llm_checked": False}
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
    return parser


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id")
    parser.add_argument("--cutoff")
    parser.add_argument("--min-score", type=int, default=60)
    parser.add_argument("--lab")
    parser.add_argument("--owner")
    parser.add_argument("--limit", type=int, help="limit single-review tasks only")
    parser.add_argument("--output-root")
