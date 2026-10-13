"""[8] assemble: build the narration track and the final video + subtitles.

With no video stretches the video stream is copied untouched; otherwise the
video is cut into the timeline's segments, slowed where needed, and encoded
once with the best available H.264 encoder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from narration import artifacts
from narration.context import RunContext
from narration.media import ffmpeg
from narration.types import TimelineEntry, Unit

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


def srt(entries: list[TimelineEntry], texts: dict[str, str]) -> str:
    cues = []
    for n, e in enumerate(sorted(entries, key=lambda e: e.out_start), start=1):
        cues.append(f"{n}\n{format_srt_time(e.out_start)} --> {format_srt_time(e.out_end)}\n"
                    f"{texts[e.unit_id]}\n")
    return "\n".join(cues)


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
    units = {u["id"]: Unit.from_dict(u)
             for u in artifacts.read_json(ctx.path("script.json"))["units"]}
    probe = artifacts.read_json(ctx.path("probe.json"))
    ctx.reporter.set_total(STAGE, int(duration) + 1)

    narration = ctx.path("narration.wav")
    sf.write(narration, build_track(ctx, entries, duration), RATE, subtype="PCM_16")
    artifacts.atomic_write_text(ctx.path("clean.srt"),
                                srt(entries, {k: u.clean_text or "" for k, u in units.items()}))

    last = 0

    def progress(pos: float) -> None:
        nonlocal last
        if int(pos) > last:
            ctx.reporter.advance(STAGE, int(pos) - last, "encoding")
            last = int(pos)

    out = ctx.path("clean.mp4")
    tmp = out.with_suffix(".partial.mp4")
    audio_args = ["-c:a", "aac", "-b:a", cfg.assemble.audio_bitrate]
    stretched = any(abs((s["out_end"] - s["out_start"]) - (s["src_end"] - s["src_start"]))
                    > 1e-3 for s in segments)
    if not stretched:
        ctx.info("no video stretches: copying the video stream")
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
                 f"({len(segments)} segments, slowed where clips needed room)")
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
