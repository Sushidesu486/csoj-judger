import json
import os
import re
import uuid
from pathlib import Path

from oj_checker.domain import RunManifest

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FileReportStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def write_manifest(self, manifest: RunManifest) -> Path:
        if not _SAFE_RUN_ID.fullmatch(manifest.run_id):
            raise ValueError("run_id must contain only letters, numbers, dot, underscore, or dash")

        run_dir = self._root / "runs" / manifest.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "manifest.json"
        temporary = run_dir / f".manifest.{os.getpid()}.{uuid.uuid4().hex}.tmp"

        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(manifest.to_dict(), file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        return target
