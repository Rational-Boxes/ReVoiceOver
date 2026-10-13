"""Stage I/O: JSON envelopes with a schema version, atomic writes, run manifests.

Every JSON artifact is stored as::

    {"schema_version": 1, "written": "<iso time>", "data": ...}

Each stage also leaves a manifest in ``.state/<stage>.json`` recording the
digest of its inputs at the time it ran; comparing that with the current digest
is how a stage is detected as stale.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_DIR = ".state"


class SchemaError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, data: Any) -> None:
    envelope = {"schema_version": SCHEMA_VERSION, "written": _now(), "data": data}
    atomic_write_text(path, json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        envelope = json.load(fh)
    version = envelope.get("schema_version") if isinstance(envelope, dict) else None
    if version != SCHEMA_VERSION:
        raise SchemaError(f"{path}: schema_version {version!r}, expected {SCHEMA_VERSION}")
    return envelope["data"]


def digest_files(paths: list[Path]) -> str:
    """Cheap change detector: name, size and mtime of each input.

    A missing input contributes a fixed marker, so creating it later changes
    the digest.
    """
    h = hashlib.sha256()
    for path in paths:
        h.update(str(path.name).encode())
        try:
            st = path.stat()
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
        except FileNotFoundError:
            h.update(b"<missing>")
    return h.hexdigest()


def manifest_path(work_dir: Path, stage: str) -> Path:
    return work_dir / STATE_DIR / f"{stage}.json"


def write_manifest(work_dir: Path, stage: str, *, ok: bool, inputs_digest: str,
                   error: str | None = None) -> None:
    write_json(
        manifest_path(work_dir, stage),
        {"ok": ok, "inputs_digest": inputs_digest, "error": error, "finished": _now()},
    )


def read_manifest(work_dir: Path, stage: str) -> dict[str, Any] | None:
    path = manifest_path(work_dir, stage)
    try:
        data = read_json(path)
    except (FileNotFoundError, SchemaError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
