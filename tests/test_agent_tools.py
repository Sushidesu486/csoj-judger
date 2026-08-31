import os
from pathlib import Path

import pytest

from oj_checker.agent_tools import AgentWorkspace, ReadOnlyToolBroker, ToolError


def workspace(tmp_path: Path) -> AgentWorkspace:
    submission = tmp_path / "submission"
    baseline = tmp_path / "baseline"
    context = tmp_path / "context"
    for root in (submission, baseline, context):
        root.mkdir()
    (submission / "src").mkdir()
    (baseline / "src").mkdir()
    (submission / "src/main.cpp").write_text(
        "int main() {\n  run_required();\n  return 0;\n}\n",
        encoding="utf-8",
    )
    (baseline / "src/main.cpp").write_text(
        "int main() {\n  baseline_required();\n  return 0;\n}\n",
        encoding="utf-8",
    )
    (context / "lab-policy.md").write_text("必须保留 required 计算。\n", encoding="utf-8")
    return AgentWorkspace(submission, baseline, context)


def test_read_only_tools_navigate_search_read_and_diff(tmp_path: Path) -> None:
    broker = ReadOnlyToolBroker(workspace(tmp_path), max_response_bytes=4096)

    inventory = broker.inventory()
    assert inventory["roots"]["submission"]["file_count"] == 1
    tree = broker.call("list_tree", {"root": "submission", "path": ".", "depth": 3})
    assert {entry["path"] for entry in tree.payload["entries"]} == {"src", "src/main.cpp"}
    search = broker.call(
        "search",
        {"root": "submission", "path": ".", "query": "run_required"},
    )
    assert search.payload["matches"][0]["line"] == 2
    lines = broker.call(
        "read_lines",
        {"root": "submission", "path": "src/main.cpp", "start_line": 2, "end_line": 3},
    )
    assert [line["line"] for line in lines.payload["lines"]] == [2, 3]
    diff = broker.call(
        "compare_file",
        {"submission_path": "src/main.cpp", "baseline_path": "src/main.cpp"},
    )
    assert diff.payload["identical"] is False
    assert any("run_required" in line for line in diff.payload["diff_lines"])
    assert all(observation.bytes_returned <= 4096 for observation in (tree, search, lines, diff))


def test_tools_reject_traversal_symlinks_binary_and_invalid_evidence(tmp_path: Path) -> None:
    roots = workspace(tmp_path)
    broker = ReadOnlyToolBroker(roots)
    (roots.submission / "outside").symlink_to(tmp_path / "outside")
    (roots.submission / "binary.bin").write_bytes(b"a\0b")

    with pytest.raises(ToolError, match="unsafe relative path"):
        broker.call(
            "read_lines",
            {"root": "submission", "path": "../secret", "start_line": 1, "end_line": 1},
        )
    with pytest.raises(ToolError, match="symlinks are not allowed"):
        broker.call("file_info", {"root": "submission", "path": "outside"})
    with pytest.raises(ToolError, match="binary"):
        broker.call(
            "read_lines",
            {"root": "submission", "path": "binary.bin", "start_line": 1, "end_line": 1},
        )

    assert (
        broker.validate_evidence(
            {"root": "submission", "path": "src/main.cpp", "start_line": 2, "end_line": 2}
        )
        is None
    )
    assert "exceeds" in str(
        broker.validate_evidence(
            {"root": "submission", "path": "src/main.cpp", "start_line": 2, "end_line": 99}
        )
    )


def test_tool_output_uses_cursor_instead_of_unbounded_response(tmp_path: Path) -> None:
    roots = workspace(tmp_path)
    for index in range(20):
        (roots.submission / f"file-{index:02}.cpp").write_text(
            f"needle {index} " + "x" * 200 + "\n",
            encoding="utf-8",
        )
    broker = ReadOnlyToolBroker(
        roots,
        max_response_bytes=700,
        max_tree_entries=100,
        max_search_matches=100,
    )
    result = broker.call("search", {"root": "submission", "query": "needle"})

    assert result.payload["truncated"] is True
    assert isinstance(result.payload["next_cursor"], int)
    assert result.bytes_returned <= 700


def test_empty_optional_directory_path_means_workspace_root(tmp_path: Path) -> None:
    broker = ReadOnlyToolBroker(workspace(tmp_path))

    result = broker.call("list_tree", {"root": "submission", "path": "", "depth": 1})

    assert result.payload["path"] == "."
    assert result.payload["entries"]


def test_search_accepts_one_file_path_without_walking_a_directory(tmp_path: Path) -> None:
    broker = ReadOnlyToolBroker(workspace(tmp_path))

    result = broker.call(
        "search",
        {"root": "submission", "path": "src/main.cpp", "query": "run_required"},
    )

    assert result.payload["matches"][0]["path"] == "src/main.cpp"
    assert result.paths == ("submission/src/main.cpp",)


def test_compare_large_file_streams_a_bounded_diff(tmp_path: Path) -> None:
    roots = workspace(tmp_path)
    baseline_lines = [f"common line {index}\n" for index in range(100_000)]
    submission_lines = list(baseline_lines)
    submission_lines[50_000] = "changed computation\n"
    (roots.baseline / "large.cpp").write_text("".join(baseline_lines), encoding="utf-8")
    (roots.submission / "large.cpp").write_text("".join(submission_lines), encoding="utf-8")
    broker = ReadOnlyToolBroker(roots, max_response_bytes=4096, max_diff_lines=20)

    result = broker.call("compare_file", {"submission_path": "large.cpp"})

    assert result.payload["identical"] is False
    assert any("changed computation" in line for line in result.payload["diff_lines"])
    assert result.bytes_returned <= 4096


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is unavailable")
def test_workspace_root_must_not_be_a_symlink(tmp_path: Path) -> None:
    roots = workspace(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(roots.submission, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        AgentWorkspace(alias, roots.baseline, roots.context)
