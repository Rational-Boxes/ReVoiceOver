"""Audio helpers on numpy arrays (no subprocesses; those live in ffmpeg.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from narration.cache import FileCache, file_sha256, make_key

PEAKS_PER_SECOND = 200  # base resolution; views reduce it further


def read_mono(path: Path, start: float = 0.0, end: float | None = None) -> tuple[np.ndarray, int]:
    """float32 mono samples for ``[start, end)`` seconds, and the sample rate."""
    with sf.SoundFile(str(path)) as f:
        rate = f.samplerate
        first = max(0, int(start * rate))
        last = f.frames if end is None else min(f.frames, int(end * rate))
        f.seek(first)
        data = f.read(max(0, last - first), dtype="float32", always_2d=True)
    return data.mean(axis=1), rate


def compute_peaks(samples: np.ndarray, rate: int,
                  per_second: int = PEAKS_PER_SECOND) -> np.ndarray:
    """(n, 2) array of per-bin (min, max) at ``per_second`` bins per second."""
    bin_size = max(1, rate // per_second)
    n = len(samples) // bin_size
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32)
    bins = samples[: n * bin_size].reshape(n, bin_size)
    return np.stack([bins.min(axis=1), bins.max(axis=1)], axis=1).astype(np.float32)


def load_peaks(work_dir: Path, wav: Path, per_second: int = PEAKS_PER_SECOND) -> np.ndarray:
    """Peaks for ``wav``, cached as .npy keyed by the file's content hash."""
    cache = FileCache(work_dir, "peaks")
    key = make_key("peaks-v1", per_second, file_sha256(wav))
    path = cache.path(key, ".npy")
    if path.exists():
        return np.load(path)
    samples, rate = read_mono(wav)
    peaks = compute_peaks(samples, rate, per_second)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, peaks)
    return peaks


def reduce_peaks(peaks: np.ndarray, start_bin: float, bins_per_column: float,
                 columns: int) -> np.ndarray:
    """Collapse base peaks into ``columns`` display columns starting at ``start_bin``.

    Column ``i`` covers bins ``[floor(start + i*bpc), floor(start + (i+1)*bpc))``
    (at least one bin). Columns outside the audio are (0, 0).
    """
    out = np.zeros((columns, 2), dtype=np.float32)
    if len(peaks) == 0 or columns <= 0 or bins_per_column <= 0:
        return out
    edges = start_bin + bins_per_column * np.arange(columns + 1)
    lo = np.floor(edges[:-1]).astype(np.int64)
    inside = (lo >= 0) & (lo < len(peaks))
    if not inside.any():
        return out
    cols = np.flatnonzero(inside)  # contiguous, since lo increases
    lo = lo[cols]
    if bins_per_column <= 1:
        out[cols] = peaks[lo]
        return out
    end = min(len(peaks), int(np.floor(edges[cols[-1] + 1])))
    end = max(end, int(lo[-1]) + 1)
    view = peaks[:end]
    out[cols, 0] = np.minimum.reduceat(view[:, 0], lo)
    out[cols, 1] = np.maximum.reduceat(view[:, 1], lo)
    return out
