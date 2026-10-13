import asyncio
import io

import numpy as np
import pytest
import soundfile as sf

from narration import artifacts
from narration.config import Fit as FitConfig
from narration.config import load_config
from narration.context import RunContext
from narration.media import ffmpeg
from narration.pipeline import Outcome, run_pipeline
from narration.stages.assemble import format_srt_time, srt, unit_cues, video_filter
from narration.stages.fit import merge_segments, plan_anchor
from narration.types import Unit


def U(i, start, end, slot_end, text="Text."):
    return Unit(f"u{i:04d}", start, end, slot_end, "raw", text, text)


def test_plan_pad_speedup_and_stretch():
    units = [U(1, 1.0, 2.5, 4.0), U(2, 4.0, 5.0, 6.0), U(3, 6.0, 7.0, 8.0, ""),
             U(4, 8.0, 9.0, 10.0)]
    clips = {"u0001": 2.0,   # fits its 3 s slot: padded
             "u0002": 2.2,   # 10% over its 2 s slot: sped up
             "u0004": 3.0}   # 50% over: max speed-up, then the video stretches
    entries, segments = plan_anchor(units, clips, 10.0, FitConfig(max_atempo=1.15))
    e = {x.unit_id: x for x in entries}
    assert set(e) == {"u0001", "u0002", "u0004"}          # dropped unit has no audio
    assert e["u0001"].atempo == 1.0 and e["u0001"].out_start == 1.0
    assert e["u0002"].atempo == pytest.approx(1.1) and e["u0002"].video_stretch == 1.0
    assert e["u0004"].atempo == pytest.approx(1.15)
    out_slot = 3.0 / 1.15
    assert e["u0004"].video_stretch == pytest.approx(out_slot / 2.0, abs=1e-3)
    assert e["u0004"].out_start == 8.0                    # nothing before it stretched
    assert e["u0004"].out_end == pytest.approx(8.0 + out_slot, abs=1e-3)
    # Unstretched stretches merge into one segment; the stretched slot stays separate.
    assert [(s.src_start, s.src_end) for s in segments] == [(0.0, 8.0), (8.0, 10.0)]
    assert segments[-1].out_end == pytest.approx(8.0 + out_slot, abs=1e-3)


def test_stretch_shifts_everything_after_it():
    units = [U(1, 0.0, 1.0, 1.0), U(2, 1.0, 2.0, 3.0)]
    entries, segments = plan_anchor(units, {"u0001": 2.0, "u0002": 1.0}, 3.0,
                                    FitConfig(max_atempo=1.0))
    assert entries[1].out_start == pytest.approx(2.0)   # u0001 needed 1 s more
    assert segments[-1].out_end == pytest.approx(4.0)
    assert merge_segments(segments) == segments


def test_srt_and_filter():
    assert format_srt_time(3725.5) == "01:02:05,500"
    entries, _ = plan_anchor([U(1, 0.5, 1.5, 2.0)], {"u0001": 1.0}, 2.0, FitConfig())
    groups = {"u0001": {"units": ["a", "b"], "unit_texts": {"a": "Hi.", "b": "Hello you."}}}
    cues = unit_cues(entries, groups)
    assert [(round(a, 3), round(b, 3), t) for a, b, t in cues] == [
        (0.5, 0.731, "Hi."), (0.731, 1.5, "Hello you.")]
    assert srt(cues[:1]) == "1\n00:00:00,500 --> 00:00:00,731\nHi.\n"
    f = video_filter([{"src_start": 0, "src_end": 1, "out_start": 0, "out_end": 1},
                      {"src_start": 1, "src_end": 2, "out_start": 1, "out_end": 3}])
    assert "setpts=PTS-STARTPTS[v0]" in f and "*2.000000" in f and "concat=n=2" in f


def _tts_like(seconds: float, lead: float = 0.3) -> np.ndarray:
    """A clip with silence around it, like Zonos output."""
    rate = 44100
    tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(int(seconds * rate)) / rate)
    pad = np.zeros(int(lead * rate))
    return np.concatenate([pad, tone, pad]).astype(np.float32)


@pytest.mark.parametrize("stretch", [False, True])
def test_fit_and_assemble_end_to_end(demo_video, tmp_path, stretch):
    if stretch and ffmpeg.h264_encoder() is None:
        pytest.skip("no H.264 encoder")
    ctx = RunContext(video=demo_video, work_dir=tmp_path / "w", config=load_config())
    asyncio.run(run_pipeline(ctx, stop="extract"))
    units = [U(1, 0.5, 2.5, 3.5, "One."), U(2, 3.5, 4.0, 4.5, ""), U(3, 4.5, 6.0, 8.0, "Two.")]
    artifacts.write_json(ctx.path("script.json"), {"units": [u.to_dict() for u in units]})
    ctx.path("tts").mkdir()
    long_clip = 5.0 if stretch else 1.5  # u0001's group slot runs 0.5-4.5 s
    sf.write(ctx.path("tts", "u0001.wav"), _tts_like(long_clip), 44100)
    sf.write(ctx.path("tts", "u0003.wav"), _tts_like(1.0), 44100)
    def group(uid, start, end, slot_end, text, seconds):
        return {"file": f"tts/{uid}.wav", "duration": seconds + 0.6, "chars": len(text),
                "seed": 42, "cached": False, "text": text, "units": [uid], "start": start,
                "end": end, "slot_end": slot_end, "unit_texts": {uid: text}}

    # Clips are named after their unit here; real groups are g0001...
    artifacts.write_json(ctx.path("synth.json"), {"voice_id": "v", "groups": {
        "u0001": group("u0001", 0.5, 2.5, 4.5, "One.", long_clip),
        "u0003": group("u0003", 4.5, 6.0, 8.0, "Two.", 1.0)}})
    artifacts.write_json(ctx.path("qa.json"), {"groups": {}})

    result = asyncio.run(run_pipeline(ctx, start="fit"))
    assert result.outcome is Outcome.COMPLETED, result.error
    timeline = artifacts.read_json(ctx.path("timeline.json"))
    first = timeline["entries"][0]
    # Silence around the clip was trimmed before fitting.
    assert sf.info(str(ctx.path(first["audio"]))).duration < long_clip + 0.2
    out = ffmpeg.probe(ctx.path("clean.mp4"))
    assert out.has_video and out.has_audio
    if stretch:
        assert first["video_stretch"] > 1.0
        assert out.duration == pytest.approx(timeline["output_duration"], abs=0.25)
        assert out.duration > 8.15  # stretched beyond the 8 s source (10 fps rounding)
    else:
        assert first["atempo"] == 1.0 and first["video_stretch"] == 1.0
        assert out.duration == pytest.approx(8.0, abs=0.25)
    cues = ctx.path("clean.srt").read_text()
    assert cues.count("-->") == 2 and "One." in cues and "Two." in cues
    track, _ = sf.read(io.BytesIO(ctx.path("narration.wav").read_bytes()))
    assert np.abs(track[: int(0.4 * 44100)]).max() < 1e-3   # silent before u0001 starts
