import json
import math
import sys

import numpy as np
import pyloudnorm
import soundfile
from scipy import signal

from memo import memoized

BAND_CENTERS_HZ = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
SHORT_TERM_SECONDS = 3.0
SHORT_TERM_HOP_SECONDS = 0.1
LOW_END_HZ = 120
# Shorter segments smear energy across the 22 Hz edge of the 31.5 Hz band (0.3 to 0.8 dB off).
SEGMENT_SECONDS = 6.0
LOUD_PARTS_LU = 3.0


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


def short_term_lufs(meter, audio, rate):
    window = int(SHORT_TERM_SECONDS * rate)
    hop = int(SHORT_TERM_HOP_SECONDS * rate)
    if len(audio) < window:
        return np.array([]), hop
    # Reuses pyloudnorm's K-weighting so it matches its integrated loudness.
    weighted = audio.copy()
    for stage in meter._filters.values():
        for ch in range(weighted.shape[1]):
            weighted[:, ch] = stage.apply_filter(weighted[:, ch])
    energy = np.concatenate([[0.0], np.cumsum(np.sum(weighted**2, axis=1))])
    starts = np.arange(0, len(audio) - window + 1, hop)
    mean = (energy[starts + window] - energy[starts]) / window
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(mean), hop


def loud_parts(audio, rate, loudness, hop):
    if len(loudness) == 0 or not np.isfinite(loudness.max()):
        return []
    window = int(SHORT_TERM_SECONDS * rate)
    parts = []
    for start in np.flatnonzero(loudness >= loudness.max() - LOUD_PARTS_LU) * hop:
        if parts and start <= parts[-1][1]:
            parts[-1][1] = start + window
        else:
            parts.append([start, start + window])
    return [audio[start:end] for start, end in parts]


def bands_db(parts, rate):
    # Each passage gets its own spectrum, so joins between passages add no clicks.
    # Hann windows at 75% overlap, with a full window of silence at both ends, weight every sample
    # equally and keep one segment size for passages shorter than a segment.
    nperseg = 2 ** int(math.log2(SEGMENT_SECONDS * rate))
    hop = nperseg // 4
    window = signal.windows.hann(nperseg, sym=False)[:, None]
    freqs = np.fft.rfftfreq(nperseg, 1 / rate)
    power = np.zeros(len(freqs))
    for part in parts:
        padded = np.pad(part, ((nperseg, nperseg + (-len(part)) % hop), (0, 0)))
        # One frame at a time: holding every frame of a long hi-res file takes gigabytes.
        for start in range(0, len(padded) - nperseg + 1, hop):
            frame = np.fft.rfft(padded[start : start + nperseg] * window, axis=0)
            power += (np.abs(frame) ** 2).sum(axis=1)
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


def side_mid_db(mid_power, side_power):
    return db(side_power / mid_power) if mid_power > 0 else None


def stereo(parts, rate):
    if parts[0].shape[1] != 2:
        return {"side_mid_db": None, "side_mid_below_120hz_db": None, "correlation": None}
    lowpass = signal.butter(4, LOW_END_HZ, "lowpass", fs=rate, output="sos")
    sums = np.zeros(7)
    for part in parts:
        left, right = part[:, 0], part[:, 1]
        low = signal.sosfilt(lowpass, part, axis=0)
        sums += [
            np.sum(((left + right) / 2) ** 2),
            np.sum(((left - right) / 2) ** 2),
            np.sum(((low[:, 0] + low[:, 1]) / 2) ** 2),
            np.sum(((low[:, 0] - low[:, 1]) / 2) ** 2),
            np.sum(left * right),
            np.sum(left**2),
            np.sum(right**2),
        ]
    mid, side, low_mid, low_side, cross, left_power, right_power = sums
    norm = math.sqrt(left_power * right_power)
    return {
        "side_mid_db": rounded(side_mid_db(mid, side)),
        "side_mid_below_120hz_db": rounded(side_mid_db(low_mid, low_side)),
        "correlation": rounded(cross / norm) if norm > 0 else None,
    }


def loudness(audio, rate):
    meter = pyloudnorm.Meter(rate)
    integrated = integrated_lufs(meter, audio)
    peak = true_peak_dbtp(audio)
    short_term, hop = short_term_lufs(meter, audio, rate)
    fields = {
        "integrated_lufs": rounded(integrated),
        "true_peak_dbtp": rounded(peak),
        "peak_to_loudness_db": rounded(peak - integrated) if None not in (peak, integrated) else None,
        "max_short_term_lufs": rounded(short_term.max()) if len(short_term) else None,
    }
    return fields, short_term, hop


@memoized
def measure(audio, rate, loud=False):
    if len(audio) == 0 or audio.shape[1] > 2:
        raise ValueError("needs non-empty mono or stereo audio")
    fields, short_term, hop = loudness(audio, rate)
    parts = loud_parts(audio, rate, short_term, hop) if loud else [audio]
    result = {"sample_rate": rate, "channels": audio.shape[1], "seconds": round(len(audio) / rate, 3)} | fields
    if loud:
        result["loud_seconds"] = round(sum(len(part) for part in parts) / rate, 1)
        if not parts:
            return result | {"bands_db": None, "partial_bands": None, "side_mid_db": None,
                             "side_mid_below_120hz_db": None, "correlation": None}
    return result | bands_db(parts, rate) | stereo(parts, rate)


if __name__ == "__main__":
    args = sys.argv[1:]
    loud = "--loud" in args
    paths = [arg for arg in args if arg != "--loud"]
    if len(paths) != 1 or len(args) - len(paths) > 1:
        sys.exit("usage: uv run .agents/skills/analyze/analyze.py [--loud] <file.wav>")
    audio, rate = soundfile.read(paths[0], dtype="float64", always_2d=True)
    try:
        print(json.dumps({"file": paths[0]} | measure(audio, rate, loud), indent=2))
    except ValueError as error:
        sys.exit(f"{paths[0]}: {error}")
