import json
import sys
from pathlib import Path

import numpy as np
import soundfile
from scipy import fft

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analyze"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))
from hits import detect, measure
from render import pan

WINDOW_MS = 100
LIMIT_MS = 20


def placed(recipe, song, name, offset=None, invert=None, stereo=False):
    settings = recipe["stems"][name]
    audio, rate = soundfile.read(song / "stems" / name, always_2d=True)
    audio = audio * 10 ** (settings.get("gain_db", 0) / 20)
    if settings.get("invert", False) if invert is None else invert:
        audio = -audio
    audio = pan(audio, settings.get("pan", 0)) if stereo else audio.mean(axis=1, keepdims=True)
    offset = settings.get("offset_samples", 0) if offset is None else offset
    out = np.zeros((len(audio) + abs(offset), audio.shape[1]))
    start = max(offset, 0)
    out[start : start + len(audio) - max(-offset, 0)] = audio[max(-offset, 0) :]
    return (out if stereo else out[:, 0]), rate


def band(audio, rate, low, high):
    # An FFT mask has no phase shift, so every option is filtered identically.
    size = fft.next_fast_len(len(audio))
    spectrum = fft.rfft(audio, size, axis=0)
    freqs = fft.rfftfreq(size, 1 / rate)
    spectrum[(freqs < low) | (freqs >= high)] = 0
    return fft.irfft(spectrum, size, axis=0)[: len(audio)]


def windows(audio, starts, width):
    padded = np.concatenate([audio, np.zeros(width)])
    return np.stack([padded[t : t + width] for t in starts])


def search(reference, mic, starts, width, limit):
    ref = windows(reference, starts, width)
    padded = np.concatenate([np.zeros(limit), mic, np.zeros(width + limit)])
    long = np.stack([padded[t : t + width + 2 * limit] for t in starts])
    size = fft.next_fast_len(width + 2 * limit + width)
    cross = fft.irfft(fft.rfft(long, size) * np.conj(fft.rfft(ref, size)), size)[:, : 2 * limit + 1]
    squares = np.concatenate([np.zeros((len(starts), 1)), np.cumsum(long**2, axis=1)], axis=1)
    mic_energy = squares[:, width : width + 2 * limit + 1] - squares[:, : 2 * limit + 1]
    ref_energy = (ref**2).sum(axis=1, keepdims=True)
    # cross[:, k] lines the mic up k samples later than in long, i.e. shifted by limit - k.
    shifts = limit - np.arange(2 * limit + 1)
    return shifts, ref_energy, mic_energy, cross


def gain_db(ref_energy, mic_energy, cross, sign):
    total = ref_energy + mic_energy
    return 10 * np.log10(np.maximum(total + 2 * sign * cross, 1e-30) / np.maximum(total, 1e-30))


def hit_times(recipe, song, audio, rate, times_from):
    if times_from is None:
        return detect(audio**2, rate)
    # The timing stem is read unshifted, so trying offsets on it doesn't move the windows being compared.
    source, _ = placed(recipe, song, times_from, offset=0, invert=False)
    return detect(source**2, rate)


def pair(recipe, song, reference, mic, low, high, limit_ms, times_from):
    ref_audio, rate = placed(recipe, song, reference)
    mic_audio, _ = placed(recipe, song, mic, offset=0, invert=False)
    length = max(len(ref_audio), len(mic_audio))
    ref_audio, mic_audio = (np.pad(a, (0, length - len(a))) for a in (ref_audio, mic_audio))
    starts = hit_times(recipe, song, ref_audio, rate, times_from)
    starts = starts[starts < length]
    width, limit = round(WINDOW_MS / 1000 * rate), round(limit_ms / 1000 * rate)
    shifts, ref_energy, mic_energy, cross = search(band(ref_audio, rate, low, high), band(mic_audio, rate, low, high), starts, width, limit)
    current = recipe["stems"][mic]
    options = {}
    for invert, sign in ((False, 1), (True, -1)):
        total = gain_db(ref_energy.sum(), mic_energy.sum(axis=0), cross.sum(axis=0), sign)
        per_hit = shifts[np.argmax(gain_db(ref_energy, mic_energy, cross, sign), axis=1)]
        best = int(np.argmax(total))
        options["inverted" if invert else "normal"] = {
            "best_offset_samples": int(shifts[best]),
            "best_sum_gain_db": round(float(total[best]), 2),
            "sum_gain_at_0_db": round(float(total[limit]), 2),
            "per_hit_best_offset": {f"p{p}": int(np.percentile(per_hit, p)) for p in (10, 50, 90)},
            "best_at_search_edge": bool(best in (0, len(shifts) - 1)),
        }
    now = current.get("offset_samples", 0)
    now_sign = -1 if current.get("invert", False) else 1
    now_gain = None
    if abs(now) <= limit:
        index = limit - now
        now_gain = round(float(gain_db(ref_energy.sum(), mic_energy[:, index].sum(), cross[:, index].sum(), now_sign)), 2)
    return {"reference": reference, "mic": mic, "band_hz": [low, high], "hits": len(starts), "recipe_sum_gain_db": now_gain, **options}


def blend(recipe, song, names, low, high, times_from):
    audios = [placed(recipe, song, name, stereo=True)[0] for name in names]
    rate = soundfile.info(song / "stems" / names[0]).samplerate
    length = max(len(a) for a in audios)
    audios = [np.pad(a, ((0, length - len(a)), (0, 0))) for a in audios]
    starts = hit_times(recipe, song, None, rate, times_from or names[0])
    starts = starts[starts < length]
    width = round(WINDOW_MS / 1000 * rate)
    # Panned as in the render, so hard-panned mics don't count as summing.
    banded = [np.stack([windows(channel, starts, width) for channel in band(a, rate, low, high).T]) for a in audios]
    gain = 10 * np.log10((sum(banded) ** 2).sum() / sum((w**2).sum() for w in banded))
    total = sum(audios)
    # Same hits as the sum gain, but starting at the most advanced stem, whose attack now comes first.
    lead = min(0, *(recipe["stems"][name].get("offset_samples", 0) for name in names))
    attack = measure(total, rate, np.maximum(starts + lead, 0))["attack_db"]
    return {
        "stems": names,
        "band_hz": [low, high],
        "hits": len(starts),
        "sum_gain_db": round(float(gain), 2),
        "attack_db_p50": round(float(np.median(attack)), 2),
    }


USAGE = (
    "usage: uv run .agents/skills/align/align.py <recipe.json> <reference> <mic> <low_hz> <high_hz> [--limit-ms 20] [--times-from <stem>]\n"
    "       uv run .agents/skills/align/align.py <recipe.json> --blend <low_hz> <high_hz> <stem> <stem>... [--times-from <stem>]"
)


def main():
    args = sys.argv[1:]
    options = {"--limit-ms": LIMIT_MS, "--times-from": None}
    for name in options:
        if name in args:
            index = args.index(name)
            if index + 1 >= len(args):
                sys.exit(USAGE)
            options[name] = args[index + 1]
            del args[index : index + 2]
    times_from = options["--times-from"]
    if len(args) < 4:
        sys.exit(USAGE)
    path = Path(args[0])
    recipe, song = json.loads(path.read_text()), path.parent
    if args[1] == "--blend" and len(args) >= 6 and options["--limit-ms"] == LIMIT_MS:
        names = args[4:]
    elif len(args) == 5 and args[1] != "--blend":
        names = args[1:3]
    else:
        sys.exit(USAGE)
    if missing := [name for name in names + [times_from] if name is not None and name not in recipe["stems"]]:
        sys.exit(f"not in the recipe's stems: {missing}")
    if args[1] == "--blend":
        result = blend(recipe, song, names, float(args[2]), float(args[3]), times_from)
    else:
        result = pair(recipe, song, args[1], args[2], float(args[3]), float(args[4]), float(options["--limit-ms"]), times_from)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
