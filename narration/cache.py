"""Content-addressed file cache inside the work dir (shared by transcribe, synth, qa).

Entries live at ``<work_dir>/cache/<namespace>/<key><suffix>``. The key is a
sha256 over everything that determines the result, so a re-run with identical
inputs never re-bills an API call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from narration.artifacts import atomic_write_bytes


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def make_key(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class FileCache:
    def __init__(self, work_dir: Path, namespace: str) -> None:
        self.root = work_dir / "cache" / namespace

    def path(self, key: str, suffix: str) -> Path:
        return self.root / f"{key}{suffix}"

    def get_bytes(self, key: str, suffix: str) -> bytes | None:
        try:
            return self.path(key, suffix).read_bytes()
        except FileNotFoundError:
            return None

    def put_bytes(self, key: str, suffix: str, data: bytes) -> Path:
        path = self.path(key, suffix)
        atomic_write_bytes(path, data)
        return path

    def get_json(self, key: str) -> Any | None:
        data = self.get_bytes(key, ".json")
        return None if data is None else json.loads(data)

    def put_json(self, key: str, value: Any) -> Path:
        return self.put_bytes(key, ".json", json.dumps(value, ensure_ascii=False).encode())
