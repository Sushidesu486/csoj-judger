"""Verification for the signed review-bundle-v1 interchange format.

The bundle deliberately signs the original JSON bytes instead of a language
specific canonical representation.  Plat101 therefore does not need to
reproduce Python's JSON canonicalisation (or vice versa): the checker verifies
the Ed25519 signature first and only then parses the signed payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from oj_checker.domain import Submission

_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = "review-bundle-v1"
_AUDIENCE = "oj-checker"
_NIGHTLY_MODEL = "glm-5.3"
_MAX_VALIDITY = timedelta(minutes=15)


class ReviewBundleError(ValueError):
    """The envelope or signed payload is not a valid review bundle."""


@dataclass(frozen=True, slots=True)
class VerifiedReviewBundle:
    """A verified, validated bundle and the exact bytes that were signed."""

    payload: Mapping[str, Any]
    payload_bytes: bytes
    payload_digest: str
    key_id: str


def verify_review_bundle(
    envelope: bytes | bytearray | str | Mapping[str, Any],
    public_keys: Mapping[str, bytes | bytearray | str],
    *,
    now: datetime | None = None,
) -> VerifiedReviewBundle:
    """Verify an Ed25519-signed review bundle.

    ``public_keys`` is keyed by the envelope's key ID.  String keys and
    signatures use unpadded base64url, while raw 32-byte public keys are also
    accepted for callers that already decoded their key material.
    """

    parsed_envelope = _parse_envelope(envelope)
    key_id = parsed_envelope["key_id"]
    raw_key = public_keys.get(key_id)
    if raw_key is None:
        raise ReviewBundleError("unknown signing key")
    public_key = _decode_key(raw_key)
    payload_bytes = _decode_b64url(parsed_envelope["payload"], "payload")
    signature = _decode_b64url(parsed_envelope["signature"], "signature")
    if len(signature) != 64:
        raise ReviewBundleError("signature must be 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload_bytes)
    except InvalidSignature as error:
        raise ReviewBundleError("invalid bundle signature") from error

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewBundleError("signed payload must be UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ReviewBundleError("signed payload must be a JSON object")
    _validate_payload(payload, now=now)
    return VerifiedReviewBundle(
        payload=payload,
        payload_bytes=payload_bytes,
        payload_digest=hashlib.sha256(payload_bytes).hexdigest(),
        key_id=key_id,
    )


def submission_from_review_bundle(bundle: VerifiedReviewBundle) -> Submission:
    """Convert already verified request metadata into the domain model."""

    raw_submission = cast(Mapping[str, Any], bundle.payload["submission"])
    active_run = cast(Mapping[str, Any], raw_submission["active_run"])
    performance = active_run["performance"]
    finished_at = cast(str, active_run["finished_at"])
    return Submission(
        id=cast(str, raw_submission["id"]),
        owner=cast(str, raw_submission["owner"]),
        lab_id=cast(str, raw_submission["lab_id"]),
        score=cast(int, raw_submission["score"]),
        input_digest=cast(str, raw_submission["input_digest"]),
        submitted_at=_parse_timestamp(cast(str, raw_submission["submitted_at"]), "submitted_at"),
        input_manifest=cast(Mapping[str, Any], raw_submission["input_manifest"]),
        lab_definition=cast(Mapping[str, Any], raw_submission["lab_definition"]),
        active_run_id=cast(int, active_run["id"]),
        run_state=cast(str, active_run["state"]),
        run_result_info=cast(Mapping[str, Any], active_run["result_info"]),
        run_score=cast(int | None, active_run["score"]),
        run_performance=(None if performance is None else float(cast(int | float, performance))),
        run_finished_at=_parse_timestamp(finished_at, "active_run.finished_at"),
    )


def _parse_envelope(
    envelope: bytes | bytearray | str | Mapping[str, Any],
) -> dict[str, str]:
    if isinstance(envelope, Mapping):
        value: object = dict(envelope)
    else:
        if isinstance(envelope, (bytes, bytearray)):
            try:
                text = bytes(envelope).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReviewBundleError("envelope must be UTF-8 JSON") from error
        elif isinstance(envelope, str):
            text = envelope
        else:
            raise ReviewBundleError("envelope must be JSON bytes or an object")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ReviewBundleError("envelope must be JSON") from error
    if not isinstance(value, dict) or set(value) != {"payload", "key_id", "signature"}:
        raise ReviewBundleError("envelope must contain exactly payload, key_id and signature")
    payload = value.get("payload")
    key_id = value.get("key_id")
    signature = value.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str):
        raise ReviewBundleError("payload and signature must be strings")
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise ReviewBundleError("key_id is invalid")
    return {"payload": payload, "key_id": key_id, "signature": signature}


def _decode_b64url(value: str, field: str) -> bytes:
    if not value or "=" in value or _B64URL.fullmatch(value) is None:
        raise ReviewBundleError(f"{field} must be unpadded base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as error:
        raise ReviewBundleError(f"{field} is invalid base64url") from error


def _decode_key(value: bytes | bytearray | str) -> bytes:
    if isinstance(value, str):
        if value.startswith("-----BEGIN PUBLIC KEY-----"):
            try:
                loaded = serialization.load_pem_public_key(value.encode("ascii"))
            except (ValueError, TypeError) as error:
                raise ReviewBundleError("public key PEM is invalid") from error
            if not isinstance(loaded, Ed25519PublicKey):
                raise ReviewBundleError("public key PEM must contain Ed25519")
            decoded = loaded.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        else:
            decoded = _decode_b64url(value, "public key")
    elif isinstance(value, (bytes, bytearray)):
        decoded = bytes(value)
    else:
        raise ReviewBundleError("public key must be raw bytes or base64url")
    if len(decoded) != 32:
        raise ReviewBundleError("Ed25519 public key must be 32 bytes")
    return decoded


def _validate_payload(payload: Mapping[str, Any], *, now: datetime | None) -> None:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "audience",
            "submission",
            "basis",
            "model",
            "source",
            "rules_version",
            "prompt_version",
            "tool_version",
            "result_schema_version",
            "issued_at",
            "expires_at",
            "nonce",
        },
        "payload",
    )
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise ReviewBundleError("unsupported bundle schema version")
    if payload["audience"] != _AUDIENCE:
        raise ReviewBundleError("bundle audience is not oj-checker")
    model = _nonempty_string(payload, "model")
    source = _nonempty_string(payload, "source")
    if source not in {"manual", "nightly"}:
        raise ReviewBundleError("source must be manual or nightly")
    if source == "nightly" and model != _NIGHTLY_MODEL:
        raise ReviewBundleError("nightly bundles must use glm-5.3")
    for field in ("rules_version", "prompt_version", "tool_version", "result_schema_version"):
        _nonempty_string(payload, field)
    nonce = _nonempty_string(payload, "nonce")
    if len(nonce) < 16 or len(nonce) > 128:
        raise ReviewBundleError("nonce length is invalid")
    issued_at = _timestamp(payload, "issued_at")
    expires_at = _timestamp(payload, "expires_at")
    if expires_at <= issued_at:
        raise ReviewBundleError("expires_at must be after issued_at")
    if expires_at - issued_at > _MAX_VALIDITY:
        raise ReviewBundleError("bundle validity window exceeds 15 minutes")
    if now is not None:
        current = _aware_utc(now)
        if current < issued_at or current >= expires_at:
            raise ReviewBundleError("bundle is outside its validity window")
    _validate_submission(payload["submission"])
    _validate_basis(payload["basis"])


def _validate_submission(value: object) -> None:
    if not isinstance(value, dict):
        raise ReviewBundleError("submission must be an object")
    _require_exact_keys(
        value,
        {
            "id",
            "owner",
            "lab_id",
            "score",
            "input_digest",
            "submitted_at",
            "input_manifest",
            "lab_definition",
            "active_run",
        },
        "submission",
    )
    submission_id = value["id"]
    if not isinstance(submission_id, str):
        raise ReviewBundleError("submission.id must be a string")
    try:
        parsed_submission_id = uuid.UUID(submission_id)
    except ValueError as error:
        raise ReviewBundleError("submission.id must be a UUID") from error
    if str(parsed_submission_id) != submission_id:
        raise ReviewBundleError("submission.id must be a canonical UUID")
    for field in ("owner", "lab_id"):
        _nonempty_string(value, field)
    score = value["score"]
    if isinstance(score, bool) or not isinstance(score, int) or score < 0:
        raise ReviewBundleError("submission.score must be a non-negative integer")
    input_digest = value["input_digest"]
    if not isinstance(input_digest, str) or _DIGEST.fullmatch(input_digest) is None:
        raise ReviewBundleError("submission.input_digest must be a SHA-256 digest")
    _timestamp(value, "submitted_at")
    if not isinstance(value["input_manifest"], dict):
        raise ReviewBundleError("submission.input_manifest must be an object")
    if not isinstance(value["lab_definition"], dict):
        raise ReviewBundleError("submission.lab_definition must be an object")
    active_run = value["active_run"]
    if not isinstance(active_run, dict):
        raise ReviewBundleError("submission.active_run must be an object")
    _require_exact_keys(
        active_run,
        {"id", "state", "result_info", "score", "performance", "finished_at"},
        "submission.active_run",
    )
    run_id = active_run["id"]
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise ReviewBundleError("submission.active_run.id is invalid")
    if _nonempty_string(active_run, "state") != "Success":
        raise ReviewBundleError("submission.active_run must be successful")
    if not isinstance(active_run["result_info"], dict):
        raise ReviewBundleError("submission.active_run.result_info must be an object")
    run_score = active_run["score"]
    if run_score is not None and (
        isinstance(run_score, bool) or not isinstance(run_score, int) or run_score < 0
    ):
        raise ReviewBundleError("submission.active_run.score is invalid")
    performance = active_run["performance"]
    if performance is not None and (
        isinstance(performance, bool) or not isinstance(performance, (int, float))
    ):
        raise ReviewBundleError("submission.active_run.performance is invalid")
    finished_at = active_run["finished_at"]
    if not isinstance(finished_at, str):
        raise ReviewBundleError("submission.active_run.finished_at is invalid")
    _parse_timestamp(finished_at, "submission.active_run.finished_at")


def _validate_basis(value: object) -> None:
    if not isinstance(value, dict):
        raise ReviewBundleError("basis must be an object")
    _require_exact_keys(
        value,
        {
            "commit",
            "tree_digest",
            "document_digest",
            "source_path",
            "document_path",
            "public_references",
        },
        "basis",
    )
    for field in ("commit", "source_path", "document_path"):
        _nonempty_string(value, field)
    for field in ("tree_digest", "document_digest"):
        digest = value[field]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ReviewBundleError(f"basis.{field} must be a SHA-256 digest")
    references = value["public_references"]
    if not isinstance(references, list):
        raise ReviewBundleError("basis.public_references must be a list")
    for reference in references:
        if not isinstance(reference, dict):
            raise ReviewBundleError("basis public reference must be an object")
        _require_exact_keys(
            reference,
            {"repository", "revision", "tree_digest", "path_prefix"},
            "basis.public_references[]",
        )
        for field in ("repository", "revision", "path_prefix"):
            _nonempty_string(reference, field)
        digest = reference["tree_digest"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ReviewBundleError("basis public reference tree_digest is invalid")


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ReviewBundleError(f"{name} has an invalid field set")


def _nonempty_string(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ReviewBundleError(f"{field} must be a non-empty trimmed string")
    return item


def _timestamp(value: Mapping[str, Any], field: str) -> datetime:
    item = value.get(field)
    if not isinstance(item, str):
        raise ReviewBundleError(f"{field} must be an RFC3339 timestamp")
    return _parse_timestamp(item, field)


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewBundleError(f"{field} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewBundleError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
