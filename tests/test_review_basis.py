import subprocess

import pytest

from oj_checker.review_basis import GitReviewBasisProvider, UnsupportedLabError


def test_provider_reads_baseline_and_document_from_pinned_commit(tmp_path) -> None:
    repository = make_repository(
        tmp_path,
        {
            "src/lab4/run.sh": "echo baseline\n",
            "src/lab4/src/kernel.C": "void evolve() {}\n",
            "docs/lab/Lab4-AMSS-NCKU/index.md": "# Lab4\nDo not skip TwoPuncture.\n",
        },
    )
    commit = git(repository, "rev-parse", "HEAD")
    (repository / "src/lab4/run.sh").write_text("echo dirty worktree\n")

    basis = GitReviewBasisProvider(repository, commit).load("lab4-cpu")

    assert basis.lab_id == "lab4-cpu"
    assert basis.upstream_commit == commit
    assert basis.source_path == "src/lab4"
    assert basis.document_path == "docs/lab/Lab4-AMSS-NCKU/index.md"
    assert basis.file_text("run.sh") == "echo baseline\n"
    assert basis.file_text("src/kernel.C") == "void evolve() {}\n"
    assert basis.document == "# Lab4\nDo not skip TwoPuncture.\n"
    assert len(basis.tree_digest) == 64
    assert len(basis.document_digest) == 64


def test_provider_uses_shared_source_for_lab_aliases(tmp_path) -> None:
    repository = make_repository(
        tmp_path,
        {
            "src/lab2/student/moe_opt.cpp": "void moe() {}\n",
            "docs/lab/Lab2-Vectorization/index.md": "# Lab2\n",
        },
    )
    provider = GitReviewBasisProvider(repository, "HEAD")

    x86 = provider.load("lab2")
    riscv = provider.load("lab2-riscv")

    assert x86.lab_id == "lab2"
    assert riscv.lab_id == "lab2-riscv"
    assert riscv.source_path == x86.source_path == "src/lab2"
    assert riscv.tree_digest == x86.tree_digest
    assert riscv.document_digest == x86.document_digest


def test_provider_rejects_labs_without_a_review_basis(tmp_path) -> None:
    repository = make_repository(tmp_path, {"README.md": "empty\n"})

    with pytest.raises(UnsupportedLabError, match="hello-world"):
        GitReviewBasisProvider(repository, "HEAD").load("hello-world")


def make_repository(tmp_path, files: dict[str, str]):
    repository = tmp_path / "HPC101"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.email", "tests@example.com")
    git(repository, "config", "user.name", "Tests")
    for relative, content in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "fixture")
    return repository


def git(repository, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
