"""TTS adapters. Stages depend on ``TTSBackend`` only.

Observed DeepInfra behaviour (see tests/fixtures/zonos_response.json):

* ``POST /v1/voices/add`` (multipart ``name``, ``description``, ``files``)
  returns ``{"voice_id": ...}``. Voices belong to the account, not to a model.
* ``Zyphra/Zonos-v0.1-hybrid`` takes ``text``, ``voice_id``, ``language``,
  ``output_format``, ``speaker_rate`` (5-35) and ``seed``. ``Audio8`` has no
  ``voice_id`` parameter, so it cannot use a cloned voice.
* ``audio`` comes back as a base64 data URI string.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Protocol

from narration.clients.deepinfra import DeepInfraClient

VOICES_PATH = "/v1/voices/add"


class TTSBackend(Protocol):
    name: str

    async def register_voice(self, ref_wav: Path, label: str) -> str: ...

    async def synthesize(self, text: str, voice_id: str, *, rate: float | None = None,
                         seed: int | None = None, language: str = "en-us") -> bytes: ...


def decode_audio(value: Any) -> bytes:
    """Decode the ``audio`` field: data URI, bare base64, or raw bytes."""
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("TTS response has no audio")
    if value.startswith("data:"):
        value = value.split(",", 1)[1]
    return base64.b64decode(value)


async def register_voice(client: DeepInfraClient, ref_wav: Path, label: str) -> str:
    reply = await client.post_multipart(
        VOICES_PATH,
        files={"files": (ref_wav.name, ref_wav.read_bytes(), "audio/wav")},
        data={"name": label, "description": "ReVoiceOver reference voice"},
    )
    voice_id = reply.get("voice_id") if isinstance(reply, dict) else None
    if not voice_id:
        raise ValueError(f"voice registration returned no voice_id: {str(reply)[:200]}")
    return str(voice_id)


class ZonosTTS:
    def __init__(self, client: DeepInfraClient, model: str) -> None:
        self.client = client
        self.name = model
        self.last_raw: dict[str, Any] = {}

    async def register_voice(self, ref_wav: Path, label: str) -> str:
        return await register_voice(self.client, ref_wav, label)

    def payload(self, text: str, voice_id: str, rate: float | None, seed: int | None,
                language: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "voice_id": voice_id, "language": language,
                                "output_format": "wav"}
        if rate is not None:
            body["speaker_rate"] = rate
        if seed is not None:
            body["seed"] = seed
        return body

    async def synthesize(self, text: str, voice_id: str, *, rate: float | None = None,
                         seed: int | None = None, language: str = "en-us") -> bytes:
        raw = await self.client.inference(self.name,
                                          self.payload(text, voice_id, rate, seed, language))
        self.last_raw = {k: v for k, v in raw.items() if k != "audio"}
        return decode_audio(raw.get("audio"))


def make_tts(client: DeepInfraClient, model: str) -> TTSBackend:
    if model.startswith("Zyphra/Zonos"):
        return ZonosTTS(client, model)
    raise ValueError(f"{model}: no TTS adapter with voice cloning (supported: Zyphra/Zonos-*)")
