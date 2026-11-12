import json
import sys

import numpy as np
import soundfile
from scipy import ndimage, signal

FLUX_FFT = 2048
FLUX_HOP = 256
FLUX_MEDIAN_S = 0.5
FLUX_OVER = 1.5
SPACING_MS = 60
MAX_NOTE_S = 1.0
LEVEL_MIN_MS = 60
PITCH_MIN_MS = 120
PITCH_HZ = (25, 250)
PITCH_CONFIDENCE = 0.5
ATTACK_MS, BODY_MS = 25, (30, 100)
LOW_BAND_HZ = (40, 150)


def band(audio, rate, low, high):
    sos = signal.butter(4, [low, high] if low else high, "bandpass" if low else "lowpass", fs=rate, output="sos")
    return signal.sosfiltfilt(sos, audio, axis=0)


def onsets(audio, rate):
    """Spectral flux: a new note raises the level of new partials, which a distorted, already loud track hides from a level detector."""
    mono = audio.mean(axis=1)
    _, centres, spectrum = signal.stft(mono, rate, nperseg=FLUX_FFT, noverlap=FLUX_FFT - FLUX_HOP, boundary=None, padded=False)
    magnitude = np.log1p(1000 * np.abs(spectrum))
    flux = np.maximum(np.diff(magnitude, axis=1), 0).sum(axis=0)
    floor = ndimage.median_filter(flux, size=max(3, round(FLUX_MEDIAN_S * rate / FLUX_HOP)) | 1, mode="nearest")
    spread = np.median(np.abs(flux - np.median(flux))) + 1e-12
    peaks, _ = signal.find_peaks(flux, height=floor + FLUX_OVER * spread, distance=max(1, round(SPACING_MS / 1000 * rate / FLUX_HOP)))
    # A frame's flux only says the note began somewhere under its window, so each onset is moved to the
    # steepest rise of the 1 ms energy envelope within half a window of the frame's centre.
    level = 10 * np.log10(np.maximum(ndimage.uniform_filter1d(mono**2, max(1, round(0.001 * rate))), 1e-20))
    rise = np.zeros_like(level)
    step = max(1, round(0.002 * rate))
    rise[step:] = level[step:] - level[:-step]
    found = []
    for centre in np.round(centres[peaks + 1] * rate).astype(int):
        low, high = max(0, centre - FLUX_FFT // 2), min(len(level), centre + FLUX_FFT // 2)
        found.append(low + int(np.argmax(rise[low:high])) - step if high > low else centre)
    return np.unique(np.maximum(np.array(found, dtype=int), 0))


def segments(times, rate, length):
    """Each note runs to the next onset, at most a second, so no measure reads into the next note."""
    ends = np.minimum(np.append(times[1:], length), times + round(MAX_NOTE_S * rate))
    return np.stack([times, ends], axis=1) if len(times) else np.zeros((0, 2), dtype=int)


def f0(mono, rate):
    mono = mono - mono.mean()
    corr = signal.correlate(mono, mono, mode="full", method="fft")[len(mono) - 1 :]
    if corr[0] <= 0:
        return None, 0.0
    low, high = int(rate / PITCH_HZ[1]), min(int(rate / PITCH_HZ[0]), len(corr) - 1)
    if high <= low:
        return None, 0.0
    lag = low + int(np.argmax(corr[low:high]))
    return rate / lag, float(corr[lag] / corr[0])


def measure(audio, rate, times):
    mono = audio.mean(axis=1)
    power = (audio**2).sum(axis=1)
    low = band(audio, rate, *LOW_BAND_HZ)
    low_power = (low**2).sum(axis=1)
    notes = []
    for start, end in segments(times, rate, len(audio)):
        length_ms = (end - start) / rate * 1000
        note = {"start": int(start), "end": int(end), "ms": round(length_ms, 1)}
        if length_ms >= LEVEL_MIN_MS:
            head = slice(start, start + round(min(100, length_ms) / 1000 * rate))
            note["level_db"] = float(10 * np.log10(max(power[head].mean(), 1e-20)))
            note["low_db"] = float(10 * np.log10(max(low_power[head].mean(), 1e-20)))
            attack = power[start : start + round(ATTACK_MS / 1000 * rate)].sum()
            # The body window is clipped to the note, so a short note's body is not the next note's attack.
            body_end = start + round(min(BODY_MS[1], length_ms) / 1000 * rate)
            body = power[start + round(BODY_MS[0] / 1000 * rate) : body_end]
            if len(body) and body.sum() > 0:
                note["attack_db"] = float(10 * np.log10(max(attack, 1e-20) / body.sum()))
        if length_ms >= PITCH_MIN_MS:
            frequency, confidence = f0(mono[start + round(0.03 * rate) : end], rate)
            if frequency and confidence >= PITCH_CONFIDENCE:
                note["f0_hz"] = round(frequency, 2)
        notes.append(note)
    return notes


def summary(notes):
    return {
        "notes": len(notes),
        "with_level": sum("level_db" in note for note in notes),
        "with_f0": sum("f0_hz" in note for note in notes),
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: uv run .agents/skills/analyze/notes.py <performance.wav>")
    audio, rate = soundfile.read(sys.argv[1], always_2d=True)
    notes = measure(audio, rate, onsets(audio, rate))
    print(json.dumps(summary(notes) | {"per_note": notes}))


if __name__ == "__main__":
    main()
