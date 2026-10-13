"""Timeline geometry: zoom, scroll, hit-testing and waveform columns. No wx imports."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np

from narration.media.audio import PEAKS_PER_SECOND, reduce_peaks
from narration.types import Unit

MIN_PX_PER_SEC = 0.5
MAX_PX_PER_SEC = 2000.0


@dataclass
class TimelineModel:
    duration: float = 0.0
    peaks: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.float32))
    peaks_per_second: int = PEAKS_PER_SECOND
    units: list[Unit] = field(default_factory=list)
    px_per_sec: float = 20.0
    offset: float = 0.0        # seconds at the left edge
    width: int = 1             # viewport width in px
    selected: str | None = None
    marks: dict[str, str] = field(default_factory=dict)  # unit id -> "warn" | "error"

    def __post_init__(self) -> None:
        self._starts: list[float] = [u.start for u in self.units]

    # -- data --------------------------------------------------------------
    def set_units(self, units: list[Unit]) -> None:
        self.units = units
        self._starts = [u.start for u in units]
        if self.selected and self.index_of(self.selected) is None:
            self.selected = None

    def index_of(self, unit_id: str) -> int | None:
        return next((i for i, u in enumerate(self.units) if u.id == unit_id), None)

    def selected_unit(self) -> Unit | None:
        i = self.index_of(self.selected) if self.selected else None
        return self.units[i] if i is not None else None

    # -- geometry ----------------------------------------------------------
    @property
    def visible_seconds(self) -> float:
        return self.width / self.px_per_sec

    @property
    def max_offset(self) -> float:
        return max(0.0, self.duration - self.visible_seconds)

    def x_for(self, t: float) -> float:
        return (t - self.offset) * self.px_per_sec

    def time_for(self, x: float) -> float:
        return self.offset + x / self.px_per_sec

    def set_width(self, width: int) -> None:
        self.width = max(1, width)
        self.offset = min(self.offset, self.max_offset)

    def scroll_to(self, offset: float) -> None:
        self.offset = min(max(0.0, offset), self.max_offset)

    def scroll_by_px(self, dx: float) -> None:
        self.scroll_to(self.offset + dx / self.px_per_sec)

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        """Zoom by ``factor`` keeping the time under ``anchor_x`` fixed."""
        anchor_x = self.width / 2 if anchor_x is None else anchor_x
        anchor_t = self.time_for(anchor_x)
        self.px_per_sec = min(MAX_PX_PER_SEC,
                              max(self.min_px_per_sec, self.px_per_sec * factor))
        self.scroll_to(anchor_t - anchor_x / self.px_per_sec)

    @property
    def min_px_per_sec(self) -> float:
        """Zooming out stops when the whole recording fits."""
        if self.duration <= 0:
            return MIN_PX_PER_SEC
        return max(MIN_PX_PER_SEC, min(self.width / self.duration, 20.0))

    def zoom_to_fit(self) -> None:
        self.px_per_sec = self.min_px_per_sec
        self.offset = 0.0

    def ensure_visible(self, start: float, end: float, margin_px: float = 40) -> None:
        margin = margin_px / self.px_per_sec
        if start < self.offset + margin:
            self.scroll_to(start - margin)
        elif end > self.offset + self.visible_seconds - margin:
            span = end - start
            if span + 2 * margin > self.visible_seconds:
                self.scroll_to(start - margin)
            else:
                self.scroll_to(end + margin - self.visible_seconds)

    # -- hit-testing -------------------------------------------------------
    def unit_at(self, t: float) -> Unit | None:
        """The unit whose slot contains ``t`` (speech start to next unit's start)."""
        i = bisect.bisect_right(self._starts, t) - 1
        if 0 <= i < len(self.units) and t < self.units[i].slot_end:
            return self.units[i]
        return None

    def neighbour(self, step: int) -> Unit | None:
        if not self.units:
            return None
        i = self.index_of(self.selected) if self.selected else None
        if i is None:
            # Nothing selected: start from whatever is in view.
            first = self.unit_at(self.offset) or self.units[0]
            return first
        return self.units[min(max(0, i + step), len(self.units) - 1)]

    def visible_units(self) -> list[Unit]:
        end = self.offset + self.visible_seconds
        lo = max(0, bisect.bisect_right(self._starts, self.offset) - 1)
        hi = bisect.bisect_right(self._starts, end)
        return self.units[lo:hi]

    # -- waveform ----------------------------------------------------------
    def waveform_columns(self) -> np.ndarray:
        """(width, 2) min/max per pixel column for the current view."""
        bins_per_px = self.peaks_per_second / self.px_per_sec
        return reduce_peaks(self.peaks, self.offset * self.peaks_per_second,
                            bins_per_px, self.width)

    def tick_step(self, min_px: float = 80) -> float:
        """A 'nice' ruler step in seconds with at least ``min_px`` between ticks."""
        for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
            if step * self.px_per_sec >= min_px:
                return step
        return 3600


def format_time(t: float, step: float = 1.0) -> str:
    minutes, seconds = divmod(max(0.0, t), 60)
    if step < 1:
        return f"{int(minutes)}:{seconds:04.1f}"
    return f"{int(minutes)}:{int(seconds):02d}"
