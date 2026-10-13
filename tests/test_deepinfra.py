import asyncio
import threading

import httpx
import pytest

from narration.clients.deepinfra import DeepInfraClient, DeepInfraError
from narration.context import Cancelled


def _client(handler, **kw):
    return DeepInfraClient("secret-token", transport=httpx.MockTransport(handler),
                           backoff_base_s=0.0, **kw)


def test_retries_then_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        assert request.headers["authorization"] == "Bearer secret-token"
        if len(attempts) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with _client(handler) as c:
            return await c.inference("m/x", {"a": 1})

    assert asyncio.run(go()) == {"ok": True}
    assert len(attempts) == 3


def test_client_error_not_retried_and_token_masked(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_TOKEN", "secret-token")
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401, text="bad token secret-token")

    async def go():
        async with _client(handler) as c:
            await c.inference("m/x", {})

    with pytest.raises(DeepInfraError) as err:
        asyncio.run(go())
    assert err.value.status == 401 and "secret-token" not in str(err.value)
    assert len(attempts) == 1


def test_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(429, text="slow down")

    async def go():
        async with _client(handler, max_retries=2) as c:
            await c.inference("m/x", {})

    with pytest.raises(DeepInfraError) as err:
        asyncio.run(go())
    assert err.value.status == 429


def test_cancel_stops_requests():
    cancel = threading.Event()
    cancel.set()

    async def go():
        async with _client(lambda r: httpx.Response(200, json={}), cancel=cancel) as c:
            await c.inference("m/x", {})

    with pytest.raises(Cancelled):
        asyncio.run(go())
