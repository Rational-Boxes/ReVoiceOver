"""[8] assemble: build the narration track and the final video + subtitles.

When the timeline keeps the whole video at normal speed the video stream is
copied untouched; otherwise the video is cut into the timeline's segments,
slowed where needed, and encoded once with the best available H.264 encoder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from narration import artifacts
from narration.context import RunContext
from narration.media import ffmpeg
from narration.types import TimelineEntry

STAGE = "assemble"
RATE = 44100


def build_track(ctx: RunContext, entries: list[TimelineEntry], duration: float) -> np.ndarray:
    track = np.zeros(int(np.ceil(duration * RATE)) + RATE, dtype=np.float32)
    for e in entries:
        data, rate = sf.read(ctx.path(e.audio), dtype="float32", always_2d=True)
        if rate != RATE:
            raise RuntimeError(f"{e.audio}: expected {RATE} Hz, got {rate}")
        mono = data.mean(axis=1)
        i = int(round(e.out_start * RATE))
        track[i:i + len(mono)] += mono[: max(0, len(track) - i)]
    return np.clip(track[: int(np.ceil(duration * RATE))], -1.0, 1.0)


def format_srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def unit_cues(entries: list[TimelineEntry],
              groups: dict[str, dict[str, Any]]) -> list[tuple[float, float, str]]:
    """One caption per unit: a group's spoken time is shared out by text length."""
    cues = []
    for e in sorted(entries, key=lambda e: e.out_start):
        texts = [groups[e.unit_id]["unit_texts"][u] for u in groups[e.unit_id]["units"]]
        total = sum(len(t) for t in texts) or 1
        t = e.out_start
        for text in texts:
            span = (e.out_end - e.out_start) * len(text) / total
            if text:
                cues.append((t, t + span, text))
            t += span
    return cues


def srt(cues: list[tuple[float, float, str]]) -> str:
    return "\n".join(f"{n}\n{format_srt_time(a)} --> {format_srt_time(b)}\n{text}\n"
                     for n, (a, b, text) in enumerate(cues, start=1))


def video_filter(segments: list[dict[str, Any]]) -> str:
    """trim + setpts per segment, then concat (video only)."""
    parts, labels = [], []
    for i, s in enumerate(segments):
        stretch = (s["out_end"] - s["out_start"]) / max(1e-6, s["src_end"] - s["src_start"])
        pts = f"(PTS-STARTPTS)*{stretch:.6f}" if abs(stretch - 1) > 1e-6 else "PTS-STARTPTS"
        parts.append(f"[0:v]trim=start={s['src_start']:.4f}:end={s['src_end']:.4f},"
                     f"setpts={pts}[v{i}]")
        labels.append(f"[v{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vout]")
    return ";".join(parts)


def video_unchanged(segments: list[dict[str, Any]], source_duration: float) -> bool:
    """True when the segments play the whole source, in order, at normal speed."""
    if not segments:
        return True
    pos = 0.0
    for s in segments:
        if abs(s["src_start"] - pos) > 1e-3 or abs(s["out_start"] - pos) > 1e-3 \
                or abs((s["out_end"] - s["out_start"]) - (s["src_end"] - s["src_start"])) > 1e-3:
            return False
        pos = s["src_end"]
    return abs(pos - source_duration) < 1e-2


def _video_bitrate(probe: dict[str, Any], configured: str) -> str:
    if configured:
        return configured
    video: dict[str, Any] = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    bps = int(video.get("bit_rate") or probe.get("format", {}).get("bit_rate") or 6_000_000)
    return f"{max(1_000_000, bps) // 1000}k"


def run(ctx: RunContext) -> None:
    cfg = ctx.config
    timeline = artifacts.read_json(ctx.path("timeline.json"))
    entries = [TimelineEntry.from_dict(e) for e in timeline["entries"]]
    segments = timeline["segments"]
    duration = float(timeline["output_duration"])
    groups = artifacts.read_json(ctx.path("synth.json"))["groups"]
    probe = artifacts.read_json(ctx.path("probe.json"))
    ctx.reporter.set_total(STAGE, int(duration) + 1)

    narration = ctx.path("narration.wav")
    sf.write(narration, build_track(ctx, entries, duration), RATE, subtype="PCM_16")
    artifacts.atomic_write_text(ctx.path("clean.srt"), srt(unit_cues(entries, groups)))

    last = 0

    def progress(pos: float) -> None:
        nonlocal last
        if int(pos) > last:
            ctx.reporter.advance(STAGE, int(pos) - last, "encoding")
            last = int(pos)

    out = ctx.path("clean.mp4")
    tmp = out.with_suffix(".partial.mp4")
    audio_args = ["-c:a", "aac", "-b:a", cfg.assemble.audio_bitrate]
    if video_unchanged(segments, float(timeline["source_duration"])):
        ctx.info("no video stretches or cuts: copying the video stream")
        ffmpeg.run(["-i", str(ctx.video), "-i", str(narration), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", *audio_args, "-shortest", "-movflags", "+faststart",
                    str(tmp)], cancel=ctx.cancel, on_progress=progress)
    else:
        encoder = ffmpeg.h264_encoder()
        if encoder is None:
            raise RuntimeError("no H.264 encoder in this ffmpeg (need libx264, libopenh264 or "
                               "a hardware encoder); see Help > Troubleshooting")
        bitrate = _video_bitrate(probe, cfg.assemble.video_bitrate)
        ctx.info(f"re-encoding video with {encoder} at {bitrate} "
                 f"({len(segments)} segments, {timeline.get('timing', 'anchor')} timing)")
        script = ctx.path("assemble_filter.txt")
        artifacts.atomic_write_text(script, video_filter(segments))
        ffmpeg.run(["-i", str(ctx.video), "-i", str(narration),
                    "-filter_complex_script", str(script), "-map", "[vout]", "-map", "1:a:0",
                    "-c:v", encoder, "-b:v", bitrate, "-pix_fmt", "yuv420p", *audio_args,
                    "-shortest", "-movflags", "+faststart", str(tmp)],
                   cancel=ctx.cancel, on_progress=progress)
    tmp.replace(out)
    ctx.info(f"wrote {out} ({out.stat().st_size / 1e6:.0f} MB, {duration:.1f}s) and "
             f"{Path(ctx.rel(ctx.path('clean.srt')))}")
