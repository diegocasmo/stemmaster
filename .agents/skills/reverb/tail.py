import json
import sys
from pathlib import Path

import numpy as np
import pedalboard

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))
from render import PLUGINS, dry_error, load

SECONDS = 30
WINDOW_S = 0.01


def response(step, rate):
    impulse = np.zeros((2, SECONDS * rate), dtype=np.float32)
    impulse[:, rate // 10] = 1
    return load(step)(impulse, rate).T[rate // 10 :].astype(np.float64)


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: uv run .agents/skills/reverb/tail.py <plugin> '<settings json>' [<sample_rate>]")
    step = {"id": "tail", "plugin": sys.argv[1], "settings": json.loads(sys.argv[2])}
    if not (PLUGINS / f"{step['plugin']}.vst3").exists() and not hasattr(pedalboard, step["plugin"]):
        sys.exit(f"unknown plugin {step['plugin']}")
    if error := dry_error(step):
        sys.exit(error)
    rate = int(sys.argv[3]) if len(sys.argv) == 4 else 44100
    out = response(step, rate)
    level = np.abs(out).max(axis=1)
    onset = np.argmax(level > level.max() * 0.01) / rate
    energy = (out**2).sum(axis=1)
    remaining = 10 * np.log10(np.cumsum(energy[::-1])[::-1] / energy.sum() + 1e-300)
    start, end = np.argmax(remaining < -5), np.argmax(remaining < -35)
    slope = np.polyfit(np.arange(start, end) / rate, remaining[start:end], 1)[0] if end > start else None
    width = int(WINDOW_S * rate)
    count = len(out) // width
    windows = 10 * np.log10((out[: count * width] ** 2).sum(axis=1).reshape(count, width).mean(axis=1) + 1e-300)

    def last_above(drop_db):
        above = np.nonzero(windows > windows.max() - drop_db)[0]
        return round((above[-1] + 1) * width / rate, 2)

    print(json.dumps({
        "onset_ms": round(1000 * onset, 1),
        "decay_rt60_s": round(-60 / slope, 2) if slope else None,
        "tail_60db_s": last_above(60),
        "tail_90db_s": last_above(90),
        "tail_reaches_end": bool(last_above(90) >= SECONDS - 0.2),
        "left_right_correlation": round(float(np.corrcoef(out[:, 0], out[:, 1])[0, 1]), 2),
    }))


if __name__ == "__main__":
    main()
