import json
import sys

import numpy as np
import pyloudnorm
import soundfile

MAX_LAG = 4096
LAG_BANDS_HZ = {"low": (20, 250), "high": (1000, 8000)}
BANDS_HZ = [(20, 60), (60, 250), (250, 1000), (1000, 4000), (4000, 8000), (8000, 16000)]


def read(path, start, end):
    audio, rate = soundfile.read(path, always_2d=True)
    return audio[int(start * rate) : int(end * rate) if end else None], rate


def lag_samples(before, after, rate=None, band_hz=None):
    a, b = before.sum(axis=1), after.sum(axis=1)
    if band_hz:
        a, b = (bandpass(x, rate, *band_hz) for x in (a, b))
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    corr = np.concatenate([corr[-MAX_LAG:], corr[: MAX_LAG + 1]])
    best = int(np.argmax(np.abs(corr)))
    return best - MAX_LAG, bool(corr[best] < 0)


def bandpass(audio, rate, low, high):
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    spectrum[(freqs < low) | (freqs >= high)] = 0
    return np.fft.irfft(spectrum, len(audio))


def band_shares(audio, rate):
    power = np.abs(np.fft.rfft(audio.mean(axis=1))) ** 2
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    return [10 * np.log10(power[(freqs >= lo) & (freqs < hi)].sum() / power.sum() + 1e-30) for lo, hi in BANDS_HZ]


def main():
    if len(sys.argv) not in (3, 5):
        sys.exit("usage: uv run .agents/skills/saturate/match.py <before.wav> <after.wav> [<start_s> <end_s>]")
    start, end = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) == 5 else (0.0, None)
    before, rate = read(sys.argv[1], start, end)
    after, after_rate = read(sys.argv[2], start, end)
    if rate != after_rate or before.shape != after.shape:
        sys.exit("files differ in rate, length or channels")
    if len(before) < 0.4 * rate:
        sys.exit("the section must be at least 0.4 s long and inside both files")
    meter = pyloudnorm.Meter(rate)
    change = meter.integrated_loudness(after) - meter.integrated_loudness(before)
    lag, inverted = lag_samples(before, after)
    by_band = {name: lag_samples(before, after, rate, band_hz)[0] for name, band_hz in LAG_BANDS_HZ.items()}
    peak = lambda audio: 20 * np.log10(np.abs(audio).max())
    print(json.dumps({
        "loudness_change_lu": round(change, 2),
        "match_gain_db": round(-change, 2),
        "peak_change_db": round(peak(after) - peak(before), 2),
        "peak_change_at_equal_loudness_db": round(peak(after) - peak(before) - change, 2),
        "lag_samples": lag,
        "lag_samples_by_band": by_band,
        "polarity_inverted": inverted,
        "band_share_change_db": {f"{lo}-{hi}": round(b - a, 2) for (lo, hi), a, b in zip(BANDS_HZ, band_shares(before, rate), band_shares(after, rate))},
    }))


if __name__ == "__main__":
    main()
