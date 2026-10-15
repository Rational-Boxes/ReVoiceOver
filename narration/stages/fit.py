"""[7] fit: make every speech group's clip fit its slot; plan the output timeline.

``anchor`` timing (the default) starts each speech group exactly where the
original speech started. A clip that is too long is first sped up (up to
``fit.max_atempo``); whatever still does not fit is made room for by slowing
that slice of video down.

``compact`` timing also removes dead air: each group keeps the stretch of video
its words cover (plus ``fit.compact_gap_s``), and the pauses between groups and
the video under dropped lines are cut. A line that was emptied because its words
were moved into a neighbouring line still counts as covered by that neighbour, so
the on-screen action those words describe is kept.

Noises QA found in a clip are muted first. Writes fitted/<id>.wav and timeline.json.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import soundfile as sf

from narration import artifacts
from narration.cache import file_sha256
from narration.config import Fit as FitConfig
from narration.context import RunContext
from narration.media import ffmpeg
from narration.media.audio import mute, read_mono
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


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) >= 3]


def _similar(a: str, b: str) -> bool:
    """Same word, allowing for inflections and typos ("materials" / "meterial")."""
    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def added_words(raw: str, clean: str) -> list[str]:
    """Words in ``clean`` that were not said in ``raw``."""
    said = _content_words(raw)
    return [w for w in _content_words(clean) if not any(_similar(w, r) for r in said)]


def moved_lines(units: list[Unit], groups: list[Unit],
                group_units: dict[str, list[str]]) -> dict[str, str]:
    """Emptied units whose words now appear in a neighbouring group: unit id -> group id.

    An emptied unit between two groups belongs to whichever of them gained more of
    its words (the earlier one on a tie); if neither did, its content was dropped.
    """
    raw = {u.id: u.raw_text for u in units}
    gained = {g.id: added_words(" ".join(raw.get(uid, "") for uid in group_units[g.id]),
                                g.clean_text or "") for g in groups}
    voiced = {uid for ids in group_units.values() for uid in ids}
    out: dict[str, str] = {}
    for u in units:
        if u.id in voiced or u.clean_text:
            continue
        words = _content_words(u.raw_text)
        prev = next((g for g in reversed(groups) if g.start <= u.start), None)
        nxt = next((g for g in groups if g.start >= u.end), None)
        best, best_score = None, 0
        for g in (prev, nxt):
            if g is None:
                continue
            score = sum(any(_similar(w, a) for a in gained[g.id]) for w in words)
            if score > best_score:
                best, best_score = g.id, score
        if best:
            out[u.id] = best
    return out


def content_spans(units: list[Unit], groups: list[Unit], group_units: dict[str, list[str]],
                  duration: float) -> tuple[list[Unit], dict[str, str]]:
    """``groups`` with start/end widened over the lines moved into them, and slot_end
    set to the next group's (widened) start."""
    moved = moved_lines(units, groups, group_units)
    by_id = {u.id: u for u in units}
    spans = {g.id: [g.start, g.end] for g in groups}
    for uid, gid in moved.items():
        spans[gid][0] = min(spans[gid][0], by_id[uid].start)
        spans[gid][1] = max(spans[gid][1], by_id[uid].end)
    widened = sorted((Unit(id=g.id, start=spans[g.id][0], end=spans[g.id][1], slot_end=0.0,
                           raw_text="", clean_text=g.clean_text) for g in groups),
                     key=lambda g: g.start)
    for i, g in enumerate(widened):
        g.slot_end = widened[i + 1].start if i + 1 < len(widened) else duration
    return widened, moved


def plan_compact(units: list[Unit], clip_durations: dict[str, float], duration: float,
                 cfg: FitConfig) -> tuple[list[TimelineEntry], list[Segment]]:
    """Speed factors, video cuts and output times for compact timing.

    Each unit keeps its source video from ``start`` until its speech (the original
    ``end``, or the new clip if that runs longer) plus ``compact_gap_s``, never past
    ``slot_end``; everything else is cut. A clip that still does not fit is sped up
    and then the video slowed, as in anchor timing.
    """
    gap = cfg.compact_gap_s
    entries: list[TimelineEntry] = []
    segments: list[Segment] = []
    out = 0.0
    if units and units[0].start > 0:
        lead = min(units[0].start, gap)
        segments.append(Segment(units[0].start - lead, units[0].start, 0.0, lead))
        out = lead
    for u in units:
        d = clip_durations.get(u.id)
        speech = max(u.end - u.start, d or 0.0)
        src_end = min(u.slot_end, u.start + speech + gap)
        src = max(1e-3, src_end - u.start)
        # Room for the clip: up to the gap, or at least the original speech.
        room = max(1e-3, src - gap, min(src, u.end - u.start))
        atempo, out_slot = 1.0, src
        if d is not None and d > room:
            atempo = min(cfg.max_atempo, d / room)
            out_slot = max(src, d / atempo + (src - room))
        segments.append(Segment(u.start, src_end, out, out + out_slot))
        if d is not None:
            entries.append(TimelineEntry(
                unit_id=u.id, src_start=u.start, src_end=round(src_end, 3),
                out_start=round(out, 3), out_end=round(out + d / atempo, 3),
                audio=f"{FITTED_DIR}/{u.id}.wav", atempo=round(atempo, 4),
                video_stretch=round(out_slot / src, 4)))
        out += out_slot
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

    moved: dict[str, str] = {}
    if cfg.fit.timing == "compact":
        voiced, moved = content_spans(units, voiced, {gid: e["units"] for gid, e in clips.items()},
                                      duration)
        entries, segments = plan_compact(voiced, trimmed, duration, cfg.fit)
    else:
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
        "timing": cfg.fit.timing, "source_duration": duration,
        "output_duration": round(out_duration, 3),
        "entries": [e.to_dict() for e in entries],
        "segments": [asdict(s) for s in segments],
    })
    sped = [e for e in entries if e.atempo > 1.0]
    stretched = [e for e in entries if e.video_stretch > 1.0]
    ctx.info(f"{len(entries)} clips fitted: {len(sped)} sped up "
             f"(max {max((e.atempo for e in sped), default=1):.2f}x), {len(stretched)} needed "
             f"the video slowed down; output {out_duration:.1f}s vs source {duration:.1f}s")
    for uid, gid in moved.items():
        ctx.info(f"{uid} is empty but its words moved into {gid}: keeping its video there")
