import json
import math
import sys
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile
from scipy import ndimage, signal

from analyze import BAND_CENTERS_HZ, SHORT_TERM_HOP_SECONDS, SHORT_TERM_SECONDS, loudness, measure
from memo import memoized

ROOT = Path(__file__).resolve().parents[3]
VALLEY_BAND_HZ = (40, 150)
VALLEY_SMOOTH_MS = 10
VALLEY_PEAK_MS = 50
VALLEY_GUARD_MS = 5
VALLEY_MIN_GAP_MS = 60
OVERLAP_FRAME_MS = 50
OVERLAP_WITHIN_DB = 6
DIP_FRAME_MS = 5
DIP_BEFORE_MS = (-100, 0)
DIP_AFTER_MS = (0, 60)
DIP_MAX_LAG_MS = 10
FILTER_ORDER = 4
TINY = 1e-20
PROFILE_LOUD_FIELDS = ["side_mid_db", "side_mid_below_120hz_db"]
PROFILE_WHOLE_FIELDS = ["integrated_lufs", "max_short_term_lufs", "peak_to_loudness_db"]


def ms(value, rate):
    return round(value / 1000 * rate)


def band(audio, rate, low, high):
    """Zero-phase, so filtered energy stays on the hit it came from."""
    low = low or 0
    high = high if high and high < rate / 2 else None
    if low <= 0 and high is None:
        return audio
    if low <= 0:
        sos = signal.butter(FILTER_ORDER, high, "lowpass", fs=rate, output="sos")
    elif high is None:
        sos = signal.butter(FILTER_ORDER, low, "highpass", fs=rate, output="sos")
    else:
        sos = signal.butter(FILTER_ORDER, [low, high], "bandpass", fs=rate, output="sos")
    return signal.sosfiltfilt(sos, audio, axis=0)


def spread(values, points=(0, 10, 50, 90, 100)):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return None
    return {f"p{p}": round(float(np.percentile(values, p)), 2) for p in points}


def db(ratio):
    return 10 * math.log10(ratio) if ratio > 0 else None


def rounded(value):
    return round(value, 2) if value is not None and math.isfinite(value) else None


@memoized
def valley(audio, rate, times):
    if len(times) < 2:
        return {"gaps": 0, "skipped_short_gaps": 0, "silent_gaps": 0, "valley_db": None}
    filtered = band(audio, rate, *VALLEY_BAND_HZ)
    # The analytic envelope has no dips at each zero crossing, so a slow low note can't fake a valley.
    power = (np.abs(signal.hilbert(filtered, axis=0)) ** 2).sum(axis=1)
    power = ndimage.uniform_filter1d(power, max(1, ms(VALLEY_SMOOTH_MS, rate)), mode="nearest")
    values, skipped, silent = [], 0, 0
    for start, following in zip(times, times[1:]):
        if following - start < ms(VALLEY_MIN_GAP_MS, rate) or following > len(power):
            skipped += 1
            continue
        end = following - ms(VALLEY_GUARD_MS, rate)
        peak_at = start + int(np.argmax(power[start : min(start + ms(VALLEY_PEAK_MS, rate), end)]))
        if power[peak_at] <= TINY:
            silent += 1
            continue
        values.append(10 * math.log10(max(power[peak_at:end].min(), TINY) / power[peak_at]))
    return {"gaps": len(values), "skipped_short_gaps": skipped, "silent_gaps": silent, "valley_db": spread(values)}


def mask_from(length, times, window_ms, rate):
    inside = np.zeros(length, dtype=bool)
    for t in times:
        inside[max(0, t + ms(window_ms[0], rate)) : max(0, t + ms(window_ms[1], rate))] = True
    return inside


@memoized
def overlap(first, second, rate, bands, inside=None):
    # Only masked samples count, in the 6 dB test and in both sums alike, so "on hits" compares like with like.
    if len(first) != len(second):
        sys.exit("overlap needs two files of the same length")
    weight = np.ones(len(first)) if inside is None else inside.astype(float)
    frame = ms(OVERLAP_FRAME_MS, rate)
    edges = np.arange(0, len(first), frame)
    result = {"selected_seconds": round(float(weight.sum()) / rate, 2), "bands": {}}
    for low, high in bands:
        energy = [ (band(audio, rate, low, high) ** 2).sum(axis=1) * weight for audio in (first, second)]
        a, b = (np.add.reduceat(e, edges) for e in energy)
        both = (a > TINY) & (b > TINY)
        close = both & (np.abs(10 * np.log10(np.maximum(a, TINY) / np.maximum(b, TINY))) <= OVERLAP_WITHIN_DB)
        total = float(a.sum() + b.sum())
        result["bands"][f"{low}-{high or 'top'}"] = {
            "overlap_db": rounded(db(float(np.minimum(a, b)[close].sum()) / total)) if total > 0 else None,
            "frames": int(both.sum()),
            "close_frames": int(close.sum()),
        }
    return result


def frame_power(audio, rate):
    frame = ms(DIP_FRAME_MS, rate)
    usable = len(audio) // frame * frame
    return (audio[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1), frame


def lag(reference, delayed, rate):
    window = min(len(reference), 10 * rate)
    cumulative = np.concatenate([[0.0], np.cumsum((reference**2).sum(axis=1))])
    energy = cumulative[window:] - cumulative[:-window]
    start = int(np.argmax(energy)) if len(energy) else 0
    reach = min(ms(DIP_MAX_LAG_MS, rate), window - 1)
    middle = window - 1
    # Per channel, then summed: summing the channels first cancels anti-phase stereo to nothing.
    correlation = sum(
        signal.correlate(delayed[start : start + window, ch], reference[start : start + window, ch], mode="full", method="fft")
        for ch in range(reference.shape[1])
    )
    if reach < 0 or np.max(np.abs(correlation)) <= TINY:
        return None
    return int(np.argmax(correlation[middle - reach : middle + reach + 1])) - reach


@memoized
def dip(before, after, rate, times):
    shift = lag(before, after, rate)
    if shift is None:
        return {"lag_samples": None, "hits": 0, "dip_db": None}
    # Shifted with silence, not wrapped, so no sample from the other end of the song is compared.
    aligned = np.zeros_like(after)
    if shift >= 0:
        aligned[: len(after) - shift] = after[shift:]
    else:
        aligned[-shift:] = after[:shift]
    after = aligned
    power_in, frame = frame_power(before, rate)
    power_out, _ = frame_power(after, rate)
    with np.errstate(divide="ignore", invalid="ignore"):
        gain = 10 * np.log10(power_out / power_in)
    gain[power_in < TINY] = np.nan
    values = []
    for t in times:
        pre = gain[max(0, (t + ms(DIP_BEFORE_MS[0], rate)) // frame) : max(0, (t + ms(DIP_BEFORE_MS[1], rate)) // frame)]
        post = gain[(t + ms(DIP_AFTER_MS[0], rate)) // frame : (t + ms(DIP_AFTER_MS[1], rate)) // frame]
        if np.isfinite(pre).any() and np.isfinite(post).any():
            values.append(float(np.nanmedian(pre) - np.nanmin(post)))
    return {"lag_samples": shift, "hits": len(values), "dip_db": spread(values, (10, 50, 90, 100))}


def k_weighted_cumulative(audio, rate):
    weighted = audio.copy()
    for stage in pyloudnorm.Meter(rate)._filters.values():
        for ch in range(weighted.shape[1]):
            weighted[:, ch] = stage.apply_filter(weighted[:, ch])
    return np.concatenate([[0.0], np.cumsum(np.sum(weighted**2, axis=1))])


@memoized
def sections(audio, rate, named, low=None, high=None):
    filtered = band(audio, rate, low, high)
    integrated = loudness(filtered, rate)[0]["integrated_lufs"]
    cumulative = k_weighted_cumulative(filtered, rate)
    window, hop = int(SHORT_TERM_SECONDS * rate), int(SHORT_TERM_HOP_SECONDS * rate)
    result = {"integrated_lufs": integrated, "sections": {}}
    for name, section in named.items():
        first, last = round(section["start_s"] * rate), min(round(section["end_s"] * rate), len(filtered))
        # Windows start at the section's own start, so where the song-wide grid falls can't empty a section.
        starts = np.arange(first, last - window + 1, hop)
        mean = (cumulative[starts + window] - cumulative[starts]) / window
        with np.errstate(divide="ignore"):
            inside = -0.691 + 10 * np.log10(mean)
        inside = inside[np.isfinite(inside)]
        entry = {"group": section.get("group"), "windows": len(inside), "short_term_lufs": spread(inside, (10, 50, 90))}
        if len(inside):
            entry["spread_lu"] = round(float(np.percentile(inside, 90) - np.percentile(inside, 10)), 2)
            if "loudness_lu" in section and integrated is not None:
                entry["error_lu"] = round(float(np.median(inside)) - integrated - section["loudness_lu"], 2)
        result["sections"][name] = entry
    return result


@memoized(context=lambda args: (ROOT / "profiles" / args["name"] / "profile.json").read_bytes()
          if args["name"] is not None else None)
def profile_range(audio, rate, name):
    if name is None:
        return {"skipped": "no profile"}
    profile = json.loads((ROOT / "profiles" / name / "profile.json").read_text())
    if profile["sample_rate"] != rate:
        sys.exit(f"profile {name} is measured at {profile['sample_rate']} Hz, the file is {rate} Hz")
    whole = loudness(audio, rate)[0]
    loud = measure(audio, rate, loud=True)
    values = {field: whole[field] for field in PROFILE_WHOLE_FIELDS} | {field: loud[field] for field in PROFILE_LOUD_FIELDS}
    values |= {f"bands_db.{band}": value for band, value in (loud["bands_db"] or {}).items()}
    result = {}
    for field, value in values.items():
        key = field.split(".", 1)
        refs = [ref[key[0]] if len(key) == 1 else ref[key[0]].get(key[1]) for ref in profile["references"]]
        refs = [ref for ref in refs if ref is not None]
        if not refs or value is None:
            continue
        low, high = min(refs), max(refs)
        outside = value - high if value > high else value - low if value < low else 0
        result[field] = {"value": value, "min": low, "max": high, "outside": round(outside, 2)}
    return result


def read(path):
    audio, rate = soundfile.read(path, dtype="float64", always_2d=True)
    return audio, rate


def hit_samples(path, rate):
    return np.array([round(t * rate) for t in json.loads(Path(path).read_text())["times_s"]], dtype=int)


def octave_bands():
    return [(round(c / math.sqrt(2)), round(c * math.sqrt(2))) for c in BAND_CENTERS_HZ]


USAGE = """usage: uv run .agents/skills/analyze/measure.py
  valley --times <hits.json> <file.wav>
  overlap [--band <low> <high>]... [--times <hits.json> --window <from_ms> <to_ms>] <a.wav> <b.wav>
  dip --times <hits.json> <master-in.wav> <trial.wav>
  sections [--band <low> <high>] <recipe.json> <file.wav>
  range [--profile <name>] <file.wav>"""


def options(args, name, count, repeat=False):
    found = []
    while name in args:
        index = args.index(name)
        if index + count >= len(args):
            sys.exit(USAGE)
        found.append(args[index + 1 : index + 1 + count])
        del args[index : index + 1 + count]
    if len(found) > 1 and not repeat:
        sys.exit(USAGE)
    return found


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(USAGE)
    command = args.pop(0)
    times = options(args, "--times", 1)
    bands = [(float(low), None if high in ("0", "none") else float(high)) for low, high in options(args, "--band", 2, repeat=True)]
    window = options(args, "--window", 2)
    profile = options(args, "--profile", 1)
    files = [read(path) for path in args[-2:] if path.endswith(".wav")]
    rates = {rate for _, rate in files}
    if len(rates) > 1:
        sys.exit("files must share one sample rate")
    if command == "valley" and times and len(args) == 1:
        audio, rate = files[0]
        report = valley(audio, rate, hit_samples(times[0][0], rate))
    elif command == "overlap" and len(args) == 2 and bool(times) == bool(window):
        (first, rate), (second, _) = files
        inside = mask_from(len(first), hit_samples(times[0][0], rate), [float(v) for v in window[0]], rate) if times else None
        report = overlap(first, second, rate, bands or octave_bands(), inside)
    elif command == "dip" and times and len(args) == 2:
        (before, rate), (after, _) = files
        report = dip(before, after, rate, hit_samples(times[0][0], rate))
    elif command == "sections" and len(args) == 2 and len(bands) <= 1:
        audio, rate = files[0]
        named = json.loads(Path(args[0]).read_text()).get("sections", {})
        report = sections(audio, rate, named, *(bands[0] if bands else (None, None)))
    elif command == "range" and len(args) == 1:
        audio, rate = files[0]
        report = profile_range(audio, rate, profile[0][0] if profile else None)
    else:
        sys.exit(USAGE)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
