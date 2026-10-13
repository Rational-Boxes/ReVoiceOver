"""Plays slices of audio files with sounddevice; reports the playback position.

The GUI polls ``position()`` from a timer to draw the playhead, so nothing here
calls back into wx from the audio thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from narration.media.audio import read_mono


class AudioUnavailable(RuntimeError):
    pass


class AudioPlayer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._start_t = 0.0
        self._end_t = 0.0
        self._sd = None
        self.error: str | None = None
        try:
            import sounddevice as sd

            sd.query_devices(kind="output")
            self._sd = sd
        except Exception as exc:  # no PortAudio, no output device (CI, xvfb)
            self.error = f"audio output unavailable: {exc}"

    @property
    def available(self) -> bool:
        return self._sd is not None

    def play(self, path: Path, start: float, end: float,
             timeline_start: float | None = None) -> None:
        """Play ``path`` from ``start`` to ``end`` seconds.

        ``position()`` reports timeline time: ``timeline_start`` plus elapsed
        (defaults to ``start``, i.e. the file is the source recording).
        """
        if self._sd is None:
            raise AudioUnavailable(self.error or "audio output unavailable")
        samples, rate = read_mono(path, start, end)
        self.stop()
        self._sd.play(samples, rate)
        origin = start if timeline_start is None else timeline_start
        with self._lock:
            self._start_t, self._end_t = origin, origin + len(samples) / rate
            self._started_at = time.monotonic()

    def stop(self) -> None:
        if self._sd is not None:
            self._sd.stop()
        with self._lock:
            self._started_at = None

    def position(self) -> float | None:
        """Current timeline position, or None when idle."""
        with self._lock:
            if self._started_at is None:
                return None
            t = self._start_t + (time.monotonic() - self._started_at)
            if t >= self._end_t:
                self._started_at = None
                return None
            return t

    @property
    def playing(self) -> bool:
        return self.position() is not None
