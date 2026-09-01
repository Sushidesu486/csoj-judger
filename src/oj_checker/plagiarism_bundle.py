"""Verification for signed single-target plagiarism review requests."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from oj_checker.domain import Submission
from oj_checker.review_bundle import (
    ReviewBundleError,
    VerifiedReviewBundle,
    _aware_utc,
    _nonempty_string,
    _parse_timestamp,
    _require_exact_keys,
    _timestamp,
    _validate_basis,
    _verify_signed_payload,
)

_SCHEMA_VERSION = "plagiarism-review-bundle-v1"
_AUDIENCE = "oj-checker"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_VALIDITY = timedelta(minutes=15)
_MAX_SUBMISSIONS = 1000


def verify_plagiarism_bundle(
    envelope: bytes | bytearray | str | Mapping[str, Any],
    public_keys: Mapping[str, bytes | bytearray | str],
    *,
    now: datetime | None = None,
) -> VerifiedReviewBundle:
    payload, payload_bytes, payload_digest, key_id = _verify_signed_payload(
        envelope, public_keys
    )
    _validate_payload(payload, now=now)
    return VerifiedReviewBundle(payload, payload_bytes, payload_digest, key_id)


def submissions_from_plagiarism_bundle(
    bundle: VerifiedReviewBundle,
) -> tuple[str, tuple[Submission, ...]]:
    lab_definition = cast(Mapping[str, Any], bundle.payload["lab_definition"])
    submissions = tuple(
        _submission_from_payload(cast(Mapping[str, Any], raw), lab_definition)
        for raw in cast(list[object], bundle.payload["submissions"])
    )
    return cast(str, bundle.payload["target_submission_id"]), submissions


def _submission_from_payload(
    value: Mapping[str, Any], lab_definition: Mapping[str, Any]
) -> Submission:
    return Submission(
        id=cast(str, value["id"]),
        owner=cast(str, value["owner"]),
        lab_id=cast(str, value["lab_id"]),
        score=cast(int, value["score"]),
        input_digest=cast(str, value["input_digest"]),
        submitted_at=_parse_timestamp(cast(str, value["submitted_at"]), "submitted_at"),
        input_manifest=cast(Mapping[str, Any], value["input_manifest"]),
        lab_definition=lab_definition,
    )


def _validate_payload(payload: Mapping[str, Any], *, now: datetime | None) -> None:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "audience",
            "target_submission_id",
            "submissions",
            "lab_definition",
            "basis",
            "model",
            "source",
            "rules_version",
            "prompt_version",
            "result_schema_version",
            "issued_at",
            "expires_at",
            "nonce",
        },
        "payload",
    )
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise ReviewBundleError("unsupported plagiarism bundle schema version")
    if payload["audience"] != _AUDIENCE:
        raise ReviewBundleError("bundle audience is not oj-checker")
    _nonempty_string(payload, "model")
    if _nonempty_string(payload, "source") != "manual":
        raise ReviewBundleError("plagiarism bundle source must be manual")
    for field in ("rules_version", "prompt_version", "result_schema_version"):
        _nonempty_string(payload, field)
    if payload["prompt_version"] != "plagiarism-v2":
        raise ReviewBundleError("unsupported plagiarism prompt version")
    if payload["result_schema_version"] != "plagiarism-result-v1":
        raise ReviewBundleError("unsupported plagiarism result schema version")
    nonce = _nonempty_string(payload, "nonce")
    if len(nonce) < 16 or len(nonce) > 128:
        raise ReviewBundleError("nonce length is invalid")
    issued_at = _timestamp(payload, "issued_at")
    expires_at = _timestamp(payload, "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > _MAX_VALIDITY:
        raise ReviewBundleError("plagiarism bundle validity window is invalid")
    if now is not None:
        current = _aware_utc(now)
        if current < issued_at or current >= expires_at:
            raise ReviewBundleError("bundle is outside its validity window")
    target_id = _canonical_uuid(payload.get("target_submission_id"), "target_submission_id")
    submissions = payload.get("submissions")
    if (
        not isinstance(submissions, list)
        or not submissions
        or len(submissions) > _MAX_SUBMISSIONS
    ):
        raise ReviewBundleError("submissions must contain 1-1000 entries")
    seen: set[str] = set()
    lab_id: str | None = None
    for raw_submission in submissions:
        current_id, current_lab = _validate_submission(raw_submission)
        if current_id in seen:
            raise ReviewBundleError("plagiarism bundle submissions must be unique")
        seen.add(current_id)
        if lab_id is None:
            lab_id = current_lab
        elif current_lab != lab_id:
            raise ReviewBundleError("plagiarism bundle submissions must share one lab")
    if target_id not in seen:
        raise ReviewBundleError("target submission is absent from the corpus")
    if not isinstance(payload["lab_definition"], dict):
        raise ReviewBundleError("lab_definition must be an object")
    _validate_basis(payload["basis"])


def _validate_submission(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ReviewBundleError("plagiarism submission must be an object")
    _require_exact_keys(
        value,
        {"id", "owner", "lab_id", "score", "input_digest", "submitted_at", "input_manifest"},
        "submissions[]",
    )
    submission_id = _canonical_uuid(value.get("id"), "submissions[].id")
    owner = _nonempty_string(value, "owner")
    if len(owner) > 256:
        raise ReviewBundleError("submission owner is too long")
    lab_id = _nonempty_string(value, "lab_id")
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or score < 0:
        raise ReviewBundleError("submission score must be a non-negative integer")
    digest = value.get("input_digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ReviewBundleError("submission input_digest must be SHA-256")
    _timestamp(value, "submitted_at")
    if not isinstance(value.get("input_manifest"), dict):
        raise ReviewBundleError("submission input_manifest must be an object")
    return submission_id, lab_id


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewBundleError(f"{field} must be a UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as error:
        raise ReviewBundleError(f"{field} must be a UUID") from error
    if parsed != value:
        raise ReviewBundleError(f"{field} must be a canonical UUID")
    return parsed
