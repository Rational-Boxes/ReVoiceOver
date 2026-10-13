"""Custom wx events and the reporter that posts them from the worker thread."""

from __future__ import annotations

import threading
import time

import wx
import wx.lib.newevent

StageStartedEvent, EVT_STAGE_STARTED = wx.lib.newevent.NewEvent()   # stage, total
ProgressEvent, EVT_PROGRESS = wx.lib.newevent.NewEvent()            # stage, done, total, detail
UnitUpdatedEvent, EVT_UNIT_UPDATED = wx.lib.newevent.NewEvent()     # unit_id, status
LogEvent, EVT_LOG = wx.lib.newevent.NewEvent()                      # level, msg
StageDoneEvent, EVT_STAGE_DONE = wx.lib.newevent.NewEvent()         # stage, ok

PROGRESS_INTERVAL_S = 0.05  # coalesce progress to ~20 Hz


class WxReporter:
    """ProgressReporter that posts wx events; safe to call from any thread.

    Progress is posted as an absolute ``done`` count, so dropping intermediate
    updates while coalescing loses nothing.
    """

    def __init__(self, target: wx.EvtHandler) -> None:
        self._target = target
        self._lock = threading.Lock()
        self._done: dict[str, int] = {}
        self._total: dict[str, int | None] = {}
        self._last_post = 0.0

    def _post(self, event: wx.PyEvent) -> None:
        # The frame may already be destroyed when a late worker event arrives.
        if self._target:
            wx.PostEvent(self._target, event)

    def stage_started(self, stage: str, total: int | None) -> None:
        with self._lock:
            self._done[stage] = 0
            self._total[stage] = total
        self._post(StageStartedEvent(stage=stage, total=total))

    def set_total(self, stage: str, total: int | None) -> None:
        with self._lock:
            self._done[stage] = 0
            self._total[stage] = total
        self._flush(stage, "")

    def advance(self, stage: str, n: int = 1, detail: str = "") -> None:
        with self._lock:
            self._done[stage] = self._done.get(stage, 0) + n
            due = time.monotonic() - self._last_post >= PROGRESS_INTERVAL_S
        if due:
            self._flush(stage, detail)

    def _flush(self, stage: str, detail: str) -> None:
        with self._lock:
            done, total = self._done.get(stage, 0), self._total.get(stage)
            self._last_post = time.monotonic()
        self._post(ProgressEvent(stage=stage, done=done, total=total, detail=detail))

    def unit_updated(self, unit_id: str, status: str) -> None:
        self._post(UnitUpdatedEvent(unit_id=unit_id, status=status))

    def log(self, level: int, msg: str) -> None:
        self._post(LogEvent(level=level, msg=msg))

    def stage_finished(self, stage: str, ok: bool) -> None:
        self._flush(stage, "")
        self._post(StageDoneEvent(stage=stage, ok=ok))
