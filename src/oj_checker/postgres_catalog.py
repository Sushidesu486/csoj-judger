from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row

from oj_checker.domain import SelectionPolicy, SnapshotRequest, Submission, SubmissionSnapshot

_READ_ONLY_OPTIONS = " ".join(
    (
        "-c default_transaction_read_only=on",
        "-c statement_timeout=30000",
        "-c lock_timeout=1000",
        "-c idle_in_transaction_session_timeout=30000",
        "-c application_name=oj-checker",
    )
)


class PostgresSubmissionCatalog:
    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 10) -> None:
        self._database_url = _normalize_database_url(database_url)
        self._connect_timeout_seconds = connect_timeout_seconds

    def snapshot(self, request: SnapshotRequest) -> SubmissionSnapshot:
        with psycopg.connect(
            self._database_url,
            autocommit=False,
            row_factory=dict_row,
            connect_timeout=self._connect_timeout_seconds,
            options=_READ_ONLY_OPTIONS,
        ) as connection:
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            with connection.transaction():
                read_only_row = connection.execute("SHOW transaction_read_only").fetchone()
                if read_only_row is None or read_only_row["transaction_read_only"] != "on":
                    raise RuntimeError("database transaction is not read-only")
                rows = connection.execute(*self._query(request)).fetchall()

        submissions = tuple(self._submission_from_row(row) for row in rows)
        return SubmissionSnapshot(request.policy, request.cutoff, submissions)

    @staticmethod
    def _query(request: SnapshotRequest) -> tuple[str, Sequence[Any]]:
        score_column = (
            "b.score"
            if request.policy is SelectionPolicy.BEST_PER_OWNER_LAB
            else "s.score"
        )
        conditions = [
            "s.status = %s",
            "s.is_valid = true",
            f"{score_column} >= %s",
            "s.submitted_at <= %s",
        ]
        parameters: list[Any] = ["Success", request.min_score, request.cutoff]

        if request.labs:
            conditions.append("s.lab_id = ANY(%s)")
            parameters.append(list(request.labs))
        if request.owners:
            conditions.append("s.owner = ANY(%s)")
            parameters.append(list(request.owners))
        if request.submission_ids:
            conditions.append("s.id = ANY(%s::uuid[])")
            parameters.append(list(request.submission_ids))

        where_clause = " AND ".join(conditions)
        active_run_column = (
            "b.submission_run_id"
            if request.policy is SelectionPolicy.BEST_PER_OWNER_LAB
            else "s.active_result_run_id"
        )
        columns = f"""
            s.id::text AS id,
            s.owner,
            s.lab_id,
            {score_column} AS score,
            s.input_digest,
            s.submitted_at,
            COALESCE(s.input_manifest, '{{}}'::jsonb) AS input_manifest,
            COALESCE(r.lab_definition, '{{}}'::jsonb) AS lab_definition,
            {active_run_column} AS active_run_id,
            r.state AS run_state,
            COALESCE(r.result_info, '{{}}'::jsonb) AS run_result_info,
            r.failure_class AS run_failure_class,
            r.failure_reason AS run_failure_reason,
            r.score AS run_score,
            r.performance AS run_performance,
            r.finished_at AS run_finished_at
        """
        if request.policy is SelectionPolicy.BEST_PER_OWNER_LAB:
            source = """
                FROM oj_user_lab_best_scores b
                JOIN oj_submissions s ON s.id = b.submission_id
                JOIN oj_submission_runs r ON r.id = b.submission_run_id
            """
        else:
            source = """
                FROM oj_submissions s
                LEFT JOIN oj_submission_runs r ON r.id = s.active_result_run_id
            """

        query = f"""
            SELECT {columns}
            {source}
            WHERE {where_clause}
            ORDER BY s.owner, s.lab_id, s.submitted_at, s.id
        """

        if request.limit is not None:
            query += " LIMIT %s"
            parameters.append(request.limit)

        return query, parameters

    @staticmethod
    def _submission_from_row(row: dict[str, Any]) -> Submission:
        return Submission(
            id=row["id"],
            owner=row["owner"],
            lab_id=row["lab_id"],
            score=row["score"],
            input_digest=row["input_digest"] or "",
            submitted_at=row["submitted_at"],
            input_manifest=row["input_manifest"],
            lab_definition=row["lab_definition"],
            active_run_id=row["active_run_id"],
            run_state=row["run_state"],
            run_result_info=row["run_result_info"],
            run_failure_class=row["run_failure_class"],
            run_failure_reason=row["run_failure_reason"],
            run_score=row["run_score"],
            run_performance=row["run_performance"],
            run_finished_at=row["run_finished_at"],
        )


def _normalize_database_url(database_url: str) -> str:
    return database_url.replace(
        "@plat101-db:", "@plat101-db.plat101-system.svc.cluster.local:"
    )
