from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from narration import credentials


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """No real user config, no real token, no keyring access."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    for name in credentials.ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credentials, "keyring_token", lambda: None)
    monkeypatch.setattr(credentials, "dotenv_token", lambda: None)


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An 8 s test clip: tone, 1 s of silence, tone."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "demo.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=f=440:d=3.5",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=f=330:d=3.5",
         "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]",
         "-map", "0:v", "-map", "[a]", "-t", "8", "-c:a", "aac", "-c:v", "mpeg4",
         str(out)],
        check=True,
    )
    return out
