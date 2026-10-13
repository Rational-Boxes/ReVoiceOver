import asyncio
import base64
import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from narration import artifacts
from narration.clients import deepinfra
from narration.clients.tts import decode_audio
from narration.config import Voice as VoiceConfig
from narration.config import load_config
from narration.context import RunContext
from narration.pipeline import Outcome, run_pipeline
from narration.stages.synth import tts_text
from narration.stages.voice import find_candidates, load_registry
from narration.types import Unit, Word

FIXTURES = Path(__file__).parent / "fixtures"


def wav_bytes(seconds: float, rate: int = 44100, amp: float = 0.3) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    buf = io.BytesIO()
    sf.write(buf, (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32), rate, format="WAV")
    return buf.getvalue()


def test_decode_audio_matches_recorded_shape():
    shape = json.loads((FIXTURES / "zonos_response.json").read_text())
    assert shape["audio"].startswith("data:audio/wav;base64,")
    assert shape["words"] == [] and shape["output_format"] == "wav"
    data = wav_bytes(0.1)
    assert decode_audio("data:audio/wav;base64," + base64.b64encode(data).decode()) == data
    assert decode_audio(base64.b64encode(data).decode()) == data
    with pytest.raises(ValueError):
        decode_audio(None)


def test_tts_text_pronunciations():
    fixes = {"LEED": "leed", "LEED 5.0": "leed five point oh", "AI": "A.I."}
    assert tts_text("LEED 5.0 and LEED 4.1 use AI, not AIA.", fixes) == \
        "leed five point oh and leed 4.1 use A.I., not AIA."


def words_from(spec):
    """spec: list of (text, start, end)."""
    return [Word(t, s, e, 0.9) for t, s, e in spec]


def test_candidates_avoid_fillers_and_long_pauses():
    cfg = VoiceConfig(min_ref_s=4, max_ref_s=8, max_gap_s=0.5)
    clean = [(f"w{i}", 10 + i * 0.5, 10 + i * 0.5 + 0.45) for i in range(14)]  # 7 s
    filler = [("um", 0.0, 0.3)] + [(f"x{i}", 0.4 + i * 0.5, 0.85 + i * 0.5) for i in range(3)]
    gappy = [(f"g{i}", 30 + i * 1.2, 30.4 + i * 1.2) for i in range(8)]  # 0.8 s pauses
    cands = find_candidates(words_from(filler + clean + gappy), cfg)
    assert cands and cands[0].start >= 10 and cands[0].end <= 17
    assert all(not c.text.startswith("um") for c in cands)
    assert all(c.start < 30 for c in cands)


@pytest.fixture
def tts_ctx(tmp_path, monkeypatch, demo_video):
    ctx = RunContext(video=demo_video, work_dir=tmp_path / "w", config=load_config())
    asyncio.run(run_pipeline(ctx, stop="extract"))
    words = [Word(f"word{i}", 0.2 + i * 0.4, 0.55 + i * 0.4, 0.9) for i in range(18)]
    artifacts.write_json(ctx.path("words.json"), {"words": [w.to_dict() for w in words]})
    units = [Unit("u0001", 0.2, 3.0, 4.0, "um hello", "Hello.", "Hello."),
             Unit("u0002", 4.0, 7.0, 8.0, "uh", "", "", flags=["dropped"])]
    artifacts.write_json(ctx.path("script.json"), {"units": [u.to_dict() for u in units]})
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/voices/add":
            assert b'name="files"' in request.content
            return httpx.Response(200, json={"voice_id": "v123", "name": "x"})
        body = json.loads(request.content)
        assert body["voice_id"] == "v123" and body["seed"] == 42
        audio = base64.b64encode(wav_bytes(1.5)).decode()
        return httpx.Response(200, json={"audio": f"data:audio/wav;base64,{audio}",
                                         "words": [], "output_format": "wav"})

    original = deepinfra.DeepInfraClient.from_context.__func__

    def from_context(cls, ctx, **kw):
        monkeypatch.setenv("DEEPINFRA_TOKEN", "t")
        return original(cls, ctx, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(deepinfra.DeepInfraClient, "from_context", classmethod(from_context))
    ctx.config.voice.min_ref_s, ctx.config.voice.max_ref_s = 3.0, 8.0
    return ctx, calls


def test_voice_then_synth(tts_ctx):
    ctx, calls = tts_ctx
    result = asyncio.run(run_pipeline(ctx, start="voice", stop="synth"))
    assert result.outcome is Outcome.COMPLETED, result.error
    voice = artifacts.read_json(ctx.path("voice.json"))
    assert voice["voice_id"] == "v123" and not voice["reused"]
    assert ctx.path("voice_ref.wav").exists()
    assert load_registry()[voice["ref_sha256"]]["voice_id"] == "v123"

    synth = artifacts.read_json(ctx.path("synth.json"))
    assert list(synth["groups"]) == ["g0001"]
    assert synth["groups"]["g0001"]["units"] == ["u0001"]  # dropped unit is not spoken
    assert synth["unit_groups"] == {"u0001": "g0001"}
    clip = synth["groups"]["g0001"]
    assert clip["duration"] == pytest.approx(1.5, abs=0.05)
    assert ctx.path(clip["file"]).exists()
    assert calls.count("/v1/voices/add") == 1

    # A second run reuses both the registered voice and the cached clip.
    asyncio.run(run_pipeline(ctx, start="voice", stop="synth"))
    assert calls.count("/v1/voices/add") == 1
    assert len([c for c in calls if "Zonos" in c]) == 1
    assert artifacts.read_json(ctx.path("voice.json"))["reused"] is True


def test_plan_groups_follows_sentences_and_pauses():
    from narration.config import Synth as SynthConfig
    from narration.stages.synth import plan_groups

    def unit(i, start, end, text):
        return Unit(f"u{i:04d}", start, end, end + 0.1, "raw", text, text)

    units = [
        unit(1, 0.0, 2.0, "We retain the login screen with"),   # fragment ...
        unit(2, 4.5, 6.0, "all your workspaces."),             # ... 2.5 s later: joined
        unit(3, 6.5, 8.0, "It is fast."),                      # 0.5 s after a sentence
        unit(4, 9.5, 11.0, "Now open a project."),             # 1.5 s after a sentence
        unit(5, 11.2, 11.6, ""),                               # dropped
        unit(6, 11.7, 14.0, "Then click deploy."),       # 0.7 s after a sentence
        unit(7, 30.0, 32.0, "Done."),
    ]
    units[-1].slot_end = 40.0
    groups = plan_groups(units, SynthConfig())
    assert [g.units for g in groups] == [["u0001", "u0002", "u0003"], ["u0004", "u0006"],
                                         ["u0007"]]
    assert groups[0].text == "We retain the login screen with all your workspaces. It is fast."
    assert [(g.start, g.slot_end) for g in groups] == [(0.0, 9.5), (9.5, 30.0), (30.0, 40.0)]

    capped = plan_groups(units, SynthConfig(group_max_s=5.0))
    assert all(g.end - g.start <= 5.0 for g in capped)
