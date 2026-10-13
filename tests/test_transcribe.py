import asyncio
import json
import math
import os
import shutil
from pathlib import Path

import httpx
import pytest

from narration import artifacts, credentials, stages
from narration.clients import deepinfra
from narration.clients.asr import parse_timestamped
from narration.config import load_config
from narration.context import RunContext
from narration.pipeline import Outcome, run_pipeline
from narration.stages.transcribe import glossary_prompt, merge_chunks
from narration.types import Chunk, Word

FIXTURES = Path(__file__).parent / "fixtures"
RAW = json.loads((FIXTURES / "whisper_timestamped_word.json").read_text())
REAL_DOTENV_TOKEN = credentials.dotenv_token  # captured before conftest stubs it


def test_parse_fixture():
    words = parse_timestamped(RAW)
    assert len(words) == 28
    assert [w.text for w in words[:3]] == ["So,", "today", "I"]
    assert words[-1].text == "button."
    assert all(a.end <= b.start for a, b in zip(words, words[1:], strict=False))
    assert words[0].confidence == pytest.approx(math.exp(-0.257478941996981), abs=1e-3)
    # The pause between the two sentences survives.
    gap = words[10].start - words[9].end
    assert words[9].text == "dashboard." and gap > 2.0


def test_parse_rejects_nothing_but_handles_empty():
    assert parse_timestamped({"segments": [], "words": [{"word": " ", "start": 0, "end": 1}]}) == []


def test_merge_chunks_shifts_and_orders():
    a = Chunk("a.wav", 0.0, 10.0)
    b = Chunk("b.wav", 10.0, 10.0)
    merged = merge_chunks([
        (b, [Word("later", 0.5, 1.0)]),
        (a, [Word("first", 1.0, 2.0), Word("edge", 9.8, 10.7)]),
    ])
    assert [(w.text, w.start, w.end) for w in merged] == [
        ("first", 1.0, 2.0), ("edge", 9.8, 10.7), ("later", 10.7, 11.0)]


def test_glossary_prompt(tmp_path):
    video = tmp_path / "v.mp4"
    video.touch()
    (tmp_path / "glossary.txt").write_text("# products\nReVoiceOver\n\nDeepInfra\n")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    assert glossary_prompt(ctx) == "ReVoiceOver, DeepInfra"
    ctx.config.transcribe.glossary_prompt = False
    assert glossary_prompt(ctx) is None


@pytest.fixture
def chunked_ctx(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_text("v")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    shutil.copy(FIXTURES / "messy_flite.wav", ctx.path("audio_16k.wav"))
    artifacts.write_json(ctx.path("chunks.json"),
                         [Chunk("audio_16k.wav", 0.0, 12.755).to_dict()])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=RAW)

    original = deepinfra.DeepInfraClient.from_context.__func__

    def from_context(cls, ctx, **kw):
        monkeypatch.setenv("DEEPINFRA_TOKEN", "test-token")
        return original(cls, ctx, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(deepinfra.DeepInfraClient, "from_context", classmethod(from_context))
    return ctx, calls


def test_stage_writes_words_and_caches(chunked_ctx):
    ctx, calls = chunked_ctx
    result = asyncio.run(run_pipeline(ctx, start="transcribe", stop="transcribe"))
    assert result.outcome is Outcome.COMPLETED
    assert len(calls) == 1
    body = calls[0].content
    assert b'name="chunk_level"' in body and b"word" in body
    assert calls[0].url.path == "/v1/inference/openai/whisper-timestamped-medium"

    data = artifacts.read_json(ctx.path("words.json"))
    assert data["language"] == "en" and len(data["words"]) == 28
    assert ctx.path("raw", "whisper_000.json").exists()
    assert "cost $0.0096" in ctx.path("run.log").read_text()

    asyncio.run(run_pipeline(ctx, start="transcribe", stop="transcribe"))
    assert len(calls) == 1, "second run must come from the cache"
    assert "from cache" in ctx.path("run.log").read_text()
    assert stages.stage_status(stages.get("transcribe"), ctx.video, ctx.work_dir) \
        is stages.Status.DONE


def test_stage_without_token_fails_cleanly(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_text("v")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    artifacts.write_json(ctx.path("chunks.json"), [])
    result = asyncio.run(run_pipeline(ctx, start="transcribe", stop="transcribe"))
    assert result.outcome is Outcome.FAILED and "No DeepInfra token" in result.error


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("NARR_LIVE") != "1", reason="set NARR_LIVE=1 (costs ~1¢)")
def test_live_whisper(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "dotenv_token", REAL_DOTENV_TOKEN)
    video = tmp_path / "v.mp4"
    video.write_text("v")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    shutil.copy(FIXTURES / "messy_flite.wav", ctx.path("audio_16k.wav"))
    artifacts.write_json(ctx.path("chunks.json"), [Chunk("audio_16k.wav", 0.0, 12.755).to_dict()])
    result = asyncio.run(run_pipeline(ctx, start="transcribe", stop="transcribe"))
    assert result.outcome is Outcome.COMPLETED, result.error
    words = artifacts.read_json(ctx.path("words.json"))["words"]
    assert any("deploy" in w["text"].lower() for w in words)
