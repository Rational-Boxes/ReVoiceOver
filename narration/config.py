"""Layered configuration.

Layers, lowest to highest precedence:

1. built-in defaults (the pydantic models below)
2. user config:        ~/.config/narr/config.toml
3. project config:     narr.toml next to the source video
4. per-video config:   <work_dir>/narr.toml (written by the GUI)

The API token is *not* configuration; see ``credentials.py``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

CONFIG_NAME = "narr.toml"


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "narr"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Models(_Section):
    asr_timestamped: str = "openai/whisper-timestamped-medium"
    # Optional higher-accuracy text pass (e.g. "openai/whisper-large-v3-turbo"),
    # reconciled with the timestamped words. "" disables it; not wired up yet.
    asr_text: str = ""
    llm: str = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    tts: str = "Zyphra/Zonos-v0.1-hybrid"
    tts_alternate: str = "Audio8/Audio8-TTS-Preview-0.6b"
    # QA re-transcription of synthesized clips: plain text is enough, and turbo
    # is billed per input second rather than by runtime.
    asr_qa: str = "openai/whisper-large-v3-turbo"


class Prices(_Section):
    """USD per 1M characters, used only for the GUI cost estimate."""

    tts_per_mchar: dict[str, float] = {
        "Zyphra/Zonos-v0.1-hybrid": 7.0,
        "Audio8/Audio8-TTS-Preview-0.6b": 5.0,
    }


class Network(_Section):
    concurrency: int = Field(6, ge=1, le=32)
    max_retries: int = Field(5, ge=0)
    timeout_s: float = Field(120.0, gt=0)


class Extract(_Section):
    chunk_s: float = Field(600.0, gt=30, description="target ASR chunk length")
    chunk_search_s: float = Field(60.0, ge=0, description="look this far for a silence")
    silence_db: float = -35.0
    silence_min_s: float = 0.4


class Transcribe(_Section):
    language: str = Field("", description='ISO 639-1 code, "" = auto-detect')
    glossary_prompt: bool = Field(True, description="prime Whisper with the glossary terms")
    timeout_s: float = Field(900.0, gt=0, description="per-chunk request timeout")


class Segment(_Section):
    pause_split_s: float = 0.6
    max_unit_s: float = 20.0
    min_unit_words: int = Field(3, ge=1, description="shorter units merge into the next one")
    merge_gap_s: float = Field(1.0, ge=0, description="…if the pause before it is shorter")


class Clean(_Section):
    batch_size: int = Field(12, ge=1, description="units per request; batches run concurrently")
    temperature: float = Field(0.1, ge=0, le=2)
    glossary: str = "glossary.txt"
    pronunciations: str = "pronunciations.toml"


class Voice(_Section):
    ref: str = Field("", description="reference clip to clone; empty = pick one automatically")
    ref_span: list[float] | None = Field(None, description="[start, end] in the source video")
    min_ref_s: float = 8.0
    max_ref_s: float = 15.0
    max_gap_s: float = Field(1.0, description="longest pause allowed inside the reference")


class Synth(_Section):
    loudness_lufs: float = -16.0
    sample_rate: int = 44100
    language: str = "en-us"
    # Units are synthesized in speech groups (whole sentences/passages) so prosody and
    # loudness flow; a group breaks at pauses longer than these, or at group_max_s.
    group_sentence_gap_s: float = Field(0.8, ge=0, description="max pause between sentences")
    group_fragment_gap_s: float = Field(3.0, ge=0, description="max pause inside a sentence")
    group_max_s: float = Field(25.0, gt=0)
    speaker_rate: float | None = Field(None, ge=5, le=35, description="Zonos rate (None: default)")
    seed: int = 42


class Fit(_Section):
    timing: Literal["anchor", "compact"] = "anchor"
    max_atempo: float = Field(1.15, ge=1.0, le=2.0, description="most a clip is sped up")
    compact_gap_s: float = 0.35
    trim_db: float = Field(-50.0, description="silence threshold for trimming clip edges")


class Assemble(_Section):
    audio_bitrate: str = "192k"
    video_bitrate: str = Field("", description='e.g. "6M"; "" = match the source')


class QA(_Section):
    max_wer: float = 0.15
    max_retries: int = 3
    max_silence_s: float = Field(1.5, description="longest silence allowed inside a clip")
    silence_db: float = -45.0
    # Zonos sometimes adds noises (squeaks, breaths, mumbles) before, between or after
    # sentences. Anything audible outside the words that match the script is muted.
    mute_noise: bool = True
    noise_db: float = Field(-40.0, description="quieter than this is not a noise")
    noise_pad_before_s: float = Field(0.1, ge=0, description="kept before each word")
    noise_pad_after_s: float = Field(0.25, ge=0, description="kept after each word")


class Config(_Section):
    models: Models = Models()
    prices: Prices = Prices()
    network: Network = Network()
    extract: Extract = Extract()
    transcribe: Transcribe = Transcribe()
    segment: Segment = Segment()
    clean: Clean = Clean()
    voice: Voice = Voice()
    synth: Synth = Synth()
    fit: Fit = Fit()
    assemble: Assemble = Assemble()
    qa: QA = QA()


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def config_layers(video: Path | None, work_dir: Path | None) -> list[Path]:
    """Config files in increasing precedence (missing files included)."""
    layers = [user_config_dir() / "config.toml"]
    if video is not None:
        layers.append(video.resolve().parent / CONFIG_NAME)
    if work_dir is not None:
        layers.append(work_dir / CONFIG_NAME)
    return layers


def load_config(video: Path | None = None, work_dir: Path | None = None) -> Config:
    merged: dict[str, Any] = {}
    for path in config_layers(video, work_dir):
        merged = _deep_merge(merged, _read_toml(path))
    return Config.model_validate(merged)


def _diff(values: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            sub = _diff(value, base[key])
            if sub:
                out[key] = sub
        elif base.get(key) != value:
            out[key] = value
    return out


def save_overrides(path: Path, cfg: Config, base: Config) -> None:
    """Write only the keys of ``cfg`` that differ from ``base`` to ``path``."""
    from narration.artifacts import atomic_write_bytes

    diff = _diff(cfg.model_dump(), base.model_dump())
    atomic_write_bytes(path, tomli_w.dumps(diff).encode())


def project_file(video: Path, name: str) -> Path:
    """Resolve a project-relative file name (glossary, prompts) beside the video."""
    path = Path(name).expanduser()
    return path if path.is_absolute() else video.resolve().parent / path


def read_glossary(video: Path, cfg: Config) -> list[str]:
    """Terms from the glossary file beside the video (one per line, # comments)."""
    try:
        lines = project_file(video, cfg.clean.glossary).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [t.strip() for t in lines if t.strip() and not t.lstrip().startswith("#")]
