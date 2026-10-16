"""[6] qa: re-transcribe every speech group's clip and catch skipped/invented words.

A clip whose word error rate against its script text exceeds ``qa.max_wer`` is
re-synthesized with new seeds (up to ``qa.max_retries``); the best take wins.
Clips that stay bad, or have long silences or clipping, are flagged for review.

The voice also makes noises of its own (squeaks, breaths, a stray "no") before,
between or after sentences. Whisper's word timings show where the script is
spoken; whatever is audible elsewhere is recorded in qa.json as ``noise`` spans,
which the fit stage mutes.
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
from narration.config import QA as QAConfig
from narration.context import RunContext
from narration.media.audio import mute, read_mono
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
    # [start, end] seconds of the take to mute, and the sha256 of the take they belong to.
    noise: list[list[float]] = field(default_factory=list)
    noise_audio: str = ""


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


FRAME_S = 0.01
JOIN_S = 0.3  # an unscripted word this close to spoken script is part of the speech


def frame_peaks_db(samples: np.ndarray, rate: int) -> np.ndarray:
    """Peak level of each 10 ms frame, in dBFS."""
    hop = max(1, int(rate * FRAME_S))
    n = samples.size // hop
    peaks = np.abs(samples[: n * hop]).reshape(n, hop).max(axis=1) if n else np.zeros(0)
    return 20 * np.log10(peaks + 1e-9)


def speech_spans(expected: str, words: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Where the script is spoken: heard words that align with script words.

    Unscripted words count only when they run on from scripted ones (Whisper
    splits "AI-based" or "AcmeCloud"). Isolated ones ("Bye.", "*Squeak*", a
    lone "you") are the voice's noises, as are words with no letters at all.
    """
    ref = normalize_for_wer(expected).split()
    tokens: list[str] = []
    owner: list[int] = []
    for i, w in enumerate(words):
        for t in normalize_for_wer(str(w.get("word", ""))).split():
            tokens.append(t)
            owner.append(i)
    if not ref or not tokens:
        return []
    kept: set[int] = set()
    for chunk in jiwer.process_words(" ".join(ref), " ".join(tokens)).alignments[0]:
        if chunk.type in ("equal", "substitute"):
            kept.update(owner[j] for j in range(chunk.hyp_start_idx, chunk.hyp_end_idx))
    voiced = set(owner)
    order = list(range(len(words)))
    for seq in (order, order[::-1]):  # spread forwards, then backwards
        last: int | None = None
        for i in seq:
            if i in kept:
                last = i
            elif last is not None and i in voiced and \
                    _gap(words[last], words[i]) <= JOIN_S:
                kept.add(i)
                last = i
    return [(float(words[i]["start"]), float(words[i]["end"])) for i in sorted(kept)]


def _gap(a: dict[str, Any], b: dict[str, Any]) -> float:
    first, second = sorted((a, b), key=lambda w: float(w["start"]))
    return max(0.0, float(second["start"]) - float(first["end"]))


def noise_spans(samples: np.ndarray, rate: int, speech: list[tuple[float, float]],
                cfg: QAConfig) -> list[tuple[float, float]]:
    """Audible stretches outside the (padded) speech spans.

    Nothing is muted when no speech was recognized: then the WER check fails
    the clip and a person should listen.
    """
    if not speech or not samples.size:
        return []
    duration = samples.size / rate
    db = frame_peaks_db(samples, rate)
    gaps, t = [], 0.0
    for start, end in sorted(speech):
        if start - cfg.noise_pad_before_s > t:
            gaps.append((t, start - cfg.noise_pad_before_s))
        t = max(t, end + cfg.noise_pad_after_s)
    if t < duration:
        gaps.append((t, duration))
    out = []
    for a, b in gaps:
        frames = db[int(a / FRAME_S):int(np.ceil(b / FRAME_S))]
        if frames.size and float(frames.max()) > cfg.noise_db:
            out.append((round(a, 3), round(min(b, duration), 3)))
    return out


def audio_flags(samples: np.ndarray, rate: int, cfg: QAConfig) -> list[str]:
    flags = []
    if samples.size and float(np.mean(np.abs(samples) > 0.999)) > 0.001:
        flags.append("clipping")
    # Leading/trailing silence is trimmed by the fit stage; only interior gaps count.
    quiet = frame_peaks_db(samples, rate) < cfg.silence_db
    edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(np.int8), [0]))))
    for first, last in zip(edges[::2], edges[1::2], strict=True):
        if first > 0 and last < quiet.size and (last - first) * FRAME_S >= cfg.max_silence_s:
            flags.append("silence")
            break
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

        async def heard(path: Any) -> tuple[str, list[dict[str, Any]]]:
            """What Whisper hears in a clip, and the word timings."""
            nonlocal cost
            key = make_key(cfg.models.asr_qa, language, "words", file_sha256(path))
            raw = cache.get_json(key)
            if raw is None:
                raw = await transcribe_text(client, cfg.models.asr_qa, path,
                                            language=language, words=True)
                cache.put_json(key, raw)
                cost += float((raw.get("inference_status") or {}).get("cost") or 0.0)
            return str(raw.get("text", "")).strip(), list(raw.get("words") or [])

        async def check(gid: str, clip: Clip) -> None:
            group = groups[gid]
            expected = group.text
            # Start from exactly the take synth.json records (an earlier QA run may
            # have left a retry take in tts/<id>.wav). A cache hit: no API call.
            clip = await synthesize_group(ctx, tts, group, voice_id, pronunciations, clip.seed)
            text, words = await heard(ctx.path(clip.file))
            best = Result(word_error_rate(expected, text), text)
            best_clip, best_words = clip, words
            seed = clip.seed
            def bad(r: Result) -> bool:
                return too_many_errors(expected, r.wer, cfg.qa.max_wer) \
                    or bool(missing_acronyms(expected, r.heard))

            while bad(best) and best.retries < cfg.qa.max_retries:
                ctx.check_cancelled()
                seed += 1
                take = await synthesize_group(ctx, tts, group, voice_id, pronunciations, seed)
                text, words = await heard(ctx.path(take.file))
                wer = word_error_rate(expected, text)
                best.retries += 1
                if wer < best.wer:
                    best.wer, best.heard, best_clip, best_words = wer, text, take, words
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
            path = ctx.path(best_clip.file)
            samples, rate = read_mono(path)
            if cfg.qa.mute_noise:
                spans = noise_spans(samples, rate, speech_spans(expected, best_words), cfg.qa)
                if spans:
                    best.noise = [[a, b] for a, b in spans]
                    best.noise_audio = file_sha256(path)
                    samples = mute(samples, rate, spans)
            best.flags += audio_flags(samples, rate, cfg.qa)
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
    noisy = [gid for gid, r in sorted(results.items()) if r.noise]
    muted = sum(b - a for r in results.values() for a, b in r.noise)
    ctx.info(f"QA: mean WER {mean_wer:.1%}; {retried} retried, {improved} improved by a retry, "
             f"{len(flagged)} flagged{': ' + ', '.join(flagged[:12]) if flagged else ''}; "
             + (f"noises muted in {len(noisy)} clips ({muted:.1f}s): "
                f"{', '.join(noisy[:12])}; " if noisy else "")
             + f"ASR cost ${cost:.4f}")
