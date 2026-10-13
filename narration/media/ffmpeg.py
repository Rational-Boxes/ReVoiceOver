"""Thin, typed ffmpeg/ffprobe wrappers — the only module that shells out."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narration.context import Cancelled

# h264_videotoolbox is what the macOS build has (Apple's encoder, no extra libraries).
H264_ENCODERS = ("libx264", "libopenh264", "h264_videotoolbox", "h264_vaapi", "h264_nvenc",
                 "h264_qsv")


class FfmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    fps: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    audio_rate: int | None
    raw: dict[str, Any]


def bundled_dirs() -> list[Path]:
    """Where a packaged app keeps its own ffmpeg: ``$NARR_FFMPEG_DIR``, then
    ``ffmpeg/`` inside a PyInstaller bundle (macOS app, Windows install)."""
    dirs = []
    if env := os.environ.get("NARR_FFMPEG_DIR"):
        dirs.append(Path(env))
    if getattr(sys, "frozen", False):
        dirs.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ffmpeg")
    return dirs


@functools.cache
def _binary(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    for folder in bundled_dirs():
        if (folder / exe).is_file():
            return str(folder / exe)
    path = shutil.which(name)
    if path is None:
        raise FfmpegError(f"{name} not found on PATH; install ffmpeg")
    return path


# Text from ffmpeg is UTF-8 everywhere (not the Windows code page), and a
# windowed app must not flash a console for every call.
_TEXT: dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"}
if sys.platform == "win32":
    _TEXT["creationflags"] = subprocess.CREATE_NO_WINDOW


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    num, _, den = rate.partition("/")
    return float(num) / float(den or 1)


def probe(path: Path) -> MediaInfo:
    proc = subprocess.run(
        [_binary("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, **_TEXT,
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    raw = json.loads(proc.stdout)
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return MediaInfo(
        duration=float(raw.get("format", {}).get("duration", 0.0)),
        has_video=video is not None,
        has_audio=audio is not None,
        fps=_parse_rate(video.get("avg_frame_rate")) if video else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_rate=int(audio["sample_rate"]) if audio and "sample_rate" in audio else None,
        raw=raw,
    )


def run(
    args: list[str],
    *,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Run ffmpeg with ``args`` (without the binary name).

    ``on_progress`` receives the output position in seconds. Setting ``cancel``
    terminates ffmpeg and raises ``Cancelled``.
    """
    cmd = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, **_TEXT)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    proc.wait()
                    raise Cancelled()
                key, _, value = line.strip().partition("=")
                if on_progress and key == "out_time_us" and value.isdigit():
                    on_progress(int(value) / 1e6)
            proc.wait()
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        if proc.returncode != 0:
            err.seek(0)
            tail = "\n".join(err.read().strip().splitlines()[-15:])
            raise FfmpegError(f"ffmpeg exited with {proc.returncode}:\n{tail}")


def stderr_of(args: list[str]) -> str:
    """Run an analysis-only ffmpeg command and return its stderr."""
    proc = subprocess.run(
        [_binary("ffmpeg"), "-hide_banner", "-nostdin", *args],
        capture_output=True, **_TEXT,
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg failed: {proc.stderr.strip()[-2000:]}")
    return proc.stderr


def extract_audio(src: Path, dst: Path, *, rate: int, channels: int = 1,
                  cancel: threading.Event | None = None,
                  on_progress: Callable[[float], None] | None = None) -> None:
    run(["-i", str(src), "-vn", "-ac", str(channels), "-ar", str(rate),
         "-c:a", "pcm_s16le", str(dst)], cancel=cancel, on_progress=on_progress)


def cut_audio(src: Path, dst: Path, start: float, end: float,
              cancel: threading.Event | None = None) -> None:
    run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
         "-c:a", "pcm_s16le", str(dst)], cancel=cancel)


_SILENCE_RE = re.compile(r"silence_(start|end): (-?[\d.]+)")


def detect_silences(path: Path, *, noise_db: float, min_s: float) -> list[tuple[float, float]]:
    """Return (start, end) pairs of silent stretches."""
    text = stderr_of(["-i", str(path), "-af",
                      f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"])
    out: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(text):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            out.append((start, float(value)))
            start = None
    return out


@functools.cache
def available_encoders() -> frozenset[str]:
    proc = subprocess.run([_binary("ffmpeg"), "-hide_banner", "-encoders"],
                          capture_output=True, **_TEXT)
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def h264_encoder() -> str | None:
    """First usable H.264 encoder, preferring software ones."""
    encoders = available_encoders()
    return next((e for e in H264_ENCODERS if e in encoders), None)
