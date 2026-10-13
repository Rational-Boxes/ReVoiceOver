"""[0] extract: probe the video, pull ASR and reference audio, chunk long audio."""

from __future__ import annotations

from narration import artifacts
from narration.context import RunContext
from narration.media import ffmpeg
from narration.types import Chunk

STAGE = "extract"


def plan_chunks(
    duration: float,
    silences: list[tuple[float, float]],
    *,
    chunk_s: float,
    search_s: float,
) -> list[tuple[float, float]]:
    """Split ``[0, duration]`` near multiples of ``chunk_s``, cutting at silences.

    Each cut goes at the midpoint of the silence closest to the target time
    within ``search_s``; with no silence nearby, it cuts hard at the target.
    """
    bounds = [0.0]
    while duration - bounds[-1] > chunk_s * 1.25:
        target = bounds[-1] + chunk_s
        candidates = [
            (s + e) / 2 for s, e in silences
            if abs((s + e) / 2 - target) <= search_s and (s + e) / 2 > bounds[-1] + 1.0
        ]
        bounds.append(min(candidates, key=lambda m: abs(m - target)) if candidates else target)
    bounds.append(duration)
    return list(zip(bounds[:-1], bounds[1:], strict=True))


def run(ctx: RunContext) -> None:
    cfg = ctx.config.extract
    info = ffmpeg.probe(ctx.video)
    if not info.has_audio:
        raise RuntimeError(f"{ctx.video.name} has no audio stream to transcribe")
    if info.duration <= 0:
        raise RuntimeError(f"{ctx.video.name}: could not determine duration")
    artifacts.write_json(ctx.path("probe.json"), info.raw)
    ctx.info(
        f"{ctx.video.name}: {info.duration:.1f}s, "
        f"video={info.video_codec or 'none'} {info.width or ''}x{info.height or ''} "
        f"@ {info.fps or 0:.2f}fps, audio={info.audio_codec} {info.audio_rate}Hz"
    )

    # Progress is measured in seconds of media processed across both passes.
    total = int(info.duration * 2)
    ctx.reporter.set_total(STAGE, total)

    def tracker(label: str):
        last = 0

        def update(pos: float) -> None:
            nonlocal last
            now = min(int(pos), int(info.duration))
            if now > last:
                ctx.reporter.advance(STAGE, now - last, label)
                last = now
        return update

    for name, rate in (("audio_16k.wav", 16000), ("audio_full.wav", 44100)):
        ctx.check_cancelled()
        ffmpeg.extract_audio(ctx.video, ctx.path(name), rate=rate,
                             cancel=ctx.cancel, on_progress=tracker(name))

    ctx.check_cancelled()
    audio = ctx.path("audio_16k.wav")
    if info.duration <= cfg.chunk_s * 1.25:
        chunks = [Chunk(file=ctx.rel(audio), offset=0.0, duration=info.duration)]
    else:
        silences = ffmpeg.detect_silences(audio, noise_db=cfg.silence_db,
                                          min_s=cfg.silence_min_s)
        spans = plan_chunks(info.duration, silences, chunk_s=cfg.chunk_s,
                            search_s=cfg.chunk_search_s)
        chunk_dir = ctx.path("chunks")
        chunk_dir.mkdir(exist_ok=True)
        chunks = []
        for i, (start, end) in enumerate(spans):
            ctx.check_cancelled()
            dst = chunk_dir / f"chunk_{i:03d}.wav"
            ffmpeg.cut_audio(audio, dst, start, end, cancel=ctx.cancel)
            chunks.append(Chunk(file=ctx.rel(dst), offset=start, duration=end - start))
        ctx.info(f"split ASR audio into {len(chunks)} chunks at silences")
    artifacts.write_json(ctx.path("chunks.json"), [c.to_dict() for c in chunks])
