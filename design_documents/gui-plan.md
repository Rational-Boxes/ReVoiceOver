# ReVoiceOver GUI: Implementation Plan

A wxPython desktop front end for the narration rebuild pipeline described in
`narration-pipeline-plan.md`. That plan lists "A GUI" as a v1 non-goal; this
document reverses that decision. It does **not** change the pipeline design. The
GUI is a second front end on the same headless core, next to the `narr` CLI.

Working name: `narr-gui` (entry point), package `narration.gui`.

---

## 1. Goals and non-goals

**Goals**

- Drag in `raw.mp4`, watch the stages run, review the script, and export
  `clean.mp4`, all without touching JSON files.
- Make the **human review checkpoint** the center of the app. Reviewing the
  script is the step that benefits most from a UI: raw vs. clean with a diff,
  listening to the original slice, editing, and re-synthesizing one unit.
- Make timing visible. A timeline shows each unit's slot, how long its TTS clip
  is, and whether it was padded, sped up (`atempo`) or needed a video stretch.
- Keep the UI responsive at all times. Nothing that touches the network or
  ffmpeg runs on the UI thread, and every run can be cancelled.
- Keep it native. Use stock wx widgets that follow the GTK theme (including dark
  mode), with HiDPI-safe SVG icons.

**Non-goals (for v1)**

- No frame-accurate video editor. The video preview only lets you check sync.
- No reimplementation of pipeline logic in the GUI. If the GUI needs a
  capability (for example "re-synth one unit"), it goes into the core library
  first and the CLI gets it as well.
- No multi-project batch queue. One project window per video. Several windows
  can be open at once.

---

## 2. Technology choices

| Concern | Choice | Notes |
|---|---|---|
| Toolkit | wxPython 4.2.x (Phoenix) | On Fedora use the `python3-wxpython4` RPM. The PyPI sdist builds from source on Linux, which takes a long time and needs GTK dev headers. Create the venv with `--system-site-packages` so it can see the RPM. |
| Layout | `wx.aui.AuiManager` | Panels can be docked, resized and rearranged, and the layout is saved with `SavePerspective()`. |
| Lists/tables | `wx.dataview.DataViewCtrl` + custom `DataViewModel` | Virtual, so it handles 1,000+ units. Supports sorting and custom renderers for status icons. |
| Text editing / diff | `wx.stc.StyledTextCtrl` | Word-level diff highlighting through styles or indicators, plus spell-check-style squiggles for glossary terms. |
| Settings | `wx.propgrid.PropertyGrid` | Generated from the pydantic config schema, so new config keys show up without GUI changes. |
| Timeline / waveform | Custom `wx.Panel` drawn with `wx.GraphicsContext` on a `wx.AutoBufferedPaintDC` | Peaks are precomputed with numpy and cached as `.npy` files in the work dir. |
| Audio playback | `sounddevice` + `soundfile` | `wx.adv.Sound` is unreliable on GTK. `sounddevice` uses PortAudio, which works on PipeWire. |
| Video preview | `wx.media.MediaCtrl` (GStreamer backend) | Optional panel. If the backend is missing, fall back to opening the file with `wx.LaunchDefaultApplication`. |
| Icons | Bundled SVGs loaded with `wx.BitmapBundle.FromSVGFile` | Sharp at any DPI scale. |
| Dark mode | `wx.SystemSettings.GetAppearance().IsDark()` | Custom-drawn panels (timeline, diff colors) choose colors from a small palette module. |

New dependencies: `wxPython` (system package), `sounddevice`, and optionally
`keyring` (see section 8). GUI tests need `xvfb-run` (from `xorg-x11-server-Xvfb`).

---

## 3. Changes the core needs first

These are small additions to the pipeline plan. They make the core usable from a
GUI, and the CLI uses them too.

1. **Progress reporting protocol** (`narration/progress.py`). Stages call into a
   `ProgressReporter` instead of using `rich` directly:

   ```python
   class ProgressReporter(Protocol):
       def stage_started(self, stage: str, total: int | None) -> None: ...
       def advance(self, stage: str, n: int = 1, detail: str = "") -> None: ...
       def unit_updated(self, unit_id: str, status: str) -> None: ...
       def log(self, level: int, msg: str) -> None: ...
       def stage_finished(self, stage: str, ok: bool) -> None: ...
   ```

   `RichReporter` is the CLI implementation and `WxReporter` is the GUI one.
   `RunContext` carries the reporter.

2. **Cancellation.** `RunContext.cancel: threading.Event`. Stages check it between
   units and between batches. The shared DeepInfra client checks it before each
   request and between retries. A cancelled stage raises `Cancelled` and leaves
   its finished per-unit outputs in the cache, so the next run picks up where it
   stopped.

3. **Stage registry metadata.** Each stage declares `name`, `inputs` (artifact
   paths) and `outputs`. `artifacts.py` records an `inputs_digest` in each
   output. With that, `project_status(work_dir) -> dict[stage, Status]` can return
   `missing | done | stale | failed` without running anything. The GUI's stage
   list and the CLI's `narr status` both use this.

4. **Unit-level operations** as library functions, not just whole stages:
   `synth.synthesize_unit(ctx, unit_id, seed=None)`,
   `qa.check_unit(ctx, unit_id)` and `fit.refit(ctx)`. They make the "edit,
   re-synth, listen" loop in the review panel fast.

5. **Config round-trip.** `config.save_project(path, cfg)` writes `narr.toml` and
   only writes keys that differ from the user-level defaults.

---

## 4. Threading model

```
UI thread (wx MainLoop)                Worker thread (one per project window)
───────────────────────                ─────────────────────────────────────
PipelineRunner.start(from, to) ──────► asyncio.run(run_stages(ctx, ...))
                                         │  stages → ProgressReporter calls
WxReporter ◄── wx.CallAfter / wx.PostEvent ┘
  ├─ StagesPanel.update
  ├─ ScriptPanel.unit_updated
  └─ LogPanel.append
Cancel button ──► ctx.cancel.set()
```

- `narration/gui/runner.py` owns the thread, the asyncio loop and the cancel
  event. Only one pipeline job runs per project at a time. While a job runs, the
  UI disables the actions that would conflict with it.
- `WxReporter` never touches widgets directly. It posts custom events
  (`EVT_STAGE_STARTED`, `EVT_PROGRESS`, `EVT_UNIT_UPDATED`, `EVT_LOG`,
  `EVT_STAGE_DONE`, `EVT_RUN_FAILED`) to the frame.
- Progress events are coalesced to about 20 Hz so a fast synth stage cannot
  flood the event queue.
- Small unit-level jobs (re-synth one unit, play a clip) go through the same
  runner queue. This keeps cache writes serialized.

---

## 5. Window layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ File  Edit  Run  View  Help                                              │
│ [▶ Run] [▶ Run from…▾] [■ Cancel]  │ [⟳ Re-synth unit] [▶ Play] │  Voice: ▾ │
├───────────────┬──────────────────────────────────────────────────────────┤
│ STAGES        │  Script │ QA │ Voice │ Log                                │
│ ✔ extract     │ ┌─────┬──────────┬────────────────────────────┬──────┐   │
│ ✔ transcribe  │ │ id  │ time     │ clean text                 │ flags│   │
│ ✔ segment     │ │u0012│ 01:02.4  │ Open the settings panel…   │      │   │
│ ✔ clean       │ │u0013│ 01:05.9  │ Then click Deploy.         │ ⚠ 0.38│  │
│ ⏸ REVIEW      │ │u0014│ 01:08.1  │ (dropped — filler)         │  ∅   │   │
│ ○ voice       │ └─────┴──────────┴────────────────────────────┴──────┘   │
│ ○ synth       │ RAW:   so um, then you, you click the the deploy button  │
│ ○ qa          │ CLEAN: Then click the Deploy button.            [▶orig]  │
│ ○ fit         │        ☐ keep original   ratio 0.52   [▶ tts] [⟳]       │
│ ○ assemble    │                                                          │
├───────────────┴──────────────────────────────────────────────────────────┤
│ TIMELINE  ▁▃▅▇▅▃▁▁▁▃▅▇▇▅▃▁   [u0012][u0013][u0014]      zoom ─●──         │
│           ░░slot░░██tts██░pad░     ▲ playhead                            │
├──────────────────────────────────────────────────────────────────────────┤
│ synth 34/120 ▓▓▓▓▓░░░░░  est. cost $0.06   work/demo-video/              │
└──────────────────────────────────────────────────────────────────────────┘
```

### Panels

- **Stages panel** (left, `stages_panel.py`). The 9 stages and the review
  checkpoint, each with a status icon (missing, running, done, stale, failed).
  Right-click a stage for **Run from here** or **Run only this**. A stale stage
  shows why (for example "script.json changed after synth").

- **Script panel** (`script_panel.py`, the main view). This is the review UI.
  - The top half is a `DataViewCtrl` with columns for id, start time, slot
    length, a clean-text preview, the length ratio and flags (ratio outside
    0.4–1.1, dropped unit, keep_original, QA WER, over slot). There is a filter
    box, plus "flagged only" and "changed only" toggles.
  - The bottom half is a detail editor for the selected unit:
    - Raw text, read-only, with deleted words struck through (`difflib` word
      diff).
    - Clean text in an editable `StyledTextCtrl` with glossary terms
      highlighted.
    - A `keep_original` checkbox.
    - **Play original** (plays the source slice `start → end` from
      `audio_full.wav`), **Play TTS** and **Re-synth**.
  - Edits update the in-memory `ScriptModel` and mark the document dirty.
    Ctrl+S saves `script.json` atomically through `artifacts.py`. After a save,
    downstream stages become stale.
  - Keyboard flow for fast review: ↑/↓ moves between units, Enter edits,
    Space plays the original, Ctrl+Space plays the TTS clip, Ctrl+Enter accepts
    and moves to the next unit.
  - **Approve script & continue** runs `voice → assemble`.

- **Timeline panel** (bottom, `timeline_panel.py`). The waveform of the source
  audio, with unit blocks drawn over it. Each block shows the slot region and
  the TTS clip length, colored by fit outcome: fits/padded, `atempo` applied,
  video stretched, or QA flagged. It scrolls and zooms (Ctrl+wheel). Clicking a
  block selects that unit in the Script panel, and selection syncs both ways.
  The playhead follows audio playback. In `compact` mode a second lane shows the
  output timeline.

- **QA panel**. A table of units that failed QA, with WER, retry count, and the
  expected vs. heard text. Actions: re-synth with a new seed, edit the text, or
  accept anyway.

- **Voice panel**. The auto-picked reference clip candidates (top 3, with
  scores), each with a play button. You can pick a candidate or choose your own
  `--ref` file. The panel also lists the global voice registry
  (`~/.config/narr/voices.json`) and has a button to register a new voice.

- **Log panel**. A `wx.TextCtrl` showing log lines filtered by level. The
  DeepInfra token is masked before it reaches the panel. "Copy diagnostics"
  copies the log to the clipboard.

- **Video preview** (optional, dockable). A `MediaCtrl` for `raw.mp4`, or for
  `clean.mp4` once it exists. It seeks to the selected unit's start time.

### Dialogs

- **New project**: pick a video (or drag and drop it onto the window), then a
  work directory (default `work/<stem>/`), timing mode and TTS model.
- **Settings**: a `PropertyGrid` with two tabs, project (`narr.toml`) and user
  (`~/.config/narr/config.toml`). Each value shows its source, and values can be
  reset to the inherited default. The prompt and glossary files open in an
  embedded `StyledTextCtrl`.
- **Export**: output path, burn-in or sidecar SRT, room-tone bed gain, and
  patch mode on/off. Shows a warning when re-encoding is required and the local
  ffmpeg cannot encode H.264 (see section 8).

### App chrome

- Menus with standard accelerators. Recent projects use `wx.FileHistory`
  (stored with `wx.Config`).
- The status bar shows the current stage's progress gauge, an estimated
  remaining TTS cost (characters to synthesize × the model's price from config)
  and the work directory path.
- The AUI layout and window geometry are saved per user.
- Closing with unsaved script edits asks Save / Discard / Cancel. Closing during
  a run asks whether to cancel the run.

---

## 6. Code layout

Same rules as the core: each module stays under 500 lines, and views do not
contain business logic. View models are plain Python with no `wx` import, so
they can be tested headless.

```
narration/
├── progress.py              # ProgressReporter protocol, RichReporter, NullReporter
└── gui/
    ├── __init__.py
    ├── app.py               # wx.App subclass, `narr-gui` entry point, single-instance check
    ├── main_frame.py        # AUI manager, menus, toolbar, status bar, event wiring
    ├── runner.py            # PipelineRunner: worker thread + asyncio loop + cancel
    ├── events.py            # custom wx event types + WxReporter
    ├── theme.py             # light/dark palette for custom-drawn widgets
    ├── audio_player.py      # sounddevice playback of file slices, playhead callback
    ├── models/              # NO wx imports — unit-testable
    │   ├── project_model.py # work dir, stage statuses, config, dirty tracking
    │   ├── script_model.py  # units, edits, undo stack, filters, flag computation
    │   └── timeline_model.py# peaks loading, unit geometry, zoom/scroll math
    ├── panels/
    │   ├── stages_panel.py
    │   ├── script_panel.py
    │   ├── unit_editor.py   # detail pane: diff view + clean-text editor + actions
    │   ├── timeline_panel.py
    │   ├── qa_panel.py
    │   ├── voice_panel.py
    │   ├── log_panel.py
    │   └── video_panel.py
    ├── dialogs/
    │   ├── new_project.py
    │   ├── settings.py      # PropertyGrid generated from pydantic schema
    │   └── export.py
    └── resources/icons/*.svg
tests/gui/
    ├── test_script_model.py
    ├── test_timeline_model.py
    ├── test_runner.py       # fake stages, verifies cancel + event ordering
    └── test_smoke.py        # xvfb-run: open frame, load fixture project, close
```

`pyproject.toml` gets a `gui` extra (`sounddevice`, `keyring`) and the script
entry `narr-gui = "narration.gui.app:main"`. The `narr` CLI gets
`narr gui [video]` as a shortcut.

---

## 7. Build order

GUI work is interleaved with the core milestones from the pipeline plan, so
each GUI piece is built once real artifacts exist for it to show. Each step ends
in a runnable state and a detailed commit.

| # | After core milestone | GUI deliverable |
|---|---|---|
| G1 | 1 (scaffold) + 2 (clients/media) | Core hooks: `progress.py`, cancel token, stage metadata and `project_status`. Then the GUI shell: `app.py`, `main_frame.py` with AUI layout, menus, New Project dialog, `runner.py` running `extract` with a live progress bar and log. |
| G2 | 3 (ASR) + 4 (segment) | Stages panel with live status. Timeline panel showing the waveform plus unit blocks from `units.json`. `audio_player.py` with "play unit slice". |
| G3 | 5 (clean) | **Script review panel**: DataView list, diff view, editor, flags, save/dirty/undo, keyboard flow, "Approve & continue". This is the most important GUI milestone. |
| G4 | 6 (voice + synth) | Voice panel. Per-unit Play TTS and Re-synth (needs `synthesize_unit` in the core). Cost estimate in the status bar. |
| G5 | 7 (QA) | QA panel. QA flags in the script list and on the timeline. |
| G6 | 8 (fit + assemble) | Timeline fit coloring (pad, atempo, stretch). Export dialog. Video preview panel. Open the result in the system player. |
| G7 | 9 (compact + patch) | Compact-mode output lane on the timeline. Patch-mode `keep_original` badges. Crossfade and bed settings in Export. |
| G8 | 10 (hardening) | Settings dialog (PropertyGrid). Persisted perspective and recent files. Dark-mode pass. xvfb smoke test in CI. README screenshots. |

---

## 8. Risks and open questions

- **API token in a desktop app** (decided). Apps started from a desktop launcher
  do not inherit shell env vars. `DEEPINFRA_TOKEN` takes precedence. If it is
  not set, the token is read from the system keyring through `keyring`
  (service `narr`, user `deepinfra`). The token is set from
  *Settings → DeepInfra token…* or with `narr token set`. It is never written to
  work dirs, config files or logs.
- **ffmpeg on this machine.** Fedora's `ffmpeg-free` cannot use `libx264`, but
  it does include `libopenh264` (H.264) and `libvpx-vp9`. The `media/ffmpeg.py`
  layer picks the first available H.264 encoder (`libx264` → `libopenh264` →
  hardware) for the stretch and `compact` paths. The Export dialog warns if none
  is found.
- **wxPython packaging.** The system interpreter is Python 3.14. Fedora 43
  ships wxPython 4.2.4 split across packages: `python3-wxpython4` (core),
  `python3-wxpython4-media` (`wx.media.MediaCtrl`, needed for the video panel)
  and `python3-wxpython4-webview` (not needed). `sounddevice` is not packaged,
  so install it with pip in the venv. It needs the `portaudio` system library.
  Install with: `sudo dnf install python3-wxpython4 python3-wxpython4-media
  portaudio xorg-x11-server-Xvfb`.
- **MediaCtrl reliability.** The GStreamer backend may need extra codecs to
  decode H.264. The video panel is optional for that reason. The core review
  workflow only depends on audio playback.
- **Large projects.** Waveform peaks are computed once with numpy at several
  zoom levels and cached. DataView stays virtual. Stage status checks hash file
  metadata (mtime + size) and only fall back to content hashes when needed.
- **Scope check.** Is the video preview needed in v1, or is "open in system
  player" enough? Dropping it removes the riskiest dependency.
