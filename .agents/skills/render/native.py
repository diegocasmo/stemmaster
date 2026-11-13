import math

import numba
import numpy as np
from scipy import signal

RIDE_FIELDS = {"segments", "band_hz", "ramp_ms"}
DUCK_FIELDS = {"key", "band_hz", "key_band_hz", "threshold_db", "ratio", "range_db", "attack_ms", "release_ms"}
DUCK_REQUIRED = DUCK_FIELDS - {"band_hz", "key_band_hz"}
DEFAULT_RAMP_MS = 10


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def band_error(band_hz, rate):
    if band_hz is None:
        return None
    if not (isinstance(band_hz, list) and len(band_hz) == 2):
        return "band_hz must be [low_hz, high_hz]"
    low, high = band_hz
    if not all(edge is None or number(edge) and edge >= 0 for edge in band_hz):
        return "band_hz edges must be numbers >= 0 or null"
    if not low and not high:
        return "band_hz needs at least one edge"
    if low and high and low >= high:
        return "band_hz low edge must be below the high edge"
    if max(low or 0, high or 0) >= rate / 2:
        return f"band_hz edges must be below {rate / 2:g} Hz"
    return None


def band(audio, rate, band_hz):
    low, high = band_hz
    filters = [signal.butter(2, edge, kind, fs=rate, output="sos") for edge, kind in ((low, "highpass"), (high, "lowpass")) if edge]
    sos = np.vstack(filters)
    # A narrow band-pass peaks below unity; scaling it to 1 makes the full depth land at the band's centre.
    _, response = signal.sosfreqz(sos, worN=1 << 16, fs=rate)
    peak = np.max(np.abs(response) ** 2)
    # Silence past the file edges, as place() assumes, instead of filtfilt's mirrored padding.
    pad = rate
    padded = np.pad(audio, ((pad, pad), (0, 0)))
    # Zero phase, so x - band(x) is the exact complement and a cut never turns into a boost.
    return signal.sosfiltfilt(sos, padded, axis=0, padtype=None)[pad:-pad] / peak


def apply_gain(audio, rate, gain, band_hz):
    gain = gain[:, None]
    if band_hz is None:
        return audio * gain
    return audio + (gain - 1) * band(audio, rate, band_hz)


def segment_times(segment, sections):
    if len(segment) == 2 and segment[0] in sections:
        section = sections[segment[0]]
        return section["start_s"], section["end_s"], segment[1]
    return segment


def ride_error(settings, sections, rate):
    if unknown := set(settings) - RIDE_FIELDS:
        return f"unknown settings {sorted(unknown)}"
    segments = settings.get("segments")
    if not isinstance(segments, list) or not segments:
        return "segments must be a non-empty list"
    ramp = settings.get("ramp_ms", DEFAULT_RAMP_MS)
    if not number(ramp) or ramp < 0:
        return "ramp_ms must be a number >= 0"
    times = []
    for segment in segments:
        if not isinstance(segment, list) or len(segment) not in (2, 3):
            return f"segment {segment} must be [start_s, end_s, gain_db] or [section, gain_db]"
        if len(segment) == 2 and segment[0] not in sections:
            return f"segment {segment} names an unknown section"
        start, end, gain_db = segment_times(segment, sections)
        if not all(number(value) for value in (start, end, gain_db)) or not 0 <= start < end:
            return f"segment {segment} needs 0 <= start < end and a numeric gain"
        if (end - start) * 1000 < ramp:
            return f"segment {segment} is shorter than ramp_ms"
        times.append((start, end))
    times.sort()
    for (_, end), (start, _) in zip(times, times[1:]):
        if start < end:
            return "segments overlap"
    return band_error(settings.get("band_hz"), rate)


def ride(audio, rate, settings, context):
    half = settings.get("ramp_ms", DEFAULT_RAMP_MS) / 2000 * rate
    gain_db = np.zeros(len(audio))
    for segment in settings["segments"]:
        start, end, value = segment_times(segment, context.sections)
        start, end = start * rate, end * rate
        first, last = max(0, math.floor(start - half)), min(len(audio), math.ceil(end + half) + 1)
        times = np.arange(first, last)
        # Raised-cosine ramps centred on each edge, so back-to-back segments cross over smoothly.
        rise = np.clip((times - start + half) / (2 * half), 0, 1) if half else (times >= start) * 1.0
        fall = np.clip((end + half - times) / (2 * half), 0, 1) if half else (times < end) * 1.0
        weight = (1 - np.cos(np.pi * np.minimum(rise, fall))) / 2
        gain_db[first:last] += value * weight
    return apply_gain(audio, rate, 10 ** (gain_db / 20), settings.get("band_hz"))


def duck_error(settings, target, sources, rate):
    if unknown := set(settings) - DUCK_FIELDS:
        return f"unknown settings {sorted(unknown)}"
    if missing := DUCK_REQUIRED - set(settings):
        return f"missing settings {sorted(missing)}"
    if settings["key"] not in sources:
        return f"unknown key {settings['key']}"
    if settings["key"] == target:
        return "a step cannot key from its own target"
    values = [settings[name] for name in ("threshold_db", "ratio", "range_db", "attack_ms", "release_ms")]
    if not all(number(value) for value in values):
        return "threshold_db, ratio, range_db, attack_ms and release_ms must be numbers"
    if settings["ratio"] < 1 or settings["range_db"] < 0 or settings["attack_ms"] < 0 or settings["release_ms"] < 0:
        return "ratio must be >= 1; range_db, attack_ms and release_ms >= 0"
    return band_error(settings.get("band_hz"), rate) or band_error(settings.get("key_band_hz"), rate)


def smoothing(ms, rate):
    return math.exp(-1000 / (ms * rate)) if ms > 0 else 0.0


# No fastmath: the compiled loop must give the same bits as the plain float64 loop it replaced.
@numba.njit
def follow(level, attack, release):
    envelope, state = np.empty(len(level)), 0.0
    for index in range(len(level)):
        coefficient = attack if level[index] > state else release
        state = coefficient * state + (1 - coefficient) * level[index]
        envelope[index] = state
    return envelope


def duck(audio, rate, settings, context):
    key = context.key(settings["key"])
    if settings.get("key_band_hz") is not None:
        key = band(key, rate, settings["key_band_hz"])
    # Linked peak: one detector for all channels, so the duck never moves the stereo image.
    level = np.max(np.abs(key), axis=1)
    attack, release = smoothing(settings["attack_ms"], rate), smoothing(settings["release_ms"], rate)
    envelope = follow(level, attack, release)
    level_db = 20 * np.log10(np.maximum(envelope, 1e-10))
    over = np.maximum(0, level_db - settings["threshold_db"]) * (1 - 1 / settings["ratio"])
    gain = 10 ** (-np.minimum(settings["range_db"], over) / 20)
    return apply_gain(audio, rate, gain, settings.get("band_hz"))
