import json
import sys

import numpy as np
import soundfile
from scipy import fft, ndimage

FRAME_MS = 1
RISE_DB = 12
RISE_WINDOW_MS = 20
HOLD_MS = 15
GAP_MS = 5
FLOOR_DB = 45
SPACING_MS = 30
ATTACK_MS, BODY_MS = (0, 25), (30, 100)
TAIL_MS, HEAD_MS = (80, 200), (0, 30)
ON_HIT_MS = (0, 100)
OFF_HIT_MS = (-20, 200)
LEVEL_FLOOR_DB = -120
BANDS_HZ = {"all": (0, None), "low": (0, 150), "low_mid": (150, 1000), "high_mid": (1000, 5000), "high": (5000, None)}


def envelope_db(energy, rate):
    width = max(1, round(FRAME_MS / 1000 * rate))
    smooth = ndimage.uniform_filter1d(energy, width, mode="constant")
    return 10 * np.log10(np.maximum(smooth, 1e-20))


def trailing(window_filter, values, size, shift):
    size |= 1
    centred = window_filter(values, size, mode="nearest")
    lag = size // 2 + shift
    return np.concatenate([np.full(lag, values[0]), centred[: len(values) - lag]])


def detect(energy, rate, floor_db=FLOOR_DB, rise_db=RISE_DB):
    level = envelope_db(energy, rate)
    # Holding the peak over the last 15 ms hides the dips at each zero crossing of a low note.
    hold = trailing(ndimage.maximum_filter1d, level, round(HOLD_MS / 1000 * rate), shift=0)
    # A hit is a jump from the lowest held level in the 20 ms before it, not a peak inside a sustain.
    before = trailing(ndimage.minimum_filter1d, hold, round(RISE_WINDOW_MS / 1000 * rate), shift=1)
    # A hit right after another rises from a short gap the 20 ms window can't see. Even a 20 Hz note
    # moves under 10 dB within 5 ms, so a bigger jump over the 5 ms before the 1 ms ramp is a hit.
    recent = trailing(ndimage.maximum_filter1d, level, round(GAP_MS / 1000 * rate), shift=round(FRAME_MS / 1000 * rate))
    rising = ((hold - before >= rise_db) | (level - recent >= rise_db)) & (hold >= hold.max() - floor_db)
    starts = np.flatnonzero(rising & ~np.concatenate([[False], rising[:-1]]))
    times, last = [], -np.inf
    for index in starts:
        if index - last >= SPACING_MS / 1000 * rate:
            times.append(int(index))
            last = index
    return np.array(times, dtype=int)


def span_energy(cumulative, starts, span_ms, rate):
    first = np.clip(starts + round(span_ms[0] / 1000 * rate), 0, len(cumulative) - 1)
    last = np.clip(starts + round(span_ms[1] / 1000 * rate), 0, len(cumulative) - 1)
    return cumulative[last] - cumulative[first]


def ratio_db(top, bottom):
    return 10 * np.log10(np.maximum(top, 1e-20) / np.maximum(bottom, 1e-20))


def mask(length, times, span_ms, rate):
    inside = np.zeros(length, dtype=bool)
    for t in times:
        inside[max(0, t + round(span_ms[0] / 1000 * rate)) : max(0, t + round(span_ms[1] / 1000 * rate))] = True
    return inside


def band_energy(audio, rate, low, high):
    if low == 0 and high is None:
        return (audio**2).sum(axis=1)
    size = fft.next_fast_len(len(audio))
    spectrum = fft.rfft(audio, size, axis=0)
    freqs = fft.rfftfreq(size, 1 / rate)
    spectrum[(freqs < low) | (freqs >= (high or np.inf))] = 0
    return (fft.irfft(spectrum, size, axis=0)[: len(audio)] ** 2).sum(axis=1)


def measure(audio, rate, times, include_off_hit=True):
    energy = (audio**2).sum(axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(energy)])
    level = envelope_db(energy, rate)
    reach = round(ATTACK_MS[1] / 1000 * rate)
    on, off = mask(len(energy), times, ON_HIT_MS, rate), ~mask(len(energy), times, OFF_HIT_MS, rate)
    bleed = {}
    for name, (low, high) in BANDS_HZ.items():
        band = band_energy(audio, rate, low, high)
        # Mean power, so the long stretches between hits don't outweigh the short hits by their length.
        bleed[name] = float(ratio_db(band[off].mean() if off.any() else 0, band[on].mean() if on.any() else 0))
    return {
        "level_db": np.array([level[t : t + reach].max() for t in times]),
        "attack_db": ratio_db(span_energy(cumulative, times, ATTACK_MS, rate), span_energy(cumulative, times, BODY_MS, rate)),
        "tail_db": ratio_db(span_energy(cumulative, times, TAIL_MS, rate), span_energy(cumulative, times, HEAD_MS, rate)),
        "off_hit_level_db": np.maximum(level[off], LEVEL_FLOOR_DB) if include_off_hit else None,
        "bleed_db": bleed,
    }


def spread(values, points=(0, 10, 50, 90)):
    if len(values) == 0:
        return None
    return {f"p{p}": round(float(np.percentile(values, p)), 2) for p in points}


def read(path):
    audio, rate = soundfile.read(path, always_2d=True)
    return audio, rate


def summary(measured):
    return {
        "level_db": spread(measured["level_db"]),
        "attack_db": spread(measured["attack_db"]),
        "tail_db": spread(measured["tail_db"]),
        "off_hit_level_db": spread(measured["off_hit_level_db"], (50, 90, 99)),
        "bleed_db": {name: round(value, 2) for name, value in measured["bleed_db"].items()},
    }


USAGE = "usage: uv run .agents/skills/analyze/hits.py [--times-from <clean.wav> | --times <hits.json>] [--min-level-db <dB>] [--floor-db 45] [--rise-db 12] [--list] <input.wav> [<output.wav>]"


def main():
    args = sys.argv[1:]
    listing = "--list" in args
    args = [arg for arg in args if arg != "--list"]
    options = {"--floor-db": FLOOR_DB, "--rise-db": RISE_DB, "--times-from": None, "--times": None, "--min-level-db": None}
    for name in options:
        if name in args:
            index = args.index(name)
            if index + 1 >= len(args):
                sys.exit(USAGE)
            options[name] = args[index + 1]
            del args[index : index + 2]
    if len(args) not in (1, 2) or (options["--times"] and options["--times-from"]):
        sys.exit(USAGE)
    audio, rate = read(args[0])
    if options["--times"]:
        saved = json.loads(open(options["--times"]).read())
        times = np.array([round(t * rate) for t in saved["times_s"]], dtype=int)
    else:
        source, source_rate = read(options["--times-from"]) if options["--times-from"] else (audio, rate)
        if source_rate != rate:
            sys.exit("--times-from must have the input's sample rate")
        times = detect((source**2).sum(axis=1), rate, float(options["--floor-db"]), float(options["--rise-db"]))
    times = times[times < len(audio)]
    if options["--min-level-db"] is not None:
        times = times[measure(audio, rate, times)["level_db"] >= float(options["--min-level-db"])]
    before = measure(audio, rate, times)
    report = {"hits": len(times), "times_s": [round(t / rate, 5) for t in times], **summary(before)}
    if len(times):
        report["level_spread_db"] = round(float(np.percentile(before["level_db"], 90) - np.percentile(before["level_db"], 10)), 2)
    if listing:
        report["per_hit"] = [
            {"time_s": round(t / rate, 5), "level_db": round(float(level), 2), "attack_db": round(float(attack), 2), "tail_db": round(float(tail), 2)}
            for t, level, attack, tail in zip(times, before["level_db"], before["attack_db"], before["tail_db"])
        ]
    if len(args) == 2:
        out_audio, out_rate = read(args[1])
        if out_rate != rate or out_audio.shape != audio.shape:
            sys.exit("input and output must have the same rate, length and channels")
        after = measure(out_audio, rate, times)
        report["change"] = {name: spread(after[name] - before[name]) for name in ("level_db", "attack_db", "tail_db")}
        report["change"]["bleed_db"] = {name: round(after["bleed_db"][name] - before["bleed_db"][name], 2) for name in BANDS_HZ}
        report["after"] = summary(after)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
