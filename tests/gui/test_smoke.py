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
