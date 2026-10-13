import asyncio
import json

import pytest

from narration import artifacts, stages
from narration.config import load_config
from narration.context import RunContext
from narration.pipeline import Outcome, run_pipeline
from narration.stages.extract import plan_chunks


def test_plan_chunks_short_audio_is_one_chunk():
    assert plan_chunks(100.0, [], chunk_s=600, search_s=60) == [(0.0, 100.0)]


def test_plan_chunks_cuts_at_nearest_silence():
    silences = [(570.0, 571.0), (615.0, 616.0), (1190.0, 1192.0)]
    spans = plan_chunks(1500.0, silences, chunk_s=600, search_s=60)
    assert spans == [(0.0, 615.5), (615.5, 1191.0), (1191.0, 1500.0)]


def test_plan_chunks_hard_cut_without_silence():
    spans = plan_chunks(1300.0, [], chunk_s=600, search_s=60)
    assert spans == [(0.0, 600.0), (600.0, 1300.0)]


def test_extract_on_real_clip(demo_video, tmp_path):
    ctx = RunContext(video=demo_video, work_dir=tmp_path / "w", config=load_config())
    result = asyncio.run(run_pipeline(ctx, stop="extract"))
    assert result.outcome is Outcome.COMPLETED
    for name in ("audio_16k.wav", "audio_full.wav"):
        assert ctx.path(name).stat().st_size > 1000
    probe = artifacts.read_json(ctx.path("probe.json"))
    assert float(probe["format"]["duration"]) == pytest.approx(8.0, abs=0.2)
    chunks = artifacts.read_json(ctx.path("chunks.json"))
    assert chunks == [{"file": "audio_16k.wav", "offset": 0.0,
                       "duration": pytest.approx(8.0, abs=0.2)}]
    assert stages.project_status(ctx.video, ctx.work_dir)["extract"] is stages.Status.DONE


def test_extract_chunks_long_audio(demo_video, tmp_path):
    cfg = load_config()
    # Force chunking on the 8 s clip (bypassing the gt=30 validator): target 3.5 s
    # chunks; the clip has a 1 s gap at 3.5-4.5 s.
    cfg.extract.__dict__["chunk_s"] = 3.5
    cfg.extract.chunk_search_s = 2.0
    ctx = RunContext(video=demo_video, work_dir=tmp_path / "w", config=cfg)
    asyncio.run(run_pipeline(ctx, stop="extract"))
    chunks = artifacts.read_json(ctx.path("chunks.json"))
    assert len(chunks) == 2
    assert chunks[1]["offset"] == pytest.approx(4.0, abs=0.3)
    assert all(ctx.path(c["file"]).exists() for c in chunks)
    assert json.dumps(chunks)  # serializable


def test_extract_cancel(demo_video, tmp_path):
    ctx = RunContext(video=demo_video, work_dir=tmp_path / "w", config=load_config())
    ctx.cancel.set()
    result = asyncio.run(run_pipeline(ctx, stop="extract"))
    assert result.outcome is Outcome.CANCELLED
    assert stages.project_status(ctx.video, ctx.work_dir)["extract"] is stages.Status.MISSING


def test_bundled_ffmpeg_comes_before_path(tmp_path, monkeypatch):
    from narration.media import ffmpeg

    (tmp_path / "ffprobe").write_text("")
    monkeypatch.setenv("NARR_FFMPEG_DIR", str(tmp_path))
    ffmpeg._binary.cache_clear()
    try:
        assert ffmpeg._binary("ffprobe") == str(tmp_path / "ffprobe")
        assert ffmpeg._binary("ffmpeg") != str(tmp_path / "ffmpeg")  # not bundled: PATH
    finally:
        ffmpeg._binary.cache_clear()
