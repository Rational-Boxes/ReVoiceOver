"""[6] qa: re-transcribe every speech group's clip and catch skipped/invented words.

A clip whose word error rate against its script text exceeds ``qa.max_wer`` is
re-synthesized with new seeds (up to ``qa.max_retries``); the best take wins.
Clips that stay bad, or have long silences or clipping, are flagged for review.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import jiwer
import numpy as np

from narration import artifacts
from narration.cache import FileCache, file_sha256, make_key
from narration.clients.asr import transcribe_text
from narration.clients.deepinfra import DeepInfraClient
from narration.clients.tts import make_tts
from narration.context import RunContext
from narration.media import ffmpeg
from narration.media.audio import read_mono
from narration.stages.synth import Clip, Group, load_pronunciations, synthesize_group

STAGE = "qa"
_PUNCT = re.compile(r"[^\w\s']")
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b")


@dataclass
class Result:
    wer: float
    heard: str
    retries: int = 0
    flags: list[str] = field(default_factory=list)
    take: dict[str, Any] | None = None  # set when a retry replaced the synth take


def normalize_for_wer(text: str, hyphen: str = "") -> str:
    """Lowercase, no punctuation, ordinals as plain numbers, hyphens joined or split."""
    text = text.lower().replace("’", "'").replace("-", hyphen)
    text = _ORDINAL.sub(r"\1", text)
    return " ".join(_PUNCT.sub(" ", text).split())


def too_many_errors(expected: str, wer: float, max_wer: float) -> bool:
    """One word off is within ASR noise; beyond that, the rate must exceed max_wer.

    Losing half the words (or all of them) is always too many.
    """
    words = len(normalize_for_wer(expected).split())
    errors = round(wer * max(1, words))
    return wer >= 0.5 or errors > max(1, int(max_wer * words))


_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")


def missing_acronyms(expected: str, heard: str) -> list[str]:
    """All-caps terms (AIA, USGBC, EPD) in the script that Whisper did not hear.

    Voices tend to skip exactly these, and one dropped word can hide inside an
    otherwise fine WER. Spacing and dots are ignored ("A I A", "A.I.A." count).
    """
    squashed = re.sub(r"[^a-z0-9]", "", heard.lower())
    return [t for t in dict.fromkeys(_ACRONYM.findall(expected)) if t.lower() not in squashed]


def word_error_rate(expected: str, heard: str) -> float:
    """WER, scoring hyphens both joined and split ("sub-scored" = "subscored",
    "workspace-wide" = "workspace wide") and keeping the kinder result."""
    rates = []
    for hyphen in ("", " "):
        ref, hyp = normalize_for_wer(expected, hyphen), normalize_for_wer(heard, hyphen)
        if not ref:
            rates.append(0.0 if not hyp else 1.0)
        else:
            rates.append(float(jiwer.wer(ref, hyp)))
    return min(rates)


def audio_flags(ctx: RunContext, clip_path: Any) -> list[str]:
    cfg = ctx.config.qa
    flags = []
    samples, rate = read_mono(clip_path)
    if samples.size and float(np.mean(np.abs(samples) > 0.999)) > 0.001:
        flags.append("clipping")
    silences = ffmpeg.detect_silences(clip_path, noise_db=cfg.silence_db,
                                      min_s=cfg.max_silence_s)
    duration = samples.size / rate if samples.size else 0.0
    # Leading/trailing silence is trimmed by the fit stage; only interior gaps count.
    if any(s > 0.05 and e < duration - 0.05 for s, e in silences):
        flags.append("silence")
    return flags


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    synth = artifacts.read_json(ctx.path("synth.json"))
    entries = synth["groups"]
    groups = {gid: Group.from_entry(gid, e) for gid, e in entries.items()}
    clip_fields = set(Clip.__dataclass_fields__)
    clips = {gid: Clip(**{k: v for k, v in e.items() if k in clip_fields})
             for gid, e in entries.items()}
    voice_id = synth["voice_id"]
    pronunciations = load_pronunciations(ctx.video, cfg)
    language = (cfg.transcribe.language or cfg.synth.language.split("-")[0]) or None
    cache = FileCache(ctx.work_dir, "asr-qa")
    ctx.info(f"checking {len(clips)} clips with {cfg.models.asr_qa} "
             f"(max WER {cfg.qa.max_wer:.0%}, up to {cfg.qa.max_retries} retries)")
    ctx.reporter.set_total(STAGE, len(clips))
    results: dict[str, Result] = {}
    cost = 0.0

    async with DeepInfraClient.from_context(ctx) as client:
        tts = make_tts(client, cfg.models.tts)

        async def heard(path: Any) -> str:
            nonlocal cost
            key = make_key(cfg.models.asr_qa, language, file_sha256(path))
            raw = cache.get_json(key)
            if raw is None:
                raw = await transcribe_text(client, cfg.models.asr_qa, path, language=language)
                cache.put_json(key, raw)
                cost += float((raw.get("inference_status") or {}).get("cost") or 0.0)
            return str(raw.get("text", "")).strip()

        async def check(gid: str, clip: Clip) -> None:
            group = groups[gid]
            expected = group.text
            # Start from exactly the take synth.json records (an earlier QA run may
            # have left a retry take in tts/<id>.wav). A cache hit: no API call.
            clip = await synthesize_group(ctx, tts, group, voice_id, pronunciations, clip.seed)
            text = await heard(ctx.path(clip.file))
            best = Result(word_error_rate(expected, text), text)
            best_clip = clip
            seed = clip.seed
            def bad(r: Result) -> bool:
                return too_many_errors(expected, r.wer, cfg.qa.max_wer) \
                    or bool(missing_acronyms(expected, r.heard))

            while bad(best) and best.retries < cfg.qa.max_retries:
                ctx.check_cancelled()
                seed += 1
                take = await synthesize_group(ctx, tts, group, voice_id, pronunciations, seed)
                text = await heard(ctx.path(take.file))
                wer = word_error_rate(expected, text)
                best.retries += 1
                if wer < best.wer:
                    best.wer, best.heard, best_clip = wer, text, take
            if best_clip is not clip:
                best.take = {k: v for k, v in asdict(best_clip).items()
                             if k in ("file", "duration", "seed")}
            # The last attempt overwrote tts/<id>.wav; restore the winning take (cached).
            if best.retries and best_clip.seed != seed:
                await synthesize_group(ctx, tts, group, voice_id, pronunciations, best_clip.seed)
            if too_many_errors(expected, best.wer, cfg.qa.max_wer):
                best.flags.append("wer")
            if missing_acronyms(expected, best.heard):
                best.flags.append("acronym")
            best.flags += audio_flags(ctx, ctx.path(best_clip.file))
            results[gid] = best
            for uid in group.units:
                ctx.reporter.unit_updated(uid, "qa-flagged" if best.flags else "qa-ok")
            ctx.reporter.advance(STAGE, 1, gid)

        await asyncio.gather(*(check(gid, c) for gid, c in clips.items()))

    artifacts.write_json(ctx.path("qa.json"), {
        "model": cfg.models.asr_qa, "max_wer": cfg.qa.max_wer,
        "groups": {gid: {**asdict(r), "units": groups[gid].units}
                   for gid, r in sorted(results.items())},
    })
    flagged = [uid for uid, r in sorted(results.items()) if r.flags]
    retried = sum(1 for r in results.values() if r.retries)
    improved = sum(1 for r in results.values() if r.take)
    mean_wer = float(np.mean([r.wer for r in results.values()])) if results else 0.0
    ctx.info(f"QA: mean WER {mean_wer:.1%}; {retried} retried, {improved} improved by a retry, "
             f"{len(flagged)} flagged{': ' + ', '.join(flagged[:12]) if flagged else ''}; "
             f"ASR cost ${cost:.4f}")
