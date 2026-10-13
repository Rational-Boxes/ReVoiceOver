"""Open the real main window. Run with: xvfb-run -a pytest -m gui"""

import os
import time

import pytest

pytestmark = pytest.mark.gui

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display; run under xvfb-run", allow_module_level=True)

wx = pytest.importorskip("wx")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # keep wx.Config out of the real home
    from narration.gui.app import NarrApp

    app = NarrApp(redirect=False)
    yield app
    for frame in app.frames():
        frame.Destroy()
    wx.SafeYield()
    app.Destroy()


def _pump_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wx.SafeYield()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_open_project_and_run_extract(app, demo_video, tmp_path):
    from narration import stages
    from narration.gui.models.project_model import Project

    frame = app.new_window()
    assert frame.book.GetSelection() == 0  # welcome page

    project = Project.create(demo_video, tmp_path / "work")
    frame._attach(project)
    assert frame.book.GetSelection() == 1
    assert "demo" in frame.GetTitle()

    frame.run_only("extract")
    assert frame.runner.running
    assert _pump_until(lambda: not frame.runner.running)
    assert _pump_until(lambda: frame.gauge.IsShown() is False)
    assert project.statuses()["extract"] is stages.Status.DONE
    status_col = frame.stages_panel.list.GetValue(0, 1)
    assert status_col == "done"
    assert "Extract audio" in frame.log_panel.text.GetValue()
    model = frame.timeline.panel.model
    assert model.duration == pytest.approx(8.0, abs=0.2)
    assert model.peaks.shape[0] > 1000
    assert model.units == []


def test_script_review_flow(app, tmp_path):
    from narration import artifacts
    from narration.gui.models.project_model import Project
    from narration.types import Unit

    video = tmp_path / "talk.mp4"
    video.write_text("v")
    project = Project.create(video, tmp_path / "work")
    units = [Unit(id=f"u{i:04d}", start=i * 2.0, end=i * 2.0 + 1.5, slot_end=i * 2.0 + 2,
                  raw_text=f"um sentence {i}", clean_text=f"Sentence {i}.",
                  llm_text=f"Sentence {i}.") for i in range(1, 6)]
    units[2].flags = ["dropped-content"]
    units[2].clean_text = units[2].llm_text = ""
    artifacts.write_json(project.work_dir / "script.json",
                         {"model": "m", "units": [u.to_dict() for u in units]})

    frame = app.new_window()
    frame._attach(project)
    panel = frame.script
    assert panel.model.loaded and panel.list.GetItemCount() == 5
    assert frame.notebook.GetSelection() == 0

    panel.flagged_cb.SetValue(True)
    panel._refill()
    assert panel.list.GetItemCount() == 1

    # Selecting from the timeline clears the filter that hides the unit.
    frame.select_unit("u0002")
    assert panel.selected_id() == "u0002" and panel.list.GetItemCount() == 5
    assert frame.timeline.panel.model.selected == "u0002"

    panel.editor.clean.SetText("A better second sentence.")
    assert panel.dirty
    panel.move(1)  # commits, moves on
    assert panel.selected_id() == "u0003"
    assert panel.model.get("u0002").edited
    assert frame.GetTitle().startswith("•")

    panel.undo()
    assert panel.model.get("u0002").clean_text == "Sentence 2."
    panel.redo()
    panel.save()
    saved = artifacts.read_json(project.work_dir / "script.json")["units"]
    assert saved[1]["clean_text"] == "A better second sentence." and saved[1]["edited"]
    assert not panel.dirty and not frame.GetTitle().startswith("•")


def test_voice_and_tts_controls(app, tmp_path):
    import numpy as np
    import soundfile as sf

    from narration import artifacts
    from narration.gui.models.project_model import Project
    from narration.types import Unit

    video = tmp_path / "talk.mp4"
    video.write_text("v")
    project = Project.create(video, tmp_path / "work")
    work = project.work_dir
    units = [Unit("u0001", 0.0, 1.0, 1.2, "um hi", "Hi.", "Hi."),
             Unit("u0002", 1.2, 2.0, 3.0, "uh", "", "")]
    artifacts.write_json(work / "script.json", {"units": [u.to_dict() for u in units]})
    (work / "tts").mkdir()
    sf.write(work / "tts" / "u0001.wav", np.zeros(44100, dtype=np.float32), 44100)
    artifacts.write_json(work / "synth.json", {"units": {"u0001": {
        "file": "tts/u0001.wav", "duration": 1.5, "chars": 3, "seed": 42, "cached": False,
        "text": "Hi."}}})
    artifacts.write_json(work / "voice.json", {
        "model": "Zyphra/Zonos-v0.1-hybrid", "voice_id": "v1", "ref_span": [3.0, 14.0],
        "reused": False, "candidates": [{"start": 3.0, "end": 14.0, "score": 0.9,
                                         "confidence": 0.9, "text": "hello there"}]})

    frame = app.new_window()
    frame._attach(project)
    frame.select_unit("u0001")
    editor = frame.script.editor
    assert editor.tts_btn.IsEnabled() and editor.take_btn.IsEnabled()
    assert "clip 1.5s / slot 1.2s" in editor.clip_info.GetLabel()
    editor.clean.SetText("Hello!")
    editor.commit()
    assert "text changed since synthesis" in editor.clip_info.GetLabel()

    frame.select_unit("u0002")
    assert not frame.script.editor.take_btn.IsEnabled()  # dropped: nothing to synthesize

    vp = frame.review.voice_panel
    assert "Voice v1" in vp.summary.GetLabel()
    assert vp.alts.GetItemCount() == 1
    assert "TTS for whole script" in frame.GetStatusBar().GetStatusText(2)
    frame.script.model.dirty = False



def test_quality_tab(app, tmp_path):
    from narration import artifacts
    from narration.gui.models.project_model import Project
    from narration.types import Unit

    video = tmp_path / "talk.mp4"
    video.write_text("v")
    project = Project.create(video, tmp_path / "work")
    work = project.work_dir
    units = [Unit(f"u000{i}", i, i + 0.8, i + 1, f"raw {i}", f"Clean {i}.", f"Clean {i}.")
             for i in range(1, 4)]
    artifacts.write_json(work / "script.json", {"units": [u.to_dict() for u in units]})
    artifacts.write_json(work / "qa.json", {"model": "m", "max_wer": 0.15, "units": {
        "u0001": {"wer": 0.0, "heard": "clean 1", "retries": 0, "flags": [], "take": None},
        "u0002": {"wer": 1.0, "heard": "", "retries": 3, "flags": ["wer"], "take": None},
        "u0003": {"wer": 0.1, "heard": "clean 3", "retries": 0, "flags": ["silence"],
                  "take": None}}})

    frame = app.new_window()
    frame._attach(project)
    qa = frame.review.qa_panel
    assert qa.list.GetItemCount() == 2
    assert "2 of 3" in qa.summary.GetLabel()
    assert frame.timeline.panel.model.marks == {"u0002": "error", "u0003": "error"}
    assert frame.script.model.stats()["flagged"] == 2

    qa.list.SelectRow(0)
    frame.review.accept_qa("u0002")
    assert qa.list.GetItemCount() == 1
    assert artifacts.read_json(work / "qa.json")["units"]["u0002"]["accepted"] is True
    assert "u0002" not in frame.timeline.panel.model.marks

    frame.review.edit_unit("u0003")
    assert frame.script.selected_id() == "u0003"
    assert "long silence" in frame.script.editor.flag_label.GetLabel()
