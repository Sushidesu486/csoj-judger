import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class UnsupportedLabError(ValueError):
    """A lab has no configured HPC101 source and document mapping."""


@dataclass(frozen=True, slots=True)
class BaselineFile:
    path: str
    content: bytes
    sha256: str

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ReviewBasis:
    lab_id: str
    upstream_commit: str
    source_path: str
    document_path: str
    files: tuple[BaselineFile, ...]
    tree_digest: str
    document: str
    document_digest: str

    def file_text(self, path: str) -> str:
        for file in self.files:
            if file.path == path:
                return file.text()
        raise KeyError(path)


class ReviewBasisProvider(Protocol):
    @property
    def upstream_commit(self) -> str: ...

    def load(self, lab_id: str) -> ReviewBasis: ...


_LAB_PATHS = {
    "lab2": ("src/lab2", "docs/lab/Lab2-Vectorization/index.md"),
    "lab2-riscv": ("src/lab2", "docs/lab/Lab2-Vectorization/index.md"),
    "lab3": ("src/lab3", "docs/lab/Lab3-GDN-Prefill/index.md"),
    "lab3p5": ("src/lab3p5", "docs/lab/Lab3.5-AscendC-Op/index.md"),
    "lab4-cpu": ("src/lab4", "docs/lab/Lab4-AMSS-NCKU/index.md"),
    "lab4-gpu": ("src/lab4", "docs/lab/Lab4-AMSS-NCKU/index.md"),
    "lab4p5": ("src/lab4p5", "docs/lab/Lab4.5-INT8-FP64-GEMM/index.md"),
    "lab5": ("src/lab5", "docs/lab/Lab5-Gemma4/index.md"),
}


class GitReviewBasisProvider:
    def __init__(self, repository: str | Path, revision: str) -> None:
        self._repository = Path(repository)
        self._commit = self._git_text("rev-parse", "--verify", f"{revision}^{{commit}}")

    @property
    def upstream_commit(self) -> str:
        return self._commit

    def load(self, lab_id: str) -> ReviewBasis:
        try:
            source_path, document_path = _LAB_PATHS[lab_id]
        except KeyError:
            raise UnsupportedLabError(f"no review basis configured for lab {lab_id!r}") from None

        prefix = f"{source_path}/"
        repository_paths = tuple(
            path
            for path in self._git_bytes(
                "ls-tree", "-r", "-z", "--name-only", self._commit, "--", source_path
            )
            .decode("utf-8")
            .split("\0")
            if path
        )
        if not repository_paths:
            raise FileNotFoundError(f"baseline source path is missing: {source_path}")

        files = []
        tree_hasher = hashlib.sha256()
        for repository_path in repository_paths:
            relative_path = repository_path.removeprefix(prefix)
            content = self._git_bytes("show", f"{self._commit}:{repository_path}")
            content_digest = hashlib.sha256(content).hexdigest()
            tree_hasher.update(relative_path.encode())
            tree_hasher.update(b"\0")
            tree_hasher.update(bytes.fromhex(content_digest))
            files.append(BaselineFile(relative_path, content, content_digest))

        document_bytes = self._git_bytes("show", f"{self._commit}:{document_path}")
        return ReviewBasis(
            lab_id=lab_id,
            upstream_commit=self._commit,
            source_path=source_path,
            document_path=document_path,
            files=tuple(files),
            tree_digest=tree_hasher.hexdigest(),
            document=document_bytes.decode("utf-8", errors="replace"),
            document_digest=hashlib.sha256(document_bytes).hexdigest(),
        )

    def _git_text(self, *arguments: str) -> str:
        return self._git_bytes(*arguments).decode("utf-8").strip()

    def _git_bytes(self, *arguments: str) -> bytes:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self._repository,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
        return result.stdout
