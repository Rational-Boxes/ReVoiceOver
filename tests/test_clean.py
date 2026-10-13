import asyncio
import json

import httpx
import pytest

from narration import artifacts
from narration.clients import deepinfra
from narration.clients.llm import LLMResponseError, parse_json_reply
from narration.config import load_config
from narration.context import RunContext
from narration.pipeline import Outcome, run_pipeline
from narration.stages.clean import (
    CleanValidationError,
    apply_clean,
    flags_for,
    is_pure_filler,
    validate_batch,
    word_diff_markdown,
)
from narration.types import Unit


def U(i, text):
    return Unit(id=f"u{i:04d}", start=i * 2.0, end=i * 2.0 + 1.5, slot_end=i * 2.0 + 2.0,
                raw_text=text)


def test_validate_requires_exact_ids():
    batch = [U(1, "a"), U(2, "b")]
    reply = {"units": [{"id": "u0002", "text": "B"}, {"id": "u0001", "text": "A"}]}
    ok = validate_batch(batch, reply)
    assert ok == {"u0001": "A", "u0002": "B"}
    with pytest.raises(CleanValidationError, match="missing"):
        validate_batch(batch, {"units": [{"id": "u0001", "text": "A"}]})
    with pytest.raises(CleanValidationError, match="unexpected"):
        validate_batch(batch, {"units": [{"id": "u0001", "text": "A"},
                                         {"id": "u0003", "text": "C"}]})
    with pytest.raises(CleanValidationError):
        validate_batch(batch, {"units": [{"id": "u0001", "text": "A"}] * 2 + [{"id": "u0002"}]})
    with pytest.raises(CleanValidationError):
        validate_batch(batch, {"nope": []})


def test_filler_and_flags():
    assert is_pure_filler("Um... uh,") and is_pure_filler("")
    assert not is_pure_filler("uh workspace wide.")
    assert flags_for("um, uh", "") == ["dropped"]
    assert flags_for("uh workspace wide.", "") == ["dropped-content"]
    assert flags_for("a" * 100, "a" * 20) == ["short"]
    assert flags_for("abc", "abcdef") == ["long"]
    assert flags_for("so, um, the the deploy button", "The deploy button.") == []


def test_keep_original_only_when_unchanged_and_no_fillers():
    u = U(1, "Open the settings panel.")
    apply_clean(u, "Open the settings panel.")
    assert u.keep_original
    v = U(2, "uh open the settings panel")
    apply_clean(v, "Open the settings panel.")
    assert not v.keep_original


def test_word_diff():
    md = word_diff_markdown("so um click the the deploy button", "Click the deploy button.")
    assert md.startswith("~~so um~~ Click")
    assert md.endswith("deploy button.")
    assert md.count("~~the~~") == 1
    assert word_diff_markdown("Open it.", "open it") == "open it"


def test_parse_json_reply():
    assert parse_json_reply({"choices": [{"message": {"content": '{"a": 1}'}}]}) == {"a": 1}
    fenced = '```json\n{"a": 2}\n```'
    assert parse_json_reply({"choices": [{"message": {"content": fenced}}]}) == {"a": 2}
    with pytest.raises(LLMResponseError):
        parse_json_reply({"choices": [{"message": {"content": "sure! here you go"}}]})


@pytest.fixture
def clean_ctx(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_text("v")
    (tmp_path / "glossary.txt").write_text("Green Canary\n")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    ctx.config.clean.batch_size = 2
    units = [U(1, "so um green canary is, uh, new"), U(2, "uh, um"), U(3, "Open the panel.")]
    artifacts.write_json(ctx.path("units.json"), [u.to_dict() for u in units])
    requests: list[dict] = []
    replies = {"u0001": "Green Canary is new.", "u0002": "", "u0003": "Open the panel."}
    bad_once = {"left": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        sent = json.loads(body["messages"][1]["content"])
        ids = [u["id"] for u in sent["units"]]
        if "u0003" in ids and bad_once["left"]:
            bad_once["left"] -= 1
            content = json.dumps({"units": []})  # breaks the id contract -> retry
        else:
            content = json.dumps({"units": [{"id": i, "text": replies[i]} for i in ids]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"estimated_cost": 0.001}})

    original = deepinfra.DeepInfraClient.from_context.__func__

    def from_context(cls, ctx, **kw):
        monkeypatch.setenv("DEEPINFRA_TOKEN", "t")
        return original(cls, ctx, transport=httpx.MockTransport(handler), backoff_base_s=0, **kw)

    monkeypatch.setattr(deepinfra.DeepInfraClient, "from_context", classmethod(from_context))
    return ctx, requests


def test_clean_stage_end_to_end(clean_ctx):
    ctx, requests = clean_ctx
    result = asyncio.run(run_pipeline(ctx, start="clean", stop="clean"))
    assert result.outcome is Outcome.COMPLETED, result.error
    assert len(requests) == 3  # two batches, one retried
    sent = json.loads(requests[0]["messages"][1]["content"])
    assert sent["glossary"] == ["Green Canary"]
    assert requests[0]["response_format"] == {"type": "json_object"}

    data = artifacts.read_json(ctx.path("script.json"))
    units = {u["id"]: u for u in data["units"]}
    assert units["u0001"]["clean_text"] == "Green Canary is new."
    assert units["u0002"]["flags"] == ["dropped"]
    assert units["u0003"]["keep_original"] is True
    md = ctx.path("script.md").read_text()
    assert "~~so um~~ Green Canary is ~~uh,~~ new." in md


def test_rerun_uses_cache_and_keeps_edits(clean_ctx):
    ctx, requests = clean_ctx
    asyncio.run(run_pipeline(ctx, start="clean", stop="clean"))
    data = artifacts.read_json(ctx.path("script.json"))
    data["units"][0]["clean_text"] = "My own wording."
    data["units"][0]["edited"] = True
    artifacts.write_json(ctx.path("script.json"), data)
    calls = len(requests)

    asyncio.run(run_pipeline(ctx, start="clean", stop="clean"))
    assert len(requests) == calls, "unchanged batches must come from the cache"
    units = artifacts.read_json(ctx.path("script.json"))["units"]
    assert units[0]["clean_text"] == "My own wording." and units[0]["edited"]


def test_id_mismatch_twice_fails(clean_ctx, monkeypatch):
    ctx, _ = clean_ctx
    from narration.clients import llm as llm_mod

    async def bad(self, system, user):
        return llm_mod.ChatResult({"units": []}, {})

    monkeypatch.setattr(llm_mod.ChatLLM, "complete_json", bad)
    result = asyncio.run(run_pipeline(ctx, start="clean", stop="clean"))
    assert result.outcome is Outcome.FAILED and "failed twice" in result.error
