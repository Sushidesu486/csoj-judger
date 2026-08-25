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
        payload = (
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()

        try:
            with temporary.open("xb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != payload:
                    raise FileExistsError(
                        f"run_id {manifest.run_id!r} already has a different manifest"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)

        return target
