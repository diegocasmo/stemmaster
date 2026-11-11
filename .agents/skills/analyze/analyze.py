import json
import math
import sys

import numpy as np
import pyloudnorm
import soundfile
from scipy import signal

BAND_CENTERS_HZ = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
SHORT_TERM_SECONDS = 3.0
SHORT_TERM_HOP_SECONDS = 0.1
LOW_END_HZ = 120


def db(power_ratio):
    return 10 * math.log10(power_ratio) if power_ratio > 0 else None


def rounded(value):
    return round(value, 2) if value is not None and math.isfinite(value) else None


def integrated_lufs(meter, audio):
    try:
        return meter.integrated_loudness(audio)
    except ValueError:
        return None


def true_peak_dbtp(audio):
    # BS.1770 Annex 2: 4x oversampling finds peaks between samples.
    # Per channel, because upsampling all channels at once needs far more memory.
    peak = max(np.max(np.abs(signal.resample_poly(ch, 4, 1))) for ch in audio.T)
    return 20 * math.log10(peak) if peak > 0 else None


def max_short_term_lufs(meter, audio, rate):
    window = int(SHORT_TERM_SECONDS * rate)
    if len(audio) < window:
        return None
    # Reuses pyloudnorm's K-weighting so it matches its integrated loudness.
    weighted = audio.copy()
    for stage in meter._filters.values():
        for ch in range(weighted.shape[1]):
            weighted[:, ch] = stage.apply_filter(weighted[:, ch])
    energy = np.concatenate([[0.0], np.cumsum(np.sum(weighted**2, axis=1))])
    starts = np.arange(0, len(audio) - window + 1, int(SHORT_TERM_HOP_SECONDS * rate))
    loudest = np.max(energy[starts + window] - energy[starts]) / window
    return -0.691 + 10 * math.log10(loudest) if loudest > 0 else None


def bands_db(audio, rate):
    freqs, power = signal.welch(audio, rate, nperseg=min(16384, len(audio)), axis=0)
    power = power.sum(axis=1)
    total = power.sum()
    nyquist = rate / 2
    bands, partial = {}, []
    for center in BAND_CENTERS_HZ:
        low, high = center / math.sqrt(2), center * math.sqrt(2)
        in_band = power[(freqs >= low) & (freqs < high)].sum()
        bands[str(center)] = rounded(db(in_band / total)) if total > 0 else None
        if high > nyquist:
            partial.append(str(center))
    return {"bands_db": bands, "partial_bands": partial}


def side_mid_db(left, right):
    mid, side = (left + right) / 2, (left - right) / 2
    mid_power = np.sum(mid**2)
    return db(np.sum(side**2) / mid_power) if mid_power > 0 else None


def stereo(audio, rate):
    if audio.shape[1] != 2:
        return {"side_mid_db": None, "side_mid_below_120hz_db": None, "correlation": None}
    left, right = audio[:, 0], audio[:, 1]
    lowpass = signal.butter(4, LOW_END_HZ, "lowpass", fs=rate, output="sos")
    low = signal.sosfilt(lowpass, audio, axis=0)
    norm = math.sqrt(np.sum(left**2) * np.sum(right**2))
    return {
        "side_mid_db": rounded(side_mid_db(left, right)),
        "side_mid_below_120hz_db": rounded(side_mid_db(low[:, 0], low[:, 1])),
        "correlation": rounded(np.sum(left * right) / norm) if norm > 0 else None,
    }


def analyze(path):
    audio, rate = soundfile.read(path, dtype="float64", always_2d=True)
    if len(audio) == 0 or audio.shape[1] > 2:
        sys.exit(f"{path}: needs a non-empty mono or stereo file")
    meter = pyloudnorm.Meter(rate)
    loudness = integrated_lufs(meter, audio)
    peak = true_peak_dbtp(audio)
    return {
        "file": path,
        "sample_rate": rate,
        "channels": audio.shape[1],
        "seconds": round(len(audio) / rate, 3),
        "integrated_lufs": rounded(loudness),
        "true_peak_dbtp": rounded(peak),
        "peak_to_loudness_db": rounded(peak - loudness) if None not in (peak, loudness) else None,
        "max_short_term_lufs": rounded(max_short_term_lufs(meter, audio, rate)),
        **bands_db(audio, rate),
        **stereo(audio, rate),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: uv run .agents/skills/analyze/analyze.py <file.wav>")
    print(json.dumps(analyze(sys.argv[1]), indent=2))
