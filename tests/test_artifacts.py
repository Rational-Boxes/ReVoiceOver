import json
import os
from pathlib import Path

import pytest

from narration import artifacts


def test_round_trip_and_envelope(tmp_path: Path):
    path = tmp_path / "x.json"
    artifacts.write_json(path, {"a": [1, 2]})
    assert artifacts.read_json(path) == {"a": [1, 2]}
    assert json.loads(path.read_text())["schema_version"] == artifacts.SCHEMA_VERSION
    assert not list(tmp_path.glob(".*.tmp"))


def test_schema_mismatch(tmp_path: Path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"schema_version": 99, "data": 1}))
    with pytest.raises(artifacts.SchemaError):
        artifacts.read_json(path)


def test_digest_changes_with_content_and_existence(tmp_path: Path):
    f = tmp_path / "in.txt"
    missing = artifacts.digest_files([f])
    f.write_text("one")
    first = artifacts.digest_files([f])
    assert first != missing
    f.write_text("two!")
    os.utime(f, ns=(1, 1))
    assert artifacts.digest_files([f]) != first


def test_manifest(tmp_path: Path):
    assert artifacts.read_manifest(tmp_path, "extract") is None
    artifacts.write_manifest(tmp_path, "extract", ok=False, inputs_digest="d", error="boom")
    m = artifacts.read_manifest(tmp_path, "extract")
    assert m is not None and m["ok"] is False and m["error"] == "boom"
