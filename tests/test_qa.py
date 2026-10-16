import asyncio
import base64
import io
import json

import httpx
import numpy as np
import pytest
import soundfile as sf

from narration import artifacts
from narration.cache import file_sha256
from narration.clients import deepinfra
from narration.config import load_config
from narration.context import RunContext
from narration.media.audio import mute
from narration.pipeline import Outcome, run_pipeline
from narration.stages.qa import (
    audio_flags,
    noise_spans,
    normalize_for_wer,
    speech_spans,
    too_many_errors,
    word_error_rate,
)
from narration.stages.synth import current_clips, unit_clips
from narration.types import Unit


def test_normalization():
    assert normalize_for_wer("Date: October 12th, 2026!") == "date october 12 2026"
    assert word_error_rate("sub-scored", "subscored") == 0.0
    assert word_error_rate("Workspace wide.", "workspace-wide.") == 0.0
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("AIA", "") == 1.0


def test_missing_acronyms():
    from narration.stages.qa import missing_acronyms

    assert missing_acronyms("AIA Materials Pledge Compliance.", "materials pledge compliance") \
        == ["AIA"]
    assert missing_acronyms("the USGBC and A EPDs", "the U.S.G.B.C. and epds") == []
    assert missing_acronyms("Ask the AIA", "ask the A I A") == []
    assert missing_acronyms("No acronyms here.", "") == []


def test_flag_rule():
    assert not too_many_errors("We'll pull all candidate", 0.25, 0.15)   # one word off
    assert too_many_errors("that the USGBC expects as", 0.4, 0.15)       # two of five
    assert too_many_errors("AIA", 1.0, 0.15)                             # nothing heard
    long = " ".join(["word"] * 20)
    assert not too_many_errors(long, 0.15, 0.15)  # 3 errors of 20 is at the limit
    assert too_many_errors(long, 0.2, 0.15)


def W(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_speech_spans_skip_the_voices_own_noises():
    words = [W(" *Crickets", 0.0, 0.5), W("*", 0.5, 0.5), W(" Bye.", 1.0, 1.3),
             W(" Acme", 2.0, 2.3), W(" Cloud", 2.3, 2.7), W(" is", 2.7, 2.8),
             W(" live.", 2.8, 3.2), W(" you", 4.0, 4.4)]
    # "Cloud" is unscripted ("AcmeCloud") but runs on from scripted words.
    assert speech_spans("AcmeCloud is live.", words) == \
        [(2.0, 2.3), (2.3, 2.7), (2.7, 2.8), (2.8, 3.2)]
    assert speech_spans("Anything.", []) == []


def test_noise_spans_and_mute():
    cfg = load_config().qa
    rate = 1000
    samples = np.full(5 * rate, 0.3, dtype=np.float32)     # audible throughout
    samples[int(3.6 * rate):int(4.0 * rate)] = 0.0         # a quiet stretch
    speech = [(1.0, 2.0), (2.0, 3.0), (4.5, 4.6)]
    spans = noise_spans(samples, rate, speech, cfg)
    # Before the speech, the pause after "3.0" (audible part), and the tail.
    assert spans == [(0.0, 0.9), (3.25, 4.4), (4.85, 5.0)]
    assert noise_spans(samples, rate, [], cfg) == []        # nothing recognized: leave it
    quiet = np.zeros_like(samples)
    assert noise_spans(quiet, rate, speech, cfg) == []

    out = mute(samples, rate, spans)
    assert np.all(out[:int(0.89 * rate)] == 0)             # clip start: no fade needed
    assert out[int(3.25 * rate)] == pytest.approx(0.3)     # fades out from full level
    assert np.all(out[int(3.3 * rate):int(4.35 * rate)] == 0)
    assert np.all(out[int(1.0 * rate):int(3.0 * rate)] == pytest.approx(0.3))


def test_audio_flags():
    cfg = load_config().qa
    rate = 1000
    samples = np.full(5 * rate, 0.3, dtype=np.float32)
    assert audio_flags(samples, rate, cfg) == []
    samples[1000:3000] = 0                                 # 2 s gap inside
    assert audio_flags(samples, rate, cfg) == ["silence"]
    samples[:] = 0
    samples[4000:] = 1.0                                   # leading silence is fine
    assert audio_flags(samples, rate, cfg) == ["clipping"]


def wav_bytes(seconds, amp=0.3, gap=None):
    rate = 44100
    t = np.arange(int(seconds * rate)) / rate
    y = amp * np.sin(2 * np.pi * 220 * t)
    if gap:
        y[int(gap[0] * rate):int(gap[1] * rate)] = 0
    buf = io.BytesIO()
    sf.write(buf, y.astype(np.float32), rate, format="WAV")
    return buf.getvalue()


@pytest.fixture
def qa_ctx(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_text("v")
    ctx = RunContext(video=video, work_dir=tmp_path / "w", config=load_config())
    ctx.config.qa.max_silence_s = 0.5  # the "It is saved." takes contain a 0.8 s gap
    ctx.config.synth.group_sentence_gap_s = 0.0  # one unit per speech group here
    units = [Unit("u0001", 0, 2, 2.5, "open the panel", "Open the panel.", "Open the panel."),
             Unit("u0002", 2.5, 4, 5, "click deploy now", "Click deploy now.", "x"),
             Unit("u0003", 5, 7, 8, "it is saved", "It is saved.", "x")]
    artifacts.write_json(ctx.path("script.json"), {"units": [u.to_dict() for u in units]})
    artifacts.write_json(ctx.path("voice.json"), {"voice_id": "v1"})
    # What the "ASR" hears for each seed of each unit's take.
    heard = {("Open the panel.", 42): "open the panel",
             ("Click deploy now.", 42): "", ("Click deploy now.", 43): "click deploy now",
             ("It is saved.", 42): "it", ("It is saved.", 43): "it",
             ("It is saved.", 44): "it", ("It is saved.", 45): "it"}
    # The "Open the panel." take (1.43 s of tone) has a noise after its words.
    words = {("Open the panel.", 42): [W(" Open", 0.0, 0.3), W(" the", 0.3, 0.4),
                                       W(" panel.", 0.4, 0.8), W(" Hmm.", 1.2, 1.4)]}
    clips_by_audio: dict[bytes, tuple[str, int]] = {}
    calls = {"tts": 0, "asr": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "Zonos" in request.url.path:
            calls["tts"] += 1
            body = json.loads(request.content)
            # Distinct audio per (text, seed) so the cache and ASR can tell takes apart.
            seconds = 1.0 + body["seed"] / 100 + len(body["text"]) / 1000
            audio = wav_bytes(seconds, gap=(0.1, 0.9) if body["text"] == "It is saved." else None)
            return httpx.Response(200, json={
                "audio": "data:audio/wav;base64," + base64.b64encode(audio).decode()})
        calls["asr"] += 1
        # Identify the clip by its duration (set above from seed and text).
        data, rate = sf.read(io.BytesIO(_upload(request)))
        seconds = len(data) / rate
        match = min(heard, key=lambda k: abs(1.0 + k[1] / 100 + len(k[0]) / 1000 - seconds))
        return httpx.Response(200, json={"text": heard[match], "words": words.get(match),
                                         "inference_status": {"cost": 0.001}})

    original = deepinfra.DeepInfraClient.from_context.__func__

    def from_context(cls, ctx, **kw):
        monkeypatch.setenv("DEEPINFRA_TOKEN", "t")
        return original(cls, ctx, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(deepinfra.DeepInfraClient, "from_context", classmethod(from_context))
    return ctx, calls, clips_by_audio


def _upload(request: httpx.Request) -> bytes:
    body = request.content
    start = body.index(b"RIFF")
    end = body.rindex(b"\r\n--")
    return body[start:end]


def test_qa_retries_keeps_best_take_and_flags(qa_ctx):
    ctx, calls, _ = qa_ctx
    assert asyncio.run(run_pipeline(ctx, start="synth", stop="synth")).outcome \
        is Outcome.COMPLETED
    result = asyncio.run(run_pipeline(ctx, start="qa", stop="qa"))
    assert result.outcome is Outcome.COMPLETED, result.error

    qa = artifacts.read_json(ctx.path("qa.json"))["groups"]
    assert qa["g0001"]["wer"] == 0 and qa["g0001"]["flags"] == [] and qa["g0001"]["retries"] == 0
    assert qa["g0001"]["units"] == ["u0001"]
    assert qa["g0001"]["noise"] == [[1.05, 1.435]]         # "Hmm." is muted by fit
    assert qa["g0001"]["noise_audio"] == file_sha256(ctx.path("tts", "g0001.wav"))
    assert qa["g0002"]["noise"] == []                      # no word timings: left alone
    assert qa["g0002"]["retries"] == 1 and qa["g0002"]["take"]["seed"] == 43
    assert qa["g0002"]["flags"] == []
    assert qa["g0003"]["retries"] == 3 and "wer" in qa["g0003"]["flags"]
    assert "silence" in qa["g0003"]["flags"]

    clips = current_clips(ctx.work_dir)
    assert clips["g0002"]["seed"] == 43            # QA's take wins over synth.json
    assert clips["g0001"]["seed"] == 42
    assert unit_clips(ctx.work_dir)["u0002"]["seed"] == 43

    # Re-running QA is free: every take and transcript is cached.
    before = dict(calls)
    asyncio.run(run_pipeline(ctx, start="qa", stop="qa"))
    assert calls == before
    assert artifacts.read_json(ctx.path("qa.json"))["groups"]["g0002"]["take"]["seed"] == 43
