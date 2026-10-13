"""[4] voice: pick a clean reference clip, register it once, record the voice id.

The registry at ``~/.config/narr/voices.json`` maps a reference clip's sha256
to its DeepInfra voice id, so the same voice is never uploaded twice.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from narration import artifacts
from narration.cache import file_sha256
from narration.clients.deepinfra import DeepInfraClient
from narration.clients.tts import register_voice
from narration.config import Voice as VoiceConfig
from narration.config import project_file, user_config_dir
from narration.context import RunContext
from narration.media import ffmpeg
from narration.media.audio import read_mono
from narration.stages.clean import FILLERS
from narration.types import Word

STAGE = "voice"
REF_FILE = "voice_ref.wav"
STRICT_FILLERS = FILLERS - {"so", "like", "well", "right", "okay", "ok", "yeah"}


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    score: float
    confidence: float
    text: str


def _is_filler(word: Word) -> bool:
    return word.text.lower().strip(".,;:!?…\"'") in STRICT_FILLERS


def find_candidates(words: list[Word], cfg: VoiceConfig, limit: int = 3) -> list[Candidate]:
    """Best word runs of min..max seconds with no fillers and only short pauses."""
    found: list[Candidate] = []
    n = len(words)
    for i in range(n):
        if _is_filler(words[i]):
            continue
        for j in range(i, n):
            w = words[j]
            if _is_filler(w) or (j > i and w.start - words[j - 1].end > cfg.max_gap_s):
                break
            span = w.end - words[i].start
            if span > cfg.max_ref_s:
                break
            if span >= cfg.min_ref_s:
                run = words[i:j + 1]
                conf = float(np.mean([x.confidence or 0.5 for x in run]))
                pauses = sum(max(0.0, b.start - a.end) for a, b in zip(run, run[1:], strict=False))
                score = conf * min(1.0, span / 12.0) - 0.05 * pauses
                found.append(Candidate(run[0].start, run[-1].end, round(score, 4),
                                       round(conf, 3), " ".join(x.text for x in run)))
    found.sort(key=lambda c: c.score, reverse=True)
    picked: list[Candidate] = []
    for c in found:  # keep non-overlapping alternatives
        if all(c.end <= p.start or c.start >= p.end for p in picked):
            picked.append(c)
        if len(picked) == limit:
            break
    return picked


def loudness_ok(path: Path, start: float, end: float) -> bool:
    samples, _ = read_mono(path, start, end)
    if samples.size == 0:
        return False
    rms_db = 20 * np.log10(max(1e-9, float(np.sqrt(np.mean(samples ** 2)))))
    clipped = float(np.mean(np.abs(samples) > 0.99))
    return rms_db > -40 and clipped < 0.001


def registry_path() -> Path:
    return user_config_dir() / "voices.json"


def load_registry() -> dict[str, Any]:
    try:
        return json.loads(registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_registry(registry: dict[str, Any]) -> None:
    artifacts.atomic_write_text(registry_path(), json.dumps(registry, indent=2))


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    full = ctx.path("audio_full.wav")
    ref = ctx.path(REF_FILE)
    candidates: list[Candidate] = []
    span: tuple[float, float] | None = None

    if cfg.voice.ref:
        source = project_file(ctx.video, cfg.voice.ref)
        ctx.info(f"using reference clip {source}")
        ffmpeg.run(["-i", str(source), "-ac", "1", "-ar", "44100",
                    "-af", "afftdn,loudnorm", str(ref)], cancel=ctx.cancel)
    else:
        words = [Word.from_dict(w) for w in artifacts.read_json(ctx.path("words.json"))["words"]]
        candidates = [c for c in find_candidates(words, cfg.voice, limit=6)
                      if loudness_ok(full, c.start, c.end)][:3]
        if not candidates:
            raise RuntimeError(
                f"no clean {cfg.voice.min_ref_s:.0f}-{cfg.voice.max_ref_s:.0f}s stretch found; "
                "set [voice] ref to a clip of your voice")
        best = candidates[0]
        span = (best.start, best.end)
        ctx.info(f"reference {best.start:.1f}-{best.end:.1f}s "
                 f"(confidence {best.confidence:.2f}): {best.text[:80]}")
        ffmpeg.run(["-ss", f"{best.start:.3f}", "-to", f"{best.end:.3f}", "-i", str(full),
                    "-ac", "1", "-ar", "44100", "-af", "afftdn,loudnorm", str(ref)],
                   cancel=ctx.cancel)

    ref_sha = file_sha256(ref)
    registry = load_registry()
    entry = registry.get(ref_sha)
    reused = entry is not None
    if entry is None:
        async with DeepInfraClient.from_context(ctx) as client:
            voice_id = await register_voice(client, ref, f"narr-{ctx.video.stem}"[:60])
        entry = {"voice_id": voice_id, "label": ctx.video.stem, "source": str(ctx.video)}
        registry[ref_sha] = entry
        save_registry(registry)
        ctx.info(f"registered voice {voice_id}")
    else:
        ctx.info(f"reusing registered voice {entry['voice_id']}")

    artifacts.write_json(ctx.path("voice.json"), {
        "model": cfg.models.tts, "voice_id": entry["voice_id"], "ref_sha256": ref_sha,
        "ref_file": REF_FILE, "ref_span": span, "reused": reused,
        "candidates": [asdict(c) for c in candidates],
    })
