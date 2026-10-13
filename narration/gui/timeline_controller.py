"""Owns the timeline panel, the loaded units and audio playback for one window."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import wx

from narration import artifacts
from narration.gui.audio_player import AudioPlayer
from narration.gui.panels.timeline_panel import TimelinePanel
from narration.media.audio import load_peaks
from narration.types import Unit

PLAYHEAD_MS = 33


class TimelineController(wx.EvtHandler):
    def __init__(self, parent: wx.Window, infobar: wx.InfoBar,
                 on_select: Callable[[str], None]) -> None:
        super().__init__()
        # Clicks go to the frame first so the script list follows the selection.
        self.panel = TimelinePanel(parent, on_select, self.play, self.stop)
        self.infobar = infobar
        self.player = AudioPlayer()
        self.units: list[Unit] = []
        self.work_dir: Path | None = None
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self.timer)

    def load(self, work_dir: Path) -> None:
        """(Re)load waveform and units from whatever the work dir has so far."""
        self.work_dir = work_dir
        duration = 0.0
        peaks = np.zeros((0, 2), dtype=np.float32)
        try:
            duration = float(artifacts.read_json(work_dir / "probe.json")["format"]["duration"])
            with wx.BusyCursor():
                peaks = load_peaks(work_dir, work_dir / "audio_16k.wav")
        except (FileNotFoundError, KeyError, artifacts.SchemaError):
            pass
        self.units = _load_units(work_dir)
        self.panel.set_data(duration, peaks, self.units)

    def refresh_units(self, units: list[Unit]) -> None:
        """Show edited script units (same ids and timing) without reloading audio."""
        if units:
            self.units = units
            self.panel.model.set_units(units)
            self.panel.canvas.Refresh()
            self.panel.set_selected_info(self.panel.model.selected_unit())

    def select(self, unit_id: str) -> None:
        self.panel.select(unit_id)

    def unit(self, unit_id: str) -> Unit | None:
        return next((u for u in self.units if u.id == unit_id), None)

    def play(self, unit_id: str) -> None:
        unit = self.unit(unit_id)
        if unit is None or self.work_dir is None:
            return
        if self.player.playing and self.panel.model.selected == unit_id:
            self.stop()
            return
        try:
            self.player.play(self.work_dir / "audio_full.wav", unit.start, unit.end)
        except Exception as exc:
            self.infobar.ShowMessage(f"Cannot play audio: {exc}", wx.ICON_WARNING)
            return
        self.timer.Start(PLAYHEAD_MS)

    def stop(self) -> None:
        self.player.stop()
        self.timer.Stop()
        self.panel.set_playhead(None)

    def _on_timer(self, _event: wx.TimerEvent) -> None:
        t = self.player.position()
        self.panel.set_playhead(t)
        if t is None:
            self.timer.Stop()


def _load_units(work_dir: Path) -> list[Unit]:
    """Script units (with clean text) when the script exists, else segmented units."""
    for name, key in (("script.json", "units"), ("units.json", None)):
        try:
            data = artifacts.read_json(work_dir / name)
        except (FileNotFoundError, artifacts.SchemaError):
            continue
        return [Unit.from_dict(u) for u in (data[key] if key else data)]
    return []
