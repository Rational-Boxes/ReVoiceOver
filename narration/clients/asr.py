"""ASR adapters. Stages depend on ``ASRBackend`` only, never on a concrete model.

Observed response of ``openai/whisper-timestamped-medium`` (fixture:
tests/fixtures/whisper_timestamped_word.json):

* ``words`` is only populated when ``chunk_level=word``; otherwise it is null.
* Each word is ``{"word": " deploy,", "start": 9.27, "end": 9.99}`` — leading
  space, attached punctuation, float noise, and no per-word confidence.
* Segments carry ``avg_logprob`` / ``no_speech_prob`` but (despite the schema)
  no ``confidence``; we use ``exp(avg_logprob)`` of the containing segment.
* Within a segment, word ends run into the next word's start, so pauses are
  only visible between segments.
* ``inference_status.cost`` is the billed amount in USD.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Protocol

from narration.clients.deepinfra import DeepInfraClient
from narration.types import Word


class ASRBackend(Protocol):
    name: str

    async def transcribe_raw(self, audio: Path, *, language: str | None = None,
                             initial_prompt: str | None = None) -> dict[str, Any]: ...

    def parse_words(self, raw: dict[str, Any]) -> list[Word]: ...


def _segment_confidence(segments: list[dict[str, Any]], t: float) -> float | None:
    for seg in segments:
        if seg.get("start", 0.0) <= t <= seg.get("end", 0.0):
            if (conf := seg.get("confidence")) is not None:
                return float(conf)
            if (avg := seg.get("avg_logprob")) is not None:
                return round(math.exp(float(avg)), 4)
    return None


def parse_timestamped(raw: dict[str, Any]) -> list[Word]:
    """Normalize a whisper-timestamped response into clip-relative ``Word``s."""
    segments = raw.get("segments") or []
    out: list[Word] = []
    prev_end = 0.0
    for item in raw.get("words") or []:
        text = str(item.get("word", item.get("text", ""))).strip()
        if not text:
            continue
        start = round(max(float(item["start"]), prev_end), 3)
        end = round(max(float(item["end"]), start), 3)
        out.append(Word(text=text, start=start, end=end,
                        confidence=_segment_confidence(segments, (start + end) / 2)))
        prev_end = end
    return out


class WhisperTimestamped:
    def __init__(self, client: DeepInfraClient, model: str, *, timeout_s: float = 900.0) -> None:
        self.client = client
        self.name = model
        self.timeout_s = timeout_s

    def params(self, language: str | None, initial_prompt: str | None) -> dict[str, str]:
        data = {"task": "transcribe", "chunk_level": "word", "temperature": "0"}
        if language:
            data["language"] = language
        if initial_prompt:
            data["initial_prompt"] = initial_prompt
        return data

    async def transcribe_raw(self, audio: Path, *, language: str | None = None,
                             initial_prompt: str | None = None) -> dict[str, Any]:
        response = await self.client.request(
            "POST", f"/v1/inference/{self.name}",
            files={"audio": (audio.name, audio.read_bytes(), "audio/wav")},
            data=self.params(language, initial_prompt),
            timeout=self.timeout_s,
        )
        raw: dict[str, Any] = response.json()
        if raw.get("words") is None:
            raise ValueError(f"{self.name} returned no word timestamps")
        return raw

    def parse_words(self, raw: dict[str, Any]) -> list[Word]:
        return parse_timestamped(raw)


def make_asr(client: DeepInfraClient, model: str, *, timeout_s: float = 900.0) -> ASRBackend:
    if "timestamped" in model:
        return WhisperTimestamped(client, model, timeout_s=timeout_s)
    raise ValueError(f"{model}: only whisper-timestamped models provide word timings")


async def transcribe_text(client: DeepInfraClient, model: str, audio: Path, *,
                          language: str | None = None, timeout_s: float = 300.0) -> dict[str, Any]:
    """Plain transcription (any Whisper model); the text is in ``raw["text"]``."""
    data = {"language": language} if language else {}
    response = await client.request(
        "POST", f"/v1/inference/{model}",
        files={"audio": (audio.name, audio.read_bytes(), "audio/wav")},
        data=data, timeout=timeout_s,
    )
    raw: dict[str, Any] = response.json()
    return raw
