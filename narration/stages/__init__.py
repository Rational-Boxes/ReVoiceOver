"""Stage registry: order, declared inputs/outputs, and on-disk status."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from narration import artifacts
from narration.context import RunContext


class StageNotImplemented(NotImplementedError):
    pass


class Status(StrEnum):
    MISSING = "missing"
    DONE = "done"
    STALE = "stale"
    FAILED = "failed"


StageFn = Callable[[RunContext], Awaitable[None] | None]
PathsFn = Callable[[Path, Path], list[Path]]  # (video, work_dir) -> paths


@dataclass(frozen=True)
class Stage:
    name: str
    title: str
    inputs: PathsFn
    outputs: PathsFn
    module: str  # narration.stages.<module>, imported lazily

    def load(self) -> StageFn:
        import importlib

        return importlib.import_module(f"narration.stages.{self.module}").run


def _w(*names: str) -> PathsFn:
    return lambda video, work: [work / n for n in names]


def _vw(*names: str) -> PathsFn:
    return lambda video, work: [video, *(work / n for n in names)]


STAGES: tuple[Stage, ...] = (
    Stage("extract", "Extract audio", _vw(), _w("probe.json", "audio_16k.wav",
                                                 "audio_full.wav", "chunks.json"), "extract"),
    Stage("transcribe", "Transcribe", _w("chunks.json"), _w("words.json"), "transcribe"),
    Stage("segment", "Segment", _w("words.json", "probe.json"), _w("units.json"), "segment"),
    Stage("clean", "Clean script", _w("units.json"), _w("script.json", "script.md"), "clean"),
    Stage("voice", "Voice", _w("script.json", "audio_full.wav"), _w("voice.json"), "voice"),
    Stage("synth", "Synthesize", _w("script.json", "voice.json"), _w("synth.json"), "synth"),
    Stage("qa", "Quality check", _w("synth.json"), _w("qa.json"), "qa"),
    Stage("fit", "Fit timing", _w("synth.json", "qa.json"), _w("timeline.json"), "fit"),
    Stage("assemble", "Assemble", _vw("timeline.json"), _w("clean.mp4", "clean.srt"), "assemble"),
)

STAGE_NAMES: tuple[str, ...] = tuple(s.name for s in STAGES)
REVIEW_AFTER = "clean"  # the human review checkpoint sits after this stage


def get(name: str) -> Stage:
    for stage in STAGES:
        if stage.name == name:
            return stage
    raise KeyError(f"unknown stage {name!r}; expected one of {', '.join(STAGE_NAMES)}")


def index(name: str) -> int:
    return STAGE_NAMES.index(get(name).name)


def stage_status(stage: Stage, video: Path, work_dir: Path) -> Status:
    manifest = artifacts.read_manifest(work_dir, stage.name)
    if manifest is None:
        return Status.MISSING
    if not manifest.get("ok"):
        return Status.FAILED
    if not all(p.exists() for p in stage.outputs(video, work_dir)):
        return Status.MISSING
    current = artifacts.digest_files(stage.inputs(video, work_dir))
    return Status.DONE if manifest.get("inputs_digest") == current else Status.STALE


def project_status(video: Path, work_dir: Path) -> dict[str, Status]:
    """Status of every stage; anything downstream of a non-done stage is stale."""
    out: dict[str, Status] = {}
    upstream_ok = True
    for stage in STAGES:
        status = stage_status(stage, video, work_dir)
        if status is Status.DONE and not upstream_ok:
            status = Status.STALE
        upstream_ok = upstream_ok and status is Status.DONE
        out[stage.name] = status
    return out


def stage_error(work_dir: Path, name: str) -> str | None:
    manifest = artifacts.read_manifest(work_dir, name)
    return manifest.get("error") if manifest else None
