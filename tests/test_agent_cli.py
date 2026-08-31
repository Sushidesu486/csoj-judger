from pathlib import Path

import pytest

from oj_checker import cli


def test_agent_workspace_command_does_not_require_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DBURL", raising=False)
    monkeypatch.setattr(
        cli,
        "_agent_review_workspace",
        lambda _args: {"status": "ok"},
    )

    assert cli.main(["agent-review-workspace", "--workspace", str(tmp_path)]) == 0
    assert '"status": "ok"' in capsys.readouterr().out


def test_bundle_workspace_command_does_not_require_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DBURL", raising=False)
    monkeypatch.setattr(
        cli,
        "_prepare_agent_workspace_bundle",
        lambda _args: {"status": "ok"},
    )
    bundle = tmp_path / "bundle.json"
    key = tmp_path / "public.key"

    assert (
        cli.main(
            [
                "prepare-agent-workspace-bundle",
                "--bundle",
                str(bundle),
                "--public-key-file",
                str(key),
                "--workspace",
                str(tmp_path / "workspace"),
            ]
        )
        == 0
    )
    assert '"status": "ok"' in capsys.readouterr().out


def test_agent_report_api_does_not_require_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("DBURL", raising=False)
    monkeypatch.setattr(cli, "_agent_report_api", lambda _args: None)

    assert (
        cli.main(
            [
                "agent-report-api",
                "--public-key-file",
                str(tmp_path / "public.key"),
            ]
        )
        == 0
    )


def test_context_reader_rejects_nested_or_symlinked_files(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    (context / "policy.md").write_text("可信规则\n", encoding="utf-8")
    (context / "alias.md").symlink_to(context / "policy.md")

    assert cli._read_context_text(context, "policy.md", required=True) == "可信规则\n"
    with pytest.raises(ValueError, match="basename"):
        cli._read_context_text(context, "../policy.md", required=True)
    with pytest.raises(ValueError, match="symlink"):
        cli._read_context_text(context, "alias.md", required=True)
