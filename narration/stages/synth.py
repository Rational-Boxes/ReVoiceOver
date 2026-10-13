"""[5] synth: TTS every non-empty unit in the cloned voice, cached by content.

Pronunciation fixes from ``pronunciations.toml`` (beside the video) are applied
to the text sent to TTS only, so the script and subtitles keep the real text::

    "LEED" = "leed"
    "5.0" = "five point oh"
"""

from __future__ import annotations

import asyncio
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from narration import artifacts
from narration.cache import FileCache, make_key
from narration.clients.deepinfra import DeepInfraClient
from narration.clients.tts import TTSBackend, make_tts
from narration.config import Config, project_file
from narration.context import RunContext
from narration.media import ffmpeg
from narration.types import Unit

STAGE = "synth"
TTS_DIR = "tts"


@dataclass
class Clip:
    file: str          # normalized clip, relative to the work dir
    duration: float
    chars: int
    seed: int
    cached: bool
    text: str = ""     # the script text it was made from (before pronunciation fixes)


def load_pronunciations(video: Path, cfg: Config) -> dict[str, str]:
    path = project_file(video, cfg.clean.pronunciations)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in data.items()}


def tts_text(text: str, pronunciations: dict[str, str]) -> str:
    """Apply whole-word pronunciation replacements, longest terms first."""
    for term in sorted(pronunciations, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", pronunciations[term], text)
    return text


def clip_key(model: str, voice_id: str, text: str, cfg: Config, seed: int) -> str:
    s = cfg.synth
    return make_key("tts-v1", model, voice_id, text, s.language, s.speaker_rate, seed)


async def synthesize_unit(ctx: RunContext, tts: TTSBackend, unit: Unit, voice_id: str,
                          pronunciations: dict[str, str], seed: int | None = None) -> Clip:
    """Synthesize (or reuse) one unit's clip and write tts/<id>.wav, normalized."""
    cfg = ctx.config
    seed = cfg.synth.seed if seed is None else seed
    text = tts_text(unit.clean_text or "", pronunciations)
    cache = FileCache(ctx.work_dir, "tts")
    key = clip_key(tts.name, voice_id, text, cfg, seed)
    raw = cache.get_bytes(key, ".wav")
    cached = raw is not None
    if raw is None:
        raw = await tts.synthesize(text, voice_id, rate=cfg.synth.speaker_rate, seed=seed,
                                   language=cfg.synth.language)
        cache.put_bytes(key, ".wav", raw)
    out = ctx.path(TTS_DIR, f"{unit.id}.wav")
    out.parent.mkdir(exist_ok=True)
    ffmpeg.run(["-i", str(cache.path(key, ".wav")), "-ac", "1",
                "-ar", str(cfg.synth.sample_rate),
                "-af", f"loudnorm=I={cfg.synth.loudness_lufs}:TP=-1.5:LRA=11",
                "-c:a", "pcm_s16le", str(out)], cancel=ctx.cancel)
    duration = ffmpeg.probe(out).duration
    return Clip(file=ctx.rel(out), duration=round(duration, 3), chars=len(text), seed=seed,
                cached=cached, text=unit.clean_text or "")


def price_per_char(cfg: Config) -> float:
    return cfg.prices.tts_per_mchar.get(cfg.models.tts, 0.0) / 1_000_000


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    script = artifacts.read_json(ctx.path("script.json"))
    units = [Unit.from_dict(u) for u in script["units"]]
    voice = artifacts.read_json(ctx.path("voice.json"))
    voice_id = voice["voice_id"]
    todo = [u for u in units if u.clean_text]
    pronunciations = load_pronunciations(ctx.video, cfg)
    ctx.info(f"synthesizing {len(todo)} units with {cfg.models.tts} (voice {voice_id})"
             + (f"; {len(pronunciations)} pronunciation fixes" if pronunciations else ""))
    ctx.reporter.set_total(STAGE, len(todo))

    clips: dict[str, Clip] = {}
    async with DeepInfraClient.from_context(ctx) as client:
        tts = make_tts(client, cfg.models.tts)

        async def one(unit: Unit) -> None:
            clips[unit.id] = await synthesize_unit(ctx, tts, unit, voice_id, pronunciations)
            ctx.reporter.unit_updated(unit.id, "synth")
            ctx.reporter.advance(STAGE, 1, unit.id)

        await asyncio.gather(*(one(u) for u in todo))

    # Clips of units that are now empty must not linger into fit/assemble.
    for stale in ctx.path(TTS_DIR).glob("*.wav") if ctx.path(TTS_DIR).exists() else []:
        if stale.stem not in clips:
            stale.unlink()

    artifacts.write_json(ctx.path("synth.json"), {
        "model": cfg.models.tts, "voice_id": voice_id,
        "units": {uid: asdict(c) for uid, c in sorted(clips.items())},
    })
    billed = sum(c.chars for c in clips.values() if not c.cached)
    reused = sum(1 for c in clips.values() if c.cached)
    over = sum(1 for u in todo if clips[u.id].duration > u.slot)
    ctx.info(f"{len(clips)} clips, {sum(c.duration for c in clips.values()):.0f}s of audio; "
             f"{billed} chars billed (~${billed * price_per_char(cfg):.3f})"
             + (f", {reused} from cache" if reused else "")
             + (f"; {over} clip(s) longer than their slot" if over else ""))


async def resynth(ctx: RunContext, unit: Unit, seed: int | None = None) -> Clip:
    """Re-synthesize one unit (e.g. from the GUI) and update its synth.json entry."""
    voice_id = artifacts.read_json(ctx.path("voice.json"))["voice_id"]
    async with DeepInfraClient.from_context(ctx) as client:
        tts = make_tts(client, ctx.config.models.tts)
        clip = await synthesize_unit(ctx, tts, unit, voice_id,
                                     load_pronunciations(ctx.video, ctx.config), seed)
    path = ctx.path("synth.json")
    try:
        data = artifacts.read_json(path)
    except FileNotFoundError:
        data = {"model": ctx.config.models.tts, "voice_id": voice_id, "units": {}}
    data["units"][unit.id] = asdict(clip)
    artifacts.write_json(path, data)
    ctx.info(f"{unit.id}: new take (seed {clip.seed}), {clip.duration:.1f}s"
             + ("" if clip.cached else f", {clip.chars} chars"))
    return clip


def current_clips(work_dir: Path) -> dict[str, dict[str, Any]]:
    """synth.json clips with QA's chosen takes laid over them.

    QA may keep a retry take; it records that in qa.json (not synth.json, which
    is QA's own input) and leaves the take's audio in tts/<id>.wav.
    """
    try:
        clips: dict[str, dict[str, Any]] = dict(
            artifacts.read_json(work_dir / "synth.json")["units"])
    except (FileNotFoundError, artifacts.SchemaError):
        return {}
    try:
        qa = artifacts.read_json(work_dir / "qa.json")["units"]
    except (FileNotFoundError, artifacts.SchemaError):
        return clips
    for uid, entry in qa.items():
        take = entry.get("take")
        if uid in clips and take:
            clips[uid] = {**clips[uid], **take}
    return clips
