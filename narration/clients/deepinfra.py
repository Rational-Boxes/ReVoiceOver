"""The one HTTP client for DeepInfra: auth, retries, backoff, concurrency, cancel.

Model adapters (asr/llm/tts) only shape payloads and parse responses; every
request goes through ``DeepInfraClient.request``.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Any

import httpx

from narration.context import Cancelled, RunContext
from narration.credentials import mask, require_token

BASE_URL = "https://api.deepinfra.com"
OPENAI_BASE_URL = f"{BASE_URL}/v1/openai"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class DeepInfraError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"DeepInfra HTTP {status}: {mask(body)[:500]}")
        self.status = status


class DeepInfraClient:
    def __init__(
        self,
        token: str,
        *,
        concurrency: int = 6,
        max_retries: int = 5,
        timeout_s: float = 120.0,
        cancel: threading.Event | None = None,
        log: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base_s: float = 1.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._cancel = cancel or threading.Event()
        self._log = log
        self._backoff_base_s = backoff_base_s

    @classmethod
    def from_context(cls, ctx: RunContext, **kw: Any) -> DeepInfraClient:
        net = ctx.config.network
        return cls(
            require_token(),
            concurrency=net.concurrency,
            max_retries=net.max_retries,
            timeout_s=net.timeout_s,
            cancel=ctx.cancel,
            log=ctx,
            **kw,
        )

    async def __aenter__(self) -> DeepInfraClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so cancellation is noticed promptly."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if self._cancel.is_set():
                raise Cancelled()
            await asyncio.sleep(min(0.2, remaining))

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.replace(".", "", 1).isdigit():
                return min(float(retry_after), 60.0)
        return self._backoff_base_s * (2**attempt) * (0.5 + random.random())

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        attempt = 0
        while True:
            if self._cancel.is_set():
                raise Cancelled()
            response: httpx.Response | None = None
            async with self._sem:
                started = time.monotonic()
                try:
                    response = await self._http.request(method, path, **kw)
                    failure = (
                        None if response.status_code not in RETRY_STATUS
                        else f"HTTP {response.status_code}"
                    )
                except httpx.TransportError as exc:
                    failure = f"{type(exc).__name__}: {exc}"
            if self._log is not None:
                status = response.status_code if response is not None else "-"
                self._log.debug(
                    f"{method} {path} -> {status} in {time.monotonic() - started:.2f}s"
                )
            if failure is None and response is not None:
                if response.is_error:
                    raise DeepInfraError(response.status_code, response.text)
                return response
            if attempt >= self._max_retries:
                if response is not None:
                    raise DeepInfraError(response.status_code, response.text)
                raise DeepInfraError(0, failure or "transport error")
            delay = self._delay(attempt, response)
            if self._log is not None:
                self._log.warn(f"{method} {path}: {failure}; retry {attempt + 1} in {delay:.1f}s")
            await self._sleep(delay)
            attempt += 1

    async def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        return (await self.request("POST", path, json=payload)).json()

    async def post_multipart(
        self, path: str, *, files: dict[str, Any], data: dict[str, Any] | None = None
    ) -> Any:
        return (await self.request("POST", path, files=files, data=data or {})).json()

    async def inference(self, model: str, payload: dict[str, Any]) -> Any:
        """POST to the native inference endpoint ``/v1/inference/<model>``."""
        return await self.post_json(f"/v1/inference/{model}", payload)
