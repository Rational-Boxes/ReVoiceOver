"""Chat-completions adapter on DeepInfra's OpenAI-compatible endpoint.

Goes through ``DeepInfraClient`` (retries, cancellation, logging) rather than
the openai SDK so all network traffic shares one code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from narration.clients.deepinfra import DeepInfraClient

CHAT_PATH = "/v1/openai/chat/completions"


class LLMResponseError(ValueError):
    """The model answered, but not with the JSON we asked for."""


@dataclass(frozen=True)
class ChatResult:
    data: Any
    raw: dict[str, Any]

    @property
    def cost(self) -> float:
        usage = self.raw.get("usage") or {}
        return float(usage.get("estimated_cost") or 0.0)


class ChatLLM:
    def __init__(self, client: DeepInfraClient, model: str, *, temperature: float = 0.1,
                 max_tokens: int = 8192) -> None:
        self.client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    async def complete_json(self, system: str, user: str) -> ChatResult:
        raw = await self.client.post_json(CHAT_PATH, self.payload(system, user))
        return ChatResult(parse_json_reply(raw), raw)


def parse_json_reply(raw: dict[str, Any]) -> Any:
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMResponseError(f"unexpected chat response shape: {exc}") from None
    text = (content or "").strip()
    if text.startswith("```"):  # tolerate fenced JSON
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"model did not return JSON: {exc}: {text[:200]!r}") from None
