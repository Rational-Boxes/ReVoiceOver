import numpy as np
import soundfile as sf

from narration.media.audio import compute_peaks, load_peaks, read_mono, reduce_peaks


def test_compute_peaks():
    rate = 1000
    samples = np.concatenate([np.full(500, 0.5), np.full(500, -0.25)]).astype(np.float32)
    peaks = compute_peaks(samples, rate, per_second=2)
    assert peaks.tolist() == [[0.5, 0.5], [-0.25, -0.25]]


def test_reduce_partitions_and_pads():
    p = np.array([[-i, i] for i in range(10)], dtype=np.float32)
    assert reduce_peaks(p, 0, 2.5, 4).tolist() == [[-1, 1], [-4, 4], [-6, 6], [-9, 9]]
    zoomed = reduce_peaks(p, 8, 0.5, 6)
    assert zoomed[:4, 1].tolist() == [8, 8, 9, 9]
    assert zoomed[4:].tolist() == [[0, 0], [0, 0]]
    assert reduce_peaks(p, 50, 1.0, 3).tolist() == [[0, 0]] * 3


def test_read_mono_slice_and_cached_peaks(tmp_path):
    rate = 8000
    stereo = np.stack([np.linspace(-1, 1, rate * 2), np.zeros(rate * 2)], axis=1)
    wav = tmp_path / "a.wav"
    sf.write(wav, stereo, rate)
    mono, r = read_mono(wav, 0.5, 1.0)
    assert r == rate and len(mono) == rate // 2
    assert np.allclose(mono, stereo[rate // 2: rate, 0] / 2, atol=1e-4)

    peaks = load_peaks(tmp_path, wav)
    assert peaks.shape == (400, 2)
    cached = list((tmp_path / "cache" / "peaks").glob("*.npy"))
    assert len(cached) == 1
    assert np.array_equal(load_peaks(tmp_path, wav), peaks)
