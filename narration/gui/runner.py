"""Runs pipeline jobs on a worker thread, one at a time per project window.

Deliberately free of wx: results come back through the ``on_finished``
callback, which the GUI wraps in ``wx.CallAfter``. That keeps this module
testable without a display.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

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

    def start_job(self, project: Project, job: Callable[[RunContext], Awaitable[Any]],
                  on_done: Callable[[Any, BaseException | None], None]) -> None:
        """Run a small async job (e.g. re-synth one unit) on the worker thread.

        ``on_done(result, error)`` is called from the worker thread.
        """
        if self.running:
            raise BusyError("a pipeline job is already running")
        self._cancel = threading.Event()
        ctx = project.context(self._reporter, self._cancel)

        async def main() -> Any:
            return await job(ctx)

        def work() -> None:
            try:
                result = asyncio.run(main())
            except BaseException as exc:  # reported to the UI, never swallowed silently
                ctx.error(f"{type(exc).__name__}: {exc}")
                on_done(None, exc)
                return
            on_done(result, None)

        self._thread = threading.Thread(target=work, name=f"job-{project.name}", daemon=True)
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
