from pathlib import Path
from typing import Any

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


def test_agent_report_api_configures_reconcile_intervals_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    intervals: dict[str, float] = {}

    class Service:
        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    def agent_service(*_args: Any, **kwargs: Any) -> Service:
        intervals["agent"] = kwargs["reconcile_interval_seconds"]
        return Service()

    def plagiarism_service(*_args: Any, **kwargs: Any) -> Service:
        intervals["plagiarism"] = kwargs["reconcile_interval_seconds"]
        return Service()

    monkeypatch.setenv("LLM_API_KEY", "token")
    monkeypatch.setattr(cli, "_read_review_bundle_public_key", lambda _path: b"x" * 32)
    monkeypatch.setattr(cli, "LocalAgentRunExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "OpenAICompatibleToolChatClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "FileAgentRunService", agent_service)
    monkeypatch.setattr(cli, "LocalPlagiarismRunExecutor", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "OpenAICompatibleReviewer", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "OpenAIStreamingChatClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "FilePlagiarismRunService", plagiarism_service)
    monkeypatch.setattr(cli, "FileComplianceReportReader", lambda _path: object())
    monkeypatch.setattr(cli, "FilePlagiarismReportReader", lambda _path: object())
    monkeypatch.setattr(cli, "ComplianceApi", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "serve_report_api", lambda *_args, **_kwargs: None)
    args = cli._parser().parse_args(
        [
            "agent-report-api",
            "--api-token",
            "token",
            "--public-key-file",
            str(tmp_path / "public.key"),
            "--work-root",
            str(tmp_path / "work"),
            "--reconcile-interval",
            "0",
            "--plagiarism-reconcile-interval",
            "60",
        ]
    )

    cli._agent_report_api(args)

    assert intervals == {"agent": 0, "plagiarism": 60}


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
