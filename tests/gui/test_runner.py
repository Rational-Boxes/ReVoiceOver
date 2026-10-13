"""PipelineRunner is wx-free, so these run without a display."""

import importlib
import threading

from narration import stages
from narration.gui.models.project_model import Project
from narration.gui.runner import BusyError, PipelineRunner
from narration.pipeline import Outcome
from narration.progress import NullReporter


def test_runs_on_worker_and_cancels(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_text("v")
    project = Project.create(video)
    started = threading.Event()

    def slow_extract(ctx):
        started.set()
        while True:  # a stage that only stops when cancelled
            ctx.check_cancelled()
            threading.Event().wait(0.01)

    monkeypatch.setattr(importlib.import_module("narration.stages.extract"), "run", slow_extract)
    results = []
    finished = threading.Event()
    runner = PipelineRunner(NullReporter(), lambda r: (results.append(r), finished.set()))
    runner.start(project)
    assert started.wait(5)
    assert runner.running
    try:
        runner.start(project)
        raise AssertionError("expected BusyError")
    except BusyError:
        pass
    runner.cancel()
    assert finished.wait(5)
    assert runner.join(5)
    assert results[0].outcome is Outcome.CANCELLED and results[0].last_stage == "extract"
    assert project.statuses()["extract"] is stages.Status.MISSING


def test_start_job_reports_result_and_errors(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_text("v")
    project = Project.create(video)
    outcomes = []
    done = threading.Event()

    def on_done(result, error):
        outcomes.append((result, error))
        done.set()

    runner = PipelineRunner(NullReporter(), lambda r: None)

    async def ok(ctx):
        return ctx.work_dir.name

    runner.start_job(project, ok, on_done)
    assert done.wait(5) and runner.join(5)
    assert outcomes[-1] == (project.work_dir.name, None)

    async def boom(ctx):
        raise ValueError("nope")

    done.clear()
    runner.start_job(project, boom, on_done)
    assert done.wait(5) and runner.join(5)
    assert outcomes[-1][0] is None and isinstance(outcomes[-1][1], ValueError)
