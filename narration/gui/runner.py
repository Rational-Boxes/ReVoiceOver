"""Runs pipeline jobs on a worker thread, one at a time per project window.

Deliberately free of wx: results come back through the ``on_finished``
callback, which the GUI wraps in ``wx.CallAfter``. That keeps this module
testable without a display.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from narration.context import RunContext
from narration.gui.models.project_model import Project
from narration.pipeline import Outcome, RunResult, run_pipeline
from narration.progress import ProgressReporter


class BusyError(RuntimeError):
    pass


class PipelineRunner:
    def __init__(self, reporter: ProgressReporter,
                 on_finished: Callable[[RunResult], None]) -> None:
        self._reporter = reporter
        self._on_finished = on_finished
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, project: Project, *, start: str | None = None, stop: str | None = None,
              review: bool = True) -> None:
        if self.running:
            raise BusyError("a pipeline job is already running")
        self._cancel = threading.Event()
        ctx = project.context(self._reporter, self._cancel)
        self._thread = threading.Thread(
            target=self._work, args=(ctx, start, stop, review),
            name=f"pipeline-{project.name}", daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker; returns True if it has finished."""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.running

    def _work(self, ctx: RunContext, start: str | None, stop: str | None, review: bool) -> None:
        try:
            result = asyncio.run(run_pipeline(ctx, start=start, stop=stop, review=review))
        except Exception as exc:  # run_pipeline handles stage errors; this is a bug guard
            ctx.error(f"pipeline crashed: {type(exc).__name__}: {exc}")
            result = RunResult(Outcome.FAILED, None, f"{type(exc).__name__}: {exc}")
        self._on_finished(result)
