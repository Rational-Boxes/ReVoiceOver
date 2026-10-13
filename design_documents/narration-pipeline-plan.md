# Narration Rebuild Pipeline: Implementation Plan

Take a screen recording (or any video with voiceover), transcribe it, clean the
script with an LLM, regenerate the narration in a cloned voice, and remux it onto
the original video. All model inference runs on DeepInfra using open models;
media work runs locally with ffmpeg.

Working name: `narration-rebuild` (CLI command `narr`).

---

## 1. Goals and non-goals

**Goals**

- One command from `raw.mp4` to `clean.mp4` with regenerated narration.
- Every stage writes its output to disk so runs are resumable and inspectable.
- A human review checkpoint between script cleanup and synthesis.
- Pluggable model backends (swap Whisper, LLM, or TTS model by config only).
- Cheap: a 10 minute video should cost cents, not dollars.

**Non-goals (for v1)**

- Lip sync for on-camera footage. This targets screencasts and B-roll voiceover.
- Multi-speaker videos. Single narrator only; diarization can come later.
- A GUI. The review step edits a JSON/Markdown file.

---

## 2. Model choices on DeepInfra

All model IDs live in `config.py` so they can be swapped without code changes.
Verify request/response schemas on each model's "API" tab before wiring the
adapter; the adapters exist precisely so that drift stays contained.

| Stage | Default model | Why | Notes |
|---|---|---|---|
| Word timestamps | `openai/whisper-timestamped-medium` | Per-word start/end times, needed to anchor segments | Probe the raw response once and save it as a fixture; the docs example only shows segment-level output |
| High-accuracy text (optional) | `openai/whisper-large-v3-turbo` (`openai/whisper-large` is no longer listed) | Better transcription text | Use its text, keep timestamped-medium's timing, reconcile in `transcribe.py` |
| Script cleanup | A strong open instruct model (Qwen, DeepSeek, or Llama family) via the OpenAI-compatible endpoint | JSON output, low temperature, cheap | Pick from the current DeepInfra catalog; keep it configurable |
| Voice clone TTS | `Zyphra/Zonos-v0.1-hybrid` | Open weights, few-second reference cloning, speaking-rate control, native 44 kHz output, about $7 per 1M characters | Speaking-rate control helps the timing-fit stage |
| Alternate TTS | `Audio8/Audio8-TTS-Preview-0.6b` | 0.6B multilingual, zero-shot cloning, about $5 per 1M characters | Preview model; keep as fallback / A-B comparison |

Both TTS options support DeepInfra's custom voice registration
(`POST https://api.deepinfra.com/v1/voices/add` with an audio file, returns a
`voice_id`), so the voice is registered once and reused.

**Cost sketch for a 10 minute narration** (about 1,500 words, about 9,000
characters): TTS around $0.06 on Zonos, plus small change for Whisper and the
LLM pass. Re-synthesizing failed segments a few times does not move the needle.

---

## 3. Pipeline overview

```
raw.mp4
  │
  ├─ [0] extract   ffmpeg → audio_16k.wav, audio_full.wav, probe.json
  ├─ [1] transcribe  Whisper (word timestamps) → words.json
  ├─ [2] segment   words → narration units with time anchors → units.json
  ├─ [3] clean     LLM rewrite per unit → script.json  (+ script.md for review)
  │        ── HUMAN REVIEW CHECKPOINT (edit script.json, rerun) ──
  ├─ [4] voice     pick clean reference clip, register voice → voice.json
  ├─ [5] synth     TTS per unit → tts/<unit_id>.wav  (content-hash cached)
  ├─ [6] qa        re-transcribe each TTS clip, compare to script → qa.json
  ├─ [7] fit       fit each clip into its time slot → fitted/<unit_id>.wav, timeline.json
  └─ [8] assemble  build narration track, mux with video → clean.mp4, clean.srt
```

All artifacts live in a per-video work directory: `work/<video-stem>/`.
Each stage reads the previous stage's file and writes its own, so
`narr run raw.mp4 --from clean` resumes after manual edits.

---

## 4. Stage details

### [0] extract

- `ffprobe` for duration, frame rate, audio sample rate → `probe.json`.
- `ffmpeg -i raw.mp4 -vn -ac 1 -ar 16000 audio_16k.wav` for ASR.
- `ffmpeg -i raw.mp4 -vn -ac 1 -ar 44100 audio_full.wav` for voice reference.
- Long videos: split ASR audio into roughly 10 minute chunks at silence points
  (`silencedetect`), track each chunk's offset, and shift timestamps back on merge.

### [1] transcribe

- POST each chunk to the timestamped Whisper endpoint.
- Normalize into a single internal shape regardless of model:

  ```python
  @dataclass
  class Word:
      text: str
      start: float   # seconds, absolute in source video
      end: float
      confidence: float | None
  ```

- Save the raw API response alongside (`raw/whisper_<chunk>.json`) for debugging.
- Known quirk: Whisper often silently drops "um"/"uh". That is fine here because
  we regenerate the narration anyway; we care about the content words and the
  gaps between them.

### [2] segment

Group words into **narration units**, the atomic unit for LLM cleanup, TTS and
timing.

- Split on pauses longer than `pause_split_s` (default 0.6 s) or on sentence
  punctuation.
- Cap units at `max_unit_s` (default about 20 s) to stay within TTS context limits
  and keep timing drift local.
- Each unit records `id`, `start`, `end`, `slot_end` (start of the next unit, which
  is the time budget available), and `raw_text`.

```python
@dataclass
class Unit:
    id: str            # "u0001"
    start: float
    end: float
    slot_end: float    # next unit's start, or video end
    raw_text: str
    clean_text: str | None = None
    keep_original: bool = False   # used by patch mode, see section 6
```

### [3] clean (LLM)

- Use DeepInfra's OpenAI-compatible chat endpoint via the `openai` SDK
  (`base_url="https://api.deepinfra.com/v1/openai"`).
- Send units in batches of about 40 as JSON: `[{"id": "u0001", "text": "..."}]`.
- Ask for JSON back with **the same ids, one output per input, no merging or
  splitting across ids**. This is what keeps every sentence anchored to its spot
  in the video.
- System prompt rules:
  - Remove fillers, stutters, false starts, self-corrections and repeated phrases.
  - Keep the speaker's meaning, terminology and tone; do not add new facts.
  - Keep product names and technical terms exactly as written (supply a
    `glossary.txt` that is injected into the prompt).
  - Spell out things the TTS will mispronounce (version numbers, CLI flags) per a
    small `pronunciations.toml` map, applied after the LLM pass, not by the LLM.
- Temperature 0 to 0.2, `response_format={"type": "json_object"}` where supported.
- **Validation** (fail loudly, retry the batch once):
  - Same id set in and out.
  - Length ratio `len(clean)/len(raw)` between 0.4 and 1.1; outside that, flag it.
  - Empty output only allowed if the raw unit was pure filler (then the unit is
    dropped and its slot becomes silence).
- Write `script.json` plus a human-friendly `script.md` showing raw vs clean per
  unit with a word diff.

**Review checkpoint:** the run stops here by default (`--no-review` skips it).
Edit `clean_text` in `script.json`, then `narr run raw.mp4 --from voice`.

### [4] voice

- Pick a reference clip automatically: the longest stretch of 10 to 15 s inside a
  single unit with high word confidence, no detected fillers, and good loudness.
  Allow override with `--ref path/to/clip.wav`.
- Denoise lightly (`ffmpeg -af afftdn,loudnorm`) before upload.
- Register via `/v1/voices/add`, store `{model, voice_id, ref_sha256}` in
  `voice.json`. Reuse across videos by keeping a global `~/.config/narr/voices.json`
  keyed by reference hash, so you clone yourself once.

### [5] synth

- For each unit with `clean_text`, call the TTS adapter.
- Cache key: `sha256(model + voice_id + clean_text + params)` →
  `cache/tts/<key>.wav`. Editing one sentence re-synthesizes only that unit.
- Normalize every clip to 44.1 kHz mono, `loudnorm` to a fixed target (for example
  -16 LUFS) so units match each other.
- Run requests concurrently with a small semaphore (4 to 8) and exponential
  backoff on 429/5xx, all handled in the shared client.

### [6] qa

TTS models occasionally skip, repeat or invent words. Catch it automatically:

- Re-transcribe each TTS clip with Whisper (non-timestamped is fine).
- Compute word error rate against `clean_text` (`jiwer`).
- If WER exceeds `qa_max_wer` (default 0.15), re-synthesize with a different seed
  up to N times, then flag in `qa.json` for manual attention.
- Also flag clips with long internal silences or clipping.

### [7] fit (timing)

Each unit has a slot: `slot_end - start`. The cleaned narration is usually
shorter than the original, which leaves two strategies, selected by `--timing`:

**`anchor` (default, safest for screencasts):** every unit starts exactly where
the original speech started, so narration stays synced to what is on screen.

- Clip shorter than slot: pad with silence.
- Clip longer by up to 15 %: speed up with `atempo` (or request a faster
  speaking rate from Zonos and re-synthesize, which sounds better).
- Clip longer by more: stretch that slice of video with `setpts` to make room,
  and record the stretch in `timeline.json`.

**`compact`:** also remove dead air from the video. Units are packed back to back
with a fixed gap (for example 0.35 s), and the video is cut into the matching
segments. Produces a tighter video, but on-screen actions that happened during
pauses get trimmed, so it suits talking-over-slides better than live demos.

Output: `timeline.json` listing, per unit, the source video range and the output
range, plus the fitted audio file.

### [8] assemble

- Build the narration track by placing fitted clips on the timeline (numpy
  buffer or an ffmpeg `adelay` + `amix` filter graph).
- `anchor` with no stretches: `ffmpeg -i raw.mp4 -i narration.wav -map 0:v -map 1:a -c:v copy -c:a aac clean.mp4` (no video re-encode).
- With stretches or `compact`: generate an ffmpeg concat/filter script from
  `timeline.json` and re-encode video once.
- Emit `clean.srt` from the cleaned script and output timings (free captions).
- Optional `--bed original.wav --bed-gain -30dB` to keep faint room tone and
  avoid sterile silence.

---

## 5. Project layout

Keep every module well under 500 lines; each stage is its own module with a
single `run(ctx) -> None` entry point.

```
narration-rebuild/
├── pyproject.toml
├── README.md
├── narration/
│   ├── __init__.py
│   ├── cli.py            # typer CLI: run, stage, voices, clean-cache
│   ├── config.py         # pydantic settings: model IDs, thresholds, env token
│   ├── context.py        # RunContext: work dir paths, config, logger
│   ├── types.py          # Word, Unit, TimelineEntry dataclasses + (de)serialization
│   ├── artifacts.py      # read/write stage JSON, schema version, atomic writes
│   ├── cache.py          # content-hash file cache (shared by synth and qa)
│   ├── clients/
│   │   ├── deepinfra.py  # ONE http client: auth, retries, backoff, concurrency
│   │   ├── asr.py        # ASR adapter interface + Whisper implementations
│   │   ├── llm.py        # chat adapter (OpenAI-compatible) with JSON validation
│   │   └── tts.py        # TTS adapter interface + Zonos / Audio8 implementations
│   ├── media/
│   │   ├── ffmpeg.py     # thin, typed ffmpeg/ffprobe wrappers (only place that shells out)
│   │   └── audio.py      # loudnorm, padding, atempo, silence detection helpers
│   └── stages/
│       ├── extract.py
│       ├── transcribe.py
│       ├── segment.py
│       ├── clean.py
│       ├── voice.py
│       ├── synth.py
│       ├── qa.py
│       ├── fit.py
│       └── assemble.py
├── prompts/
│   └── clean_system.md   # LLM system prompt, versioned in git
└── tests/
    ├── fixtures/         # saved raw Whisper/LLM responses, tiny test clip
    ├── test_segment.py
    ├── test_clean_validation.py
    ├── test_fit.py
    └── test_timeline.py
```

**DRY boundaries**

- All network calls go through `clients/deepinfra.py` (auth header, retry,
  backoff, rate limiting, request logging). Adapters only shape payloads.
- All subprocess calls go through `media/ffmpeg.py`.
- All stage I/O goes through `artifacts.py`, so stage files share one schema
  version and atomic-write behavior.
- `cache.py` is used by both `synth` (TTS clips) and `qa` (re-transcriptions).
- Adapters follow small Protocols so stages never import a concrete model:

```python
class TTSBackend(Protocol):
    name: str
    def register_voice(self, ref_wav: Path, label: str) -> str: ...
    def synthesize(self, text: str, voice_id: str, *, rate: float = 1.0,
                   seed: int | None = None) -> bytes: ...
```

---

## 6. Optional "patch" mode

A hybrid that often sounds most natural: keep units that were already clean in
your real voice, and regenerate only the messy ones.

- In `clean`, mark `keep_original=True` when the cleaned text equals the raw text
  (after normalizing case and punctuation) and no fillers were detected.
- In `assemble`, those units use the original audio slice instead of TTS.
- Cheaper, and most of the video stays authentically you.
- Risk: audible timbre or room-tone shifts between real and synthetic units. The
  `--bed` room tone and a short crossfade (20 to 40 ms) at unit boundaries help.
  Worth an A/B listen against full regeneration on your first few videos.

---

## 7. Configuration

`narr.toml` (per project) layered over `~/.config/narr/config.toml`:

```toml
[models]
asr_timestamped = "openai/whisper-timestamped-medium"
asr_text        = "openai/whisper-large"      # optional, "" to disable
llm             = "<pick from DeepInfra catalog>"
tts             = "Zyphra/Zonos-v0.1-hybrid"

[segment]
pause_split_s = 0.6
max_unit_s    = 20

[clean]
batch_size  = 40
temperature = 0.1
glossary    = "glossary.txt"

[fit]
timing        = "anchor"   # or "compact"
max_atempo    = 1.15
compact_gap_s = 0.35

[qa]
max_wer     = 0.15
max_retries = 3
```

Token from `DEEPINFRA_TOKEN` env var only; never written to work dirs or logs.

---

## 8. Dependencies

- Python 3.11+
- `httpx` (async client), `openai` (chat endpoint), `pydantic` + `pydantic-settings`,
  `typer`, `numpy`, `soundfile`, `jiwer`, `rich` (progress/logging)
- Dev: `pytest`, `ruff`, `mypy`
- System: `ffmpeg` and `ffprobe`. On Fedora, the stock `ffmpeg-free` package lacks
  some codecs (notably H.264 encoding); install `ffmpeg` from RPM Fusion if you
  need to re-encode MP4 output.

---

## 9. Build order and commit plan

Each milestone is runnable and ends with a detailed commit describing what was
built, what was verified, and what comes next.

1. **Scaffold**: `pyproject.toml`, package layout, `config.py`, `context.py`,
   `artifacts.py`, CLI skeleton with `narr run` dispatching stubbed stages.
   Commit: project skeleton, stage registry, artifact conventions.
2. **Shared clients + media layer**: `clients/deepinfra.py` with retry/backoff,
   `media/ffmpeg.py`, `extract` stage. Commit includes a smoke test on a sample clip.
3. **ASR**: probe the timestamped Whisper response, save fixtures, implement
   `transcribe` with chunking and offset merging. Commit notes the exact response
   schema discovered.
4. **Segment**: pure-Python unit builder with tests on fixture words.
5. **Clean**: prompt file, LLM adapter with JSON validation and retries,
   `script.md` diff output, review checkpoint. Commit records the prompt version.
6. **Voice + synth**: voice registration, reference-clip picker, TTS adapter for
   Zonos, content-hash cache, concurrency. Commit includes an A/B note vs Audio8.
7. **QA**: re-transcription WER check with retry/flagging.
8. **Fit + assemble (anchor mode)**: silence padding, atempo, video stretch,
   mux, SRT export. First end-to-end `clean.mp4`.
9. **Compact mode and patch mode**, room-tone bed, crossfades.
10. **Hardening**: `--from` resume, `clean-cache`, README with a worked example,
    type checks and CI.

---

## 10. Open questions to settle early

- ~~Exact JSON shape of word timestamps from `whisper-timestamped-medium`~~
  Answered in milestone 3 (see `narration/clients/asr.py` and
  `tests/fixtures/whisper_timestamped_word.json`): words only come back with
  `chunk_level=word`, as a top-level `words` list of `{word, start, end}`
  with a leading space, attached punctuation and no confidence. Segments have
  `avg_logprob` but no `confidence`. Pauses are only visible between
  segments. Tokens such as `.0` or `/workspaces` come back as separate words
  and must be joined without a space. Billing is per second of audio. A
  572 s recording cost $0.43, as reported in `inference_status.cost`.
- ~~Whether Zonos's speaking-rate control is exposed through DeepInfra's API~~
  Answered in milestone 6: yes, as `speaker_rate` (5-35), alongside
  `voice_id`, `language`, `output_format` and `seed`. Audio comes back as a
  base64 WAV data URI (44.1 kHz mono). `words` is empty.
- ~~Whether DeepInfra's registered voices are scoped per model~~ Answered:
  `/v1/voices/add` takes no model, so voices belong to the account. The global
  registry is keyed only by the reference clip's sha256. Note that
  `Audio8-TTS-Preview-0.6b` has no `voice_id` parameter, so it cannot be a
  cloned-voice fallback.
- Preferred default timing mode for your typical videos: live demos favor
  `anchor`; slide narration favors `compact`.
