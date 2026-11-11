import json
import math
import sys

import numpy as np
import soundfile
from scipy import signal


def spectrum(path, start, end, low, high):
    info = soundfile.info(path)
    rate = info.samplerate
    if not 0 <= start < end <= info.frames / rate:
        raise ValueError("needs 0 <= start < end <= file duration")
    if not 0 < low < high:
        raise ValueError("needs 0 < low < high")
    audio, _ = soundfile.read(
        path, start=round(start * rate), stop=round(end * rate), always_2d=True
    )
    frequencies, power = signal.welch(
        audio, rate, nperseg=min(65536, len(audio)), axis=0
    )
    power = power.sum(axis=1)
    total = power.sum()
    rows = []
    edge = low
    while edge < high:
        upper = min(edge * 2 ** (1 / 12), high)
        selected = (frequencies >= edge) & (frequencies < upper)
        energy = power[selected].sum()
        rows.append({
            "low_hz": round(edge, 2),
            "high_hz": round(upper, 2),
            "relative_db": round(10 * math.log10(energy / total), 2)
            if energy > 0 and total > 0 else None,
            "peak_hz": round(float(frequencies[selected][np.argmax(power[selected])]), 2)
            if energy > 0 else None,
        })
        edge = upper
    return {"bin_hz": rate / min(65536, len(audio)), "bands": rows}


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit("usage: uv run spectrum.py <wav> <start_s> <end_s> <low_hz> <high_hz>")
    print(json.dumps(spectrum(sys.argv[1], *map(float, sys.argv[2:])), indent=2))
