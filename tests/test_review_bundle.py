import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from oj_checker.review_bundle import (
    ReviewBundleError,
    submission_from_review_bundle,
    verify_review_bundle,
)

KEY_ID = "plat101-review-2026-01"
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(SEED)
PUBLIC_KEY = PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
NOW = datetime(2026, 8, 30, 2, 5, tzinfo=UTC)


def payload() -> dict[str, Any]:
    return {
        "schema_version": "review-bundle-v1",
        "audience": "oj-checker",
        "submission": {
            "id": "00000000-0000-4000-8000-000000000001",
            "owner": "student",
            "lab_id": "lab4-gpu",
            "score": 100,
            "input_digest": "1" * 64,
            "submitted_at": "2026-08-30T01:02:03Z",
            "input_manifest": {
                "files": [
                    {
                        "path": "src/main.cu",
                        "size": 17,
                        "sha256": "2" * 64,
                    }
                ]
            },
            "lab_definition": {"spec": {"submission": {"allow": ["src/**"]}}},
            "active_run": {
                "id": 42,
                "state": "Success",
                "result_info": {"steps": [{"id": "score", "state": "Succeeded"}]},
                "score": 100,
                "performance": 12.5,
                "finished_at": "2026-08-30T01:04:03Z",
            },
        },
        "basis": {
            "commit": "a" * 40,
            "tree_digest": "b" * 64,
            "document_digest": "c" * 64,
            "source_path": "src/lab4",
            "document_path": "docs/lab4.md",
            "public_references": [
                {
                    "repository": "ZJUSCT/NR-amssncku",
                    "revision": "d" * 40,
                    "tree_digest": "e" * 64,
                    "path_prefix": "public/0",
                }
            ],
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


def sign(value: dict[str, Any], *, key_id: str = KEY_ID) -> dict[str, str]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "payload": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "key_id": key_id,
        "signature": base64.urlsafe_b64encode(PRIVATE_KEY.sign(raw)).rstrip(b"=").decode(),
    }


def test_verifies_signed_payload_before_parsing() -> None:
    expected = payload()

    verified = verify_review_bundle(sign(expected), {KEY_ID: PUBLIC_KEY}, now=NOW)

    assert verified.payload == expected
    assert verified.key_id == KEY_ID


def test_accepts_standard_ed25519_public_key_pem() -> None:
    pem = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    verified = verify_review_bundle(sign(payload()), {KEY_ID: pem.decode()}, now=NOW)

    assert verified.payload_digest


def test_verifies_go_generated_golden_fixture() -> None:
    envelope = Path("tests/fixtures/review_bundle_v1.json").read_bytes()

    verified = verify_review_bundle(envelope, {KEY_ID: PUBLIC_KEY}, now=NOW)

    assert verified.payload == payload()

    submission = submission_from_review_bundle(verified)
    assert submission.id == "00000000-0000-4000-8000-000000000001"
    assert submission.lab_id == "lab4-gpu"
    assert submission.active_run_id == 42
    assert submission.run_state == "Success"
    assert submission.run_finished_at == datetime(2026, 8, 30, 1, 4, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    "field, replacement",
    [
        (("submission", "id"), "00000000-0000-4000-8000-000000000002"),
        (("model",), "glm-5.3"),
        (("submission", "input_manifest"), {"files": []}),
        (("expires_at",), "2026-08-30T03:15:00Z"),
        (("audience",), "another-service"),
    ],
)
def test_rejects_payload_tampering_without_a_new_signature(
    field: tuple[str, ...],
    replacement: object,
) -> None:
    envelope = sign(payload())
    raw = json.loads(base64.urlsafe_b64decode(envelope["payload"] + "=="))
    target = raw
    for part in field[:-1]:
        target = target[part]
    target[field[-1]] = replacement
    envelope["payload"] = (
        base64.urlsafe_b64encode(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(ReviewBundleError, match="signature"):
        verify_review_bundle(envelope, {KEY_ID: PUBLIC_KEY}, now=NOW)


def test_rejects_unknown_key_expiry_audience_and_nightly_model() -> None:
    with pytest.raises(ReviewBundleError, match="unknown signing key"):
        verify_review_bundle(sign(payload(), key_id="unknown"), {KEY_ID: PUBLIC_KEY}, now=NOW)

    expired = payload()
    expired["issued_at"] = "2026-08-30T01:00:00Z"
    expired["expires_at"] = "2026-08-30T01:15:00Z"
    with pytest.raises(ReviewBundleError, match="validity window"):
        verify_review_bundle(sign(expired), {KEY_ID: PUBLIC_KEY}, now=NOW)

    wrong_audience = payload()
    wrong_audience["audience"] = "another-service"
    with pytest.raises(ReviewBundleError, match="audience"):
        verify_review_bundle(sign(wrong_audience), {KEY_ID: PUBLIC_KEY}, now=NOW)

    nightly = payload()
    nightly["source"] = "nightly"
    with pytest.raises(ReviewBundleError, match=r"glm-5\.3"):
        verify_review_bundle(sign(nightly), {KEY_ID: PUBLIC_KEY}, now=NOW)


def test_rejects_noncanonical_or_incomplete_submission_metadata() -> None:
    invalid_uuid = payload()
    invalid_uuid["submission"]["id"] = "{00000000-0000-4000-8000-000000000001}"
    with pytest.raises(ReviewBundleError, match="canonical UUID"):
        verify_review_bundle(sign(invalid_uuid), {KEY_ID: PUBLIC_KEY}, now=NOW)

    unsuccessful = deepcopy(payload())
    unsuccessful["submission"]["active_run"]["state"] = "Failed"
    with pytest.raises(ReviewBundleError, match="successful"):
        verify_review_bundle(sign(unsuccessful), {KEY_ID: PUBLIC_KEY}, now=NOW)

    missing_definition = payload()
    del missing_definition["submission"]["lab_definition"]
    with pytest.raises(ReviewBundleError, match="field set"):
        verify_review_bundle(sign(missing_definition), {KEY_ID: PUBLIC_KEY}, now=NOW)


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        verify_review_bundle(
            sign(payload()),
            {KEY_ID: PUBLIC_KEY},
            now=datetime(2026, 8, 30, 2, 5),
        )


def test_rejects_future_bundle() -> None:
    with pytest.raises(ReviewBundleError, match="validity window"):
        verify_review_bundle(
            sign(payload()),
            {KEY_ID: PUBLIC_KEY},
            now=NOW - timedelta(minutes=10),
        )


def test_rejects_excessively_long_validity_window() -> None:
    long_lived = payload()
    long_lived["expires_at"] = "2026-08-30T02:15:01Z"

    with pytest.raises(ReviewBundleError, match="exceeds 15 minutes"):
        verify_review_bundle(sign(long_lived), {KEY_ID: PUBLIC_KEY}, now=NOW)
