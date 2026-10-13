import asyncio
import importlib

import pytest

from narration import stages
from narration.config import load_config
from narration.context import RunContext
from narration.pipeline import Outcome, run_pipeline


@pytest.fixture
def fake_stages(monkeypatch, tmp_path):
    """Replace every stage with one that writes its declared outputs."""
    calls: list[str] = []
    video = tmp_path / "v.mp4"
    video.write_text("v")

    def make(stage):
        def run(ctx):
            calls.append(stage.name)
            for out in stage.outputs(ctx.video, ctx.work_dir):
                out.write_text(stage.name)
        return run

    for stage in stages.STAGES:
        mod = importlib.import_module(f"narration.stages.{stage.module}")
        monkeypatch.setattr(mod, "run", make(stage))
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    return ctx, calls


def test_pauses_for_review(fake_stages):
    ctx, calls = fake_stages
    result = asyncio.run(run_pipeline(ctx))
    assert result.outcome is Outcome.REVIEW and result.last_stage == "clean"
    assert calls == ["extract", "transcribe", "segment", "clean"]

    result = asyncio.run(run_pipeline(ctx, start="voice"))
    assert result.outcome is Outcome.COMPLETED
    assert calls[-1] == "assemble"
    assert set(stages.project_status(ctx.video, ctx.work_dir).values()) == {stages.Status.DONE}


def test_no_review_runs_through(fake_stages):
    ctx, calls = fake_stages
    assert asyncio.run(run_pipeline(ctx, review=False)).outcome is Outcome.COMPLETED
    assert len(calls) == len(stages.STAGES)


def test_failure_records_manifest(fake_stages, monkeypatch):
    ctx, _ = fake_stages

    def boom(ctx):
        raise ValueError("bad words")

    monkeypatch.setattr(importlib.import_module("narration.stages.segment"), "run", boom)
    result = asyncio.run(run_pipeline(ctx))
    assert result.outcome is Outcome.FAILED and result.last_stage == "segment"
    st = stages.project_status(ctx.video, ctx.work_dir)
    assert st["segment"] is stages.Status.FAILED
    assert "bad words" in stages.stage_error(ctx.work_dir, "segment")


def test_async_stage_supported(fake_stages, monkeypatch):
    ctx, calls = fake_stages

    async def arun(ctx):
        await asyncio.sleep(0)
        calls.append("async-transcribe")
        ctx.path("words.json").write_text("w")

    monkeypatch.setattr(importlib.import_module("narration.stages.transcribe"), "run", arun)
    asyncio.run(run_pipeline(ctx, stop="transcribe"))
    assert "async-transcribe" in calls


def test_bad_range(fake_stages):
    ctx, _ = fake_stages
    with pytest.raises(ValueError):
        asyncio.run(run_pipeline(ctx, start="qa", stop="segment"))


def test_stub_stage_reports_not_implemented(fake_stages, monkeypatch):
    ctx, _ = fake_stages
    stub = importlib.reload(importlib.import_module("narration.stages.clean"))
    monkeypatch.setattr(stub, "run", stub.run)
    result = asyncio.run(run_pipeline(ctx))
    assert result.outcome is Outcome.NOT_IMPLEMENTED and result.last_stage == "clean"
