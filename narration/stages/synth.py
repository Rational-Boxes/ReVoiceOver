"""[5] synth: speak the script in the cloned voice, one speech group at a time.

Review units are often sentence fragments; synthesizing each on its own resets
the voice's intonation and level every few seconds. Consecutive units are
therefore joined into speech groups: a fragment always continues into the unit
that completes its sentence (unless the pause is very long), and sentences join
across short pauses. Each group starts at its first unit's original time.

Pronunciation fixes from ``pronunciations.toml`` (beside the video) are applied
to the text sent to TTS only, so the script and subtitles keep the real text::

    "LEED" = "leed"
    "5.0" = "five point oh"
"""

from __future__ import annotations

import asyncio
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from narration import artifacts
from narration.cache import FileCache, make_key
from narration.clients.deepinfra import DeepInfraClient
from narration.clients.tts import TTSBackend, make_tts
from narration.config import Config, project_file
from narration.config import Synth as SynthConfig
from narration.context import RunContext
from narration.media import ffmpeg
from narration.types import Unit

STAGE = "synth"
TTS_DIR = "tts"
SENTENCE_END = (".", "?", "!", "…")


@dataclass
class Clip:
    file: str          # normalized clip, relative to the work dir
    duration: float
    chars: int
    seed: int
    cached: bool
    text: str = ""     # the script text it was made from (before pronunciation fixes)


@dataclass
class Group:
    """Consecutive units spoken as one TTS request."""
    id: str
    units: list[str]
    start: float
    end: float
    slot_end: float    # next group's start (or the end of the video)
    unit_texts: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(self.unit_texts[u] for u in self.units)

    @property
    def slot(self) -> float:
        return self.slot_end - self.start

    @classmethod
    def from_entry(cls, gid: str, entry: dict[str, Any]) -> Group:
        return cls(gid, list(entry["units"]), entry["start"], entry["end"], entry["slot_end"],
                   dict(entry["unit_texts"]))


def ends_sentence(text: str) -> bool:
    return text.rstrip("\"')”’ ").endswith(SENTENCE_END)


def plan_groups(units: list[Unit], cfg: SynthConfig) -> list[Group]:
    voiced = [u for u in units if u.clean_text]
    runs: list[list[Unit]] = []
    for u in voiced:
        if runs:
            prev = runs[-1][-1]
            gap = u.start - prev.end
            limit = cfg.group_sentence_gap_s if ends_sentence(prev.clean_text or "") \
                else cfg.group_fragment_gap_s
            if gap <= limit and u.end - runs[-1][0].start <= cfg.group_max_s:
                runs[-1].append(u)
                continue
        runs.append([u])
    video_end = units[-1].slot_end if units else 0.0
    groups = []
    for i, run in enumerate(runs):
        slot_end = runs[i + 1][0].start if i + 1 < len(runs) else max(video_end, run[-1].end)
        groups.append(Group(f"g{i + 1:04d}", [u.id for u in run], run[0].start, run[-1].end,
                            slot_end, {u.id: u.clean_text or "" for u in run}))
    return groups


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


async def synthesize_group(ctx: RunContext, tts: TTSBackend, group: Group, voice_id: str,
                           pronunciations: dict[str, str], seed: int | None = None) -> Clip:
    """Synthesize (or reuse) one group's clip and write tts/<id>.wav, normalized."""
    cfg = ctx.config
    seed = cfg.synth.seed if seed is None else seed
    text = tts_text(group.text, pronunciations)
    cache = FileCache(ctx.work_dir, "tts")
    key = clip_key(tts.name, voice_id, text, cfg, seed)
    raw = cache.get_bytes(key, ".wav")
    cached = raw is not None
    if raw is None:
        raw = await tts.synthesize(text, voice_id, rate=cfg.synth.speaker_rate, seed=seed,
                                   language=cfg.synth.language)
        cache.put_bytes(key, ".wav", raw)
    out = ctx.path(TTS_DIR, f"{group.id}.wav")
    out.parent.mkdir(exist_ok=True)
    ffmpeg.run(["-i", str(cache.path(key, ".wav")), "-ac", "1",
                "-ar", str(cfg.synth.sample_rate),
                "-af", f"loudnorm=I={cfg.synth.loudness_lufs}:TP=-1.5:LRA=11",
                "-c:a", "pcm_s16le", str(out)], cancel=ctx.cancel)
    duration = ffmpeg.probe(out).duration
    return Clip(file=ctx.rel(out), duration=round(duration, 3), chars=len(text), seed=seed,
                cached=cached, text=group.text)


def group_entry(group: Group, clip: Clip) -> dict[str, Any]:
    return {**asdict(clip), "units": group.units, "start": group.start, "end": group.end,
            "slot_end": group.slot_end, "unit_texts": group.unit_texts}


def price_per_char(cfg: Config) -> float:
    return cfg.prices.tts_per_mchar.get(cfg.models.tts, 0.0) / 1_000_000


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    units = [Unit.from_dict(u) for u in artifacts.read_json(ctx.path("script.json"))["units"]]
    voice_id = artifacts.read_json(ctx.path("voice.json"))["voice_id"]
    groups = plan_groups(units, cfg.synth)
    pronunciations = load_pronunciations(ctx.video, cfg)
    ctx.info(f"synthesizing {sum(len(g.units) for g in groups)} units as {len(groups)} speech "
             f"groups with {cfg.models.tts} (voice {voice_id})"
             + (f"; {len(pronunciations)} pronunciation fixes" if pronunciations else ""))
    ctx.reporter.set_total(STAGE, len(groups))

    clips: dict[str, Clip] = {}
    async with DeepInfraClient.from_context(ctx) as client:
        tts = make_tts(client, cfg.models.tts)

        async def one(group: Group) -> None:
            clips[group.id] = await synthesize_group(ctx, tts, group, voice_id, pronunciations)
            for uid in group.units:
                ctx.reporter.unit_updated(uid, "synth")
            ctx.reporter.advance(STAGE, 1, group.id)

        await asyncio.gather(*(one(g) for g in groups))

    # Clips that no longer belong to a group must not linger into fit/assemble.
    tts_dir = ctx.path(TTS_DIR)
    for stale in tts_dir.glob("*.wav") if tts_dir.exists() else []:
        if stale.stem not in clips:
            stale.unlink()

    artifacts.write_json(ctx.path("synth.json"), {
        "model": cfg.models.tts, "voice_id": voice_id,
        "groups": {g.id: group_entry(g, clips[g.id]) for g in groups},
        "unit_groups": {uid: g.id for g in groups for uid in g.units},
    })
    billed = sum(c.chars for c in clips.values() if not c.cached)
    reused = sum(1 for c in clips.values() if c.cached)
    over = sum(1 for g in groups if clips[g.id].duration > g.slot)
    ctx.info(f"{len(clips)} clips, {sum(c.duration for c in clips.values()):.0f}s of audio; "
             f"{billed} chars billed (~${billed * price_per_char(cfg):.3f})"
             + (f", {reused} from cache" if reused else "")
             + (f"; {over} clip(s) longer than their slot" if over else ""))


async def resynth(ctx: RunContext, unit: Unit, seed: int | None = None) -> Clip:
    """Re-synthesize the group containing ``unit`` (e.g. a GUI "new take").

    ``unit`` carries its current (possibly unsaved) text; the group's other units
    use the saved script.
    """
    data = artifacts.read_json(ctx.path("synth.json"))
    gid = data["unit_groups"].get(unit.id)
    if gid is None:
        raise ValueError(f"{unit.id} is not in any speech group; run Synthesize first")
    group = Group.from_entry(gid, data["groups"][gid])
    script = {u["id"]: u for u in artifacts.read_json(ctx.path("script.json"))["units"]}
    for uid in group.units:
        group.unit_texts[uid] = (unit.clean_text if uid == unit.id
                                 else script.get(uid, {}).get("clean_text")) or ""
    voice_id = data["voice_id"]
    async with DeepInfraClient.from_context(ctx) as client:
        tts = make_tts(client, ctx.config.models.tts)
        clip = await synthesize_group(ctx, tts, group, voice_id,
                                      load_pronunciations(ctx.video, ctx.config), seed)
    data["groups"][gid] = group_entry(group, clip)
    artifacts.write_json(ctx.path("synth.json"), data)
    ctx.info(f"{gid} ({len(group.units)} units): new take (seed {clip.seed}), "
             f"{clip.duration:.1f}s" + ("" if clip.cached else f", {clip.chars} chars"))
    return clip


def current_clips(work_dir: Path) -> dict[str, dict[str, Any]]:
    """synth.json groups with QA's chosen takes laid over them.

    QA may keep a retry take; it records that in qa.json (not synth.json, which
    is QA's own input) and leaves the take's audio in tts/<id>.wav.
    """
    try:
        groups: dict[str, dict[str, Any]] = dict(
            artifacts.read_json(work_dir / "synth.json")["groups"])
    except (FileNotFoundError, artifacts.SchemaError, KeyError):
        return {}
    try:
        qa = artifacts.read_json(work_dir / "qa.json")["groups"]
    except (FileNotFoundError, artifacts.SchemaError, KeyError):
        return groups
    for gid, entry in qa.items():
        take = entry.get("take")
        if gid in groups and take:
            groups[gid] = {**groups[gid], **take}
    return groups


def unit_clips(work_dir: Path) -> dict[str, dict[str, Any]]:
    """Per-unit view for the GUI: each unit's group clip, plus the unit's own text."""
    out = {}
    for gid, entry in current_clips(work_dir).items():
        for uid in entry["units"]:
            out[uid] = {**entry, "group": gid, "text": entry["unit_texts"].get(uid, "")}
    return out
