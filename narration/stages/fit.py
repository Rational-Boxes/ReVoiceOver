"""[7] fit: make every speech group's clip fit its slot; plan the output timeline.

``anchor`` timing (the default) starts each speech group exactly where the
original speech started. A clip that is too long is first sped up (up to
``fit.max_atempo``); whatever still does not fit is made room for by slowing
that slice of video down. Noises QA found in a clip are muted first.
Writes fitted/<id>.wav and timeline.json.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import soundfile as sf

from narration import artifacts
from narration.cache import file_sha256
from narration.config import Fit as FitConfig
from narration.context import RunContext
from narration.media import ffmpeg
from narration.media.audio import mute, read_mono
from narration.stages import StageNotImplemented
from narration.stages.synth import current_clips
from narration.types import TimelineEntry, Unit

STAGE = "fit"
FITTED_DIR = "fitted"


@dataclass(frozen=True)
class Segment:
    """A range of source video and where it lands in the output."""
    src_start: float
    src_end: float
    out_start: float
    out_end: float

    @property
    def stretch(self) -> float:
        span = self.src_end - self.src_start
        return (self.out_end - self.out_start) / span if span > 0 else 1.0


def plan_anchor(units: list[Unit], clip_durations: dict[str, float], duration: float,
                cfg: FitConfig) -> tuple[list[TimelineEntry], list[Segment]]:
    """Speed factors, video stretches and output times for anchor timing.

    ``clip_durations`` holds trimmed clip lengths for units that have audio.
    """
    entries: list[TimelineEntry] = []
    segments: list[Segment] = []
    shift = 0.0  # how much later the output runs than the source
    first = units[0].start if units else duration
    if first > 0:
        segments.append(Segment(0.0, first, 0.0, first))
    for u in units:
        slot = max(1e-3, u.slot)
        d = clip_durations.get(u.id)
        atempo, out_slot = 1.0, slot
        if d is not None and d > slot:
            atempo = min(cfg.max_atempo, d / slot)
            out_slot = max(slot, d / atempo)  # still too long: stretch the video
        out_start = u.start + shift
        segments.append(Segment(u.start, u.slot_end, out_start, out_start + out_slot))
        if d is not None:
            entries.append(TimelineEntry(
                unit_id=u.id, src_start=u.start, src_end=u.slot_end,
                out_start=round(out_start, 3), out_end=round(out_start + d / atempo, 3),
                audio=f"{FITTED_DIR}/{u.id}.wav", atempo=round(atempo, 4),
                video_stretch=round(out_slot / slot, 4)))
        shift += out_slot - slot
    return entries, merge_segments(segments)


def merge_segments(segments: list[Segment]) -> list[Segment]:
    """Join neighbouring unstretched segments (fewer cuts when assembling)."""
    out: list[Segment] = []
    for seg in segments:
        if out and abs(seg.stretch - 1.0) < 1e-6 and abs(out[-1].stretch - 1.0) < 1e-6 \
                and abs(out[-1].src_end - seg.src_start) < 1e-6:
            prev = out[-1]
            out[-1] = Segment(prev.src_start, seg.src_end, prev.out_start, seg.out_end)
        else:
            out.append(seg)
    return out


def trim_filter(cfg: FitConfig) -> str:
    """Remove leading/trailing silence (keeping 30 ms of air), then fade the edges
    over 15 ms so clips never start or stop with a click."""
    edge = f"silenceremove=start_periods=1:start_threshold={cfg.trim_db}dB:start_silence=0.03"
    fade = "afade=t=in:d=0.015"
    return f"{edge},{fade},areverse,{edge},{fade},areverse"


def atempo_filter(factor: float) -> str:
    # atempo accepts 0.5..100 per instance; our factors are small, one is enough.
    return f"atempo={factor:.4f}"


def noise_by_group(work_dir: Path) -> dict[str, dict[str, Any]]:
    """qa.json's noise spans (and the take they were found in), by group."""
    try:
        groups = artifacts.read_json(work_dir / "qa.json")["groups"]
    except (FileNotFoundError, artifacts.SchemaError, KeyError):
        return {}
    return {gid: e for gid, e in groups.items() if e.get("noise")}


def muted_source(clip: Path, noise: dict[str, Any] | None, tmp: Path) -> Path:
    """``clip``, or a copy with QA's noise spans muted when they belong to this take
    (a new take made after QA keeps its sound until QA runs again)."""
    if not noise or noise.get("noise_audio") != file_sha256(clip):
        return clip
    samples, rate = read_mono(clip)
    sf.write(str(tmp), mute(samples, rate, noise["noise"]), rate, subtype="PCM_16")
    return tmp


def run(ctx: RunContext) -> None:
    cfg = ctx.config
    if cfg.fit.timing != "anchor":
        raise StageNotImplemented("compact timing arrives in milestone 9; use timing = anchor")
    units = [Unit.from_dict(u) for u in artifacts.read_json(ctx.path("script.json"))["units"]]
    clips = current_clips(ctx.work_dir)
    duration = float(artifacts.read_json(ctx.path("probe.json"))["format"]["duration"])
    covered = {uid for entry in clips.values() for uid in entry["units"]}
    missing = [u.id for u in units if u.clean_text and u.id not in covered]
    if missing:
        raise RuntimeError(f"{len(missing)} unit(s) have text but no clip "
                           f"({', '.join(missing[:5])}); run Synthesize first")
    # Each speech group is placed like one big unit spanning its slot.
    voiced = sorted((Unit(id=gid, start=e["start"], end=e["end"], slot_end=e["slot_end"],
                          raw_text="", clean_text=e["text"]) for gid, e in clips.items()),
                    key=lambda g: g.start)

    out_dir = ctx.path(FITTED_DIR)
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("*.wav"):
        old.unlink()
    ctx.reporter.set_total(STAGE, len(voiced) * 2)

    # Pass 1: mute noises, trim the edges and measure what is left.
    noise = noise_by_group(ctx.work_dir)
    trimmed: dict[str, float] = {}
    for u in voiced:
        ctx.check_cancelled()
        tmp = out_dir / f"{u.id}.trim.wav"
        muted = out_dir / f"{u.id}.mute.wav"
        src = muted_source(ctx.path(clips[u.id]["file"]), noise.get(u.id), muted)
        ffmpeg.run(["-i", str(src), "-af", trim_filter(cfg.fit),
                    "-c:a", "pcm_s16le", str(tmp)], cancel=ctx.cancel)
        muted.unlink(missing_ok=True)
        trimmed[u.id] = ffmpeg.probe(tmp).duration
        ctx.reporter.advance(STAGE, 1, u.id)

    entries, segments = plan_anchor(voiced, trimmed, duration, cfg.fit)

    # Pass 2: apply speed-ups.
    for e in entries:
        ctx.check_cancelled()
        tmp = out_dir / f"{e.unit_id}.trim.wav"
        final = ctx.path(e.audio)
        if e.atempo > 1.0:
            ffmpeg.run(["-i", str(tmp), "-af", atempo_filter(e.atempo), "-c:a", "pcm_s16le",
                        str(final)], cancel=ctx.cancel)
            tmp.unlink()
        else:
            tmp.replace(final)
        ctx.reporter.advance(STAGE, 1, e.unit_id)

    out_duration = segments[-1].out_end if segments else duration
    artifacts.write_json(ctx.path("timeline.json"), {
        "timing": "anchor", "source_duration": duration,
        "output_duration": round(out_duration, 3),
        "entries": [e.to_dict() for e in entries],
        "segments": [asdict(s) for s in segments],
    })
    sped = [e for e in entries if e.atempo > 1.0]
    stretched = [e for e in entries if e.video_stretch > 1.0]
    ctx.info(f"{len(entries)} clips fitted: {len(sped)} sped up "
             f"(max {max((e.atempo for e in sped), default=1):.2f}x), {len(stretched)} needed "
             f"the video slowed down; output {out_duration:.1f}s vs source {duration:.1f}s")
