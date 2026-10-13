"""[1] transcribe: word timestamps for every ASR chunk, merged onto the video timeline."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from narration import artifacts
from narration.cache import FileCache, file_sha256, make_key
from narration.clients.asr import ASRBackend, make_asr
from narration.clients.deepinfra import DeepInfraClient
from narration.config import project_file
from narration.context import RunContext
from narration.types import Chunk, Word

STAGE = "transcribe"
MAX_PROMPT_CHARS = 600  # Whisper's prompt window is ~224 tokens


def glossary_prompt(ctx: RunContext) -> str | None:
    """Prime Whisper with product names so it spells them right."""
    if not ctx.config.transcribe.glossary_prompt:
        return None
    path = project_file(ctx.video, ctx.config.clean.glossary)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    terms = [t.strip() for t in lines if t.strip() and not t.lstrip().startswith("#")]
    return ", ".join(terms)[:MAX_PROMPT_CHARS] or None


def merge_chunks(per_chunk: list[tuple[Chunk, list[Word]]]) -> list[Word]:
    """Shift clip-relative words by their chunk offset and keep them ordered."""
    merged: list[Word] = []
    for chunk, words in sorted(per_chunk, key=lambda cw: cw[0].offset):
        for w in words:
            start = round(w.start + chunk.offset, 3)
            end = round(w.end + chunk.offset, 3)
            if merged and start < merged[-1].end:  # never overlap the previous chunk
                start = merged[-1].end
                end = max(end, start)
            merged.append(Word(text=w.text, start=start, end=end, confidence=w.confidence))
    return merged


async def _transcribe_chunk(ctx: RunContext, asr: ASRBackend, cache: FileCache, chunk: Chunk,
                            index: int, language: str | None,
                            prompt: str | None) -> tuple[list[Word], dict[str, Any], bool]:
    audio = ctx.path(chunk.file)
    key = make_key(asr.name, language, prompt, file_sha256(audio))
    raw = cache.get_json(key)
    cached = raw is not None
    if raw is None:
        raw = await asr.transcribe_raw(audio, language=language, initial_prompt=prompt)
        cache.put_json(key, raw)
    artifacts.atomic_write_text(ctx.path("raw", f"whisper_{index:03d}.json"),
                                json.dumps(raw, indent=1, ensure_ascii=False))
    return asr.parse_words(raw), raw, cached


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    chunks = [Chunk.from_dict(c) for c in artifacts.read_json(ctx.path("chunks.json"))]
    language = cfg.transcribe.language or None
    prompt = glossary_prompt(ctx)
    if prompt:
        ctx.info(f"priming Whisper with {len(prompt.split(','))} glossary terms")
    if cfg.models.asr_text:
        ctx.warn(f"models.asr_text={cfg.models.asr_text!r} is not used yet; "
                 "transcribing with the timestamped model only")

    ctx.reporter.set_total(STAGE, len(chunks))
    cache = FileCache(ctx.work_dir, "asr")
    async with DeepInfraClient.from_context(ctx) as client:
        asr = make_asr(client, cfg.models.asr_timestamped, timeout_s=cfg.transcribe.timeout_s)

        async def one(i: int, chunk: Chunk) -> tuple[Chunk, list[Word], dict[str, Any], bool]:
            ctx.info(f"transcribing chunk {i + 1}/{len(chunks)} "
                     f"({chunk.duration:.0f}s at {chunk.offset:.0f}s) with {asr.name}")
            words, raw, cached = await _transcribe_chunk(ctx, asr, cache, chunk, i,
                                                         language, prompt)
            ctx.reporter.advance(STAGE, 1, f"chunk {i + 1}/{len(chunks)}")
            return chunk, words, raw, cached

        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))

    words = merge_chunks([(chunk, w) for chunk, w, _, _ in results])
    languages = sorted({str(raw["language"]) for _, _, raw, _ in results if raw.get("language")})
    billed = sum((raw.get("inference_status") or {}).get("cost") or 0.0
                 for _, _, raw, cached in results if not cached)
    reused = sum(1 for *_, cached in results if cached)

    artifacts.write_json(ctx.path("words.json"), {
        "model": cfg.models.asr_timestamped,
        "language": languages[0] if len(languages) == 1 else languages,
        "words": [w.to_dict() for w in words],
    })
    ctx.info(f"{len(words)} words, language {', '.join(languages) or '?'}; "
             f"cost ${billed:.4f}" + (f" ({reused} chunk(s) from cache)" if reused else ""))
