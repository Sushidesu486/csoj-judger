import os
import uuid
from pathlib import Path


def write_create_only(target: Path, payload: bytes, *, conflict_message: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise FileExistsError(conflict_message) from None
    finally:
        temporary.unlink(missing_ok=True)
    return target
