import io
import json
import sys
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "render"))
from render import Mix, check, master_target, process, timeline

TOLERANCE_LU = 0.004
SEARCH_DB = np.arange(-24, 48.1, 2)


def loudness(mix, rate, steps, gain_step, gain, mixer):
    gain_step["settings"]["gain_db"] = gain
    # Measure the 24-bit file render writes, not the float signal.
    buffer = io.BytesIO()
    soundfile.write(buffer, process(mix, rate, steps, "mix", mixer), rate, subtype="PCM_24", format="WAV")
    buffer.seek(0)
    audio, _ = soundfile.read(buffer, always_2d=True)
    return pyloudnorm.Meter(rate).integrated_loudness(audio)


def solve(recipe_path):
    recipe_path = Path(recipe_path)
    recipe = json.loads(recipe_path.read_text())
    song = recipe_path.parent
    rate, length = timeline(recipe, song)
    check(recipe, rate, length)
    target = master_target(recipe)
    steps = [step for step in recipe["steps"] if step.get("stage") == "master"]
    gains = [step for step in steps if step["plugin"] == "Gain"]
    if len(gains) != 1 or steps[0] is not gains[0]:
        sys.exit("master steps must start with exactly one Gain step")
    # A fresh whole-song mix: the cached mix.wav may be a clip or out of date.
    mixer = Mix(recipe, song, rate, length)
    mix = mixer.render()
    mix = mix.astype(np.float32).astype(np.float64)

    def error(gain):
        return loudness(mix, rate, steps, gains[0], float(gain), mixer) - target

    # A limiter's loudness can stop rising or even fall as gain rises, so take the lowest crossing.
    low, low_error = None, None
    for gain in SEARCH_DB:
        current = error(gain)
        if abs(current) <= TOLERANCE_LU:
            return save(recipe_path, recipe, gains[0], float(gain), target + current)
        if current > 0:
            if low is None:
                sys.exit(f"{current:+.3f} LU over the target even at {gain} dB")
            high, high_error = gain, current
            break
        low, low_error = gain, current
    else:
        sys.exit(f"never reaches {target} LUFS up to +{SEARCH_DB[-1]} dB; check the limiter settings")

    side = 0
    for _ in range(60):
        gain = round(low - low_error * (high - low) / (high_error - low_error), 3)
        current = error(gain)
        if abs(current) <= TOLERANCE_LU:
            break
        # Illinois step: halving the stale end keeps regula falsi from crawling on a flat curve.
        if current < 0:
            low, low_error = gain, current
            high_error /= 2 if side == -1 else 1
            side = -1
        else:
            high, high_error = gain, current
            low_error /= 2 if side == 1 else 1
            side = 1
    else:
        sys.exit("did not converge; check the limiter settings")

    return save(recipe_path, recipe, gains[0], gain, target + current)


def save(recipe_path, recipe, gain_step, gain, lufs):
    gain_step["settings"]["gain_db"] = gain
    recipe_path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n")
    return gain, lufs


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: uv run .agents/skills/limit/solve.py <recipe.json>")
    gain, lufs = solve(sys.argv[1])
    print(f"gain_db {gain}: {lufs:.4f} LUFS")
