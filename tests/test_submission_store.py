from datetime import UTC, datetime

import pytest

from oj_checker.domain import Submission
from oj_checker.submission_store import NfsSubmissionStore, SourcePolicy, UnsafeSubmissionPath


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/passwd",
        "../escape.cpp",
        "student/../../escape.cpp",
        "student//kernel.cpp",
        "student/./kernel.cpp",
        r"student\kernel.cpp",
        "student/kernel.cpp\x00ignored",
    ],
)
def test_load_bundle_rejects_unsafe_manifest_paths(tmp_path, unsafe_path: str) -> None:
    store = NfsSubmissionStore(tmp_path)
    submission = make_submission([{"path": unsafe_path, "size": 1}])

    with pytest.raises(UnsafeSubmissionPath):
        store.load_bundle(submission, SourcePolicy())


@pytest.mark.parametrize("escape_kind", ["directory", "file"])
def test_load_bundle_rejects_symlink_escape(tmp_path, escape_kind: str) -> None:
    submission = make_submission([{"path": "student/kernel.cpp", "size": 12}])
    input_root = tmp_path / "submissions" / submission.id / "input"
    input_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kernel.cpp").write_text("stolen code")

    if escape_kind == "directory":
        (input_root / "student").symlink_to(outside, target_is_directory=True)
    else:
        (input_root / "student").mkdir()
        (input_root / "student" / "kernel.cpp").symlink_to(outside / "kernel.cpp")

    with pytest.raises(UnsafeSubmissionPath):
        NfsSubmissionStore(tmp_path).load_bundle(submission, SourcePolicy())


@pytest.mark.parametrize("symlink_level", ["submissions", "submission", "input"])
def test_load_bundle_rejects_symlinks_in_submission_root_chain(
    tmp_path, symlink_level: str
) -> None:
    submission = make_submission([{"path": "student/kernel.cpp", "size": 4}])
    oj_root = tmp_path / "oj"
    oj_root.mkdir()
    outside = tmp_path / "outside"

    if symlink_level == "submissions":
        target_input = outside / submission.id / "input"
        (oj_root / "submissions").symlink_to(outside, target_is_directory=True)
    elif symlink_level == "submission":
        (oj_root / "submissions").mkdir()
        target_input = outside / "input"
        (oj_root / "submissions" / submission.id).symlink_to(
            outside, target_is_directory=True
        )
    else:
        (oj_root / "submissions" / submission.id).mkdir(parents=True)
        target_input = outside
        (oj_root / "submissions" / submission.id / "input").symlink_to(
            outside, target_is_directory=True
        )

    (target_input / "student").mkdir(parents=True)
    (target_input / "student" / "kernel.cpp").write_text("code")

    with pytest.raises(UnsafeSubmissionPath):
        NfsSubmissionStore(oj_root).load_bundle(submission, SourcePolicy())


def test_load_bundle_keeps_multifile_metadata_when_content_budget_is_exhausted(tmp_path) -> None:
    files = [
        {"path": "CMakeLists.txt", "size": 4},
        {"path": "compile.sh", "size": 6},
        {"path": "src/a_kernel.cu", "size": 20},
        {"path": "src/z_tail.cpp", "size": 4},
        {"path": "README.txt", "size": 7},
    ]
    submission = make_submission(files)
    input_root = tmp_path / "submissions" / submission.id / "input"
    (input_root / "src").mkdir(parents=True)
    (input_root / "CMakeLists.txt").write_text("cmak")
    (input_root / "compile.sh").write_text("build!")
    (input_root / "src" / "a_kernel.cu").write_text("abcdefghijklmnopqrst")
    (input_root / "src" / "z_tail.cpp").write_text("tail")
    (input_root / "README.txt").write_text("ignored")

    bundle = NfsSubmissionStore(tmp_path).load_bundle(
        submission,
        SourcePolicy(max_file_bytes=8, max_total_bytes=18),
    )

    assert [file.path for file in bundle.files] == [
        "CMakeLists.txt",
        "compile.sh",
        "src/a_kernel.cu",
        "src/z_tail.cpp",
    ]
    assert bundle.total_bytes_read == 18
    assert bundle.files[2].content == "abcdefgh"
    assert bundle.files[2].omission_reason == "file_budget"
    assert bundle.files[3].content == ""
    assert bundle.files[3].omission_reason == "total_budget"


def test_load_bundle_uses_frozen_lab_file_rules(tmp_path) -> None:
    files = [
        {"path": ".env", "size": 9},
        {"path": "datasets/calibration.jsonl", "size": 5},
        {"path": "student/entry.custom", "size": 5},
        {"path": "assets/blob.bin", "size": 4},
    ]
    lab_definition = {
        "spec": {
            "submissions": {
                "home": {
                    "allow": [".env", "datasets/*.jsonl", "src/**"],
                    "required": ["student/*"],
                }
            }
        }
    }
    submission = make_submission(files, lab_definition=lab_definition)
    input_root = tmp_path / "submissions" / submission.id / "input"
    (input_root / "datasets").mkdir(parents=True)
    (input_root / "student").mkdir()
    (input_root / "assets").mkdir()
    (input_root / ".env").write_text("TOKEN=abc")
    (input_root / "datasets" / "calibration.jsonl").write_text("data\n")
    (input_root / "student" / "entry.custom").write_text("entry")
    (input_root / "assets" / "blob.bin").write_bytes(b"blob")

    bundle = NfsSubmissionStore(tmp_path).load_bundle(submission, SourcePolicy())

    assert [file.path for file in bundle.files] == [
        ".env",
        "datasets/calibration.jsonl",
        "student/entry.custom",
    ]


def test_load_bundle_rejects_non_regular_files(tmp_path) -> None:
    submission = make_submission([{"path": "student/kernel.cpp", "size": 0}])
    input_root = tmp_path / "submissions" / submission.id / "input" / "student"
    input_root.mkdir(parents=True)
    (input_root / "kernel.cpp").mkdir()

    with pytest.raises(UnsafeSubmissionPath, match="regular file"):
        NfsSubmissionStore(tmp_path).load_bundle(submission, SourcePolicy())


def make_submission(
    files: list[dict[str, object]],
    *,
    lab_definition: dict[str, object] | None = None,
) -> Submission:
    return Submission(
        id="11111111-1111-4111-8111-111111111111",
        owner="alice",
        lab_id="lab4-gpu",
        score=90,
        input_digest="digest",
        submitted_at=datetime(2026, 8, 25, tzinfo=UTC),
        input_manifest={"files": files},
        lab_definition=lab_definition or {},
    )
