import json
import pickle
from pathlib import Path

import numpy as np
import pyloudnorm
import pytest
import soundfile

import analyze
import evidence
import outcomes
import render

RATE = 44100
ZL = render.PLUGINS / "ZL Limiter.vst3"


def signals(rate=RATE):
    rng = np.random.default_rng(7)
    noise = rng.standard_normal((3 * RATE, 2)) * 0.1
    gated = noise.copy()
    gated[RATE // 2 : 2 * RATE] *= 1e-5
    clicks = noise * 0.01
    for start in range(0, len(clicks), RATE // 3):
        clicks[start : start + 2000] += np.exp(-np.arange(min(2000, len(clicks) - start)) / 300)[:, None]
    return {
        "stereo": noise,
        "gated": gated,
        "clicks": clicks,
        "mono 1-d": noise[:, 0].copy(),
        "mono column": noise[:, :1].copy(),
        "five channels": rng.standard_normal((2 * RATE, 5)) * 0.2,
        "float32": noise.astype(np.float32),
        "fortran order": np.asfortranarray(noise),
        "strided": noise[::2],
        "just over a block": noise[: int(0.4 * rate) + 1],
        "loud": np.clip(noise * 40, -1, 1),
        "silent": np.zeros((RATE, 2)),
        "nan": np.where(np.arange(RATE)[:, None] == 9, np.nan, noise[:RATE]),
    }


def same(a, b):
    return np.float64(a).tobytes() == np.float64(b).tobytes()


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("name", list(signals()))
def test_gated_loudness_is_pyloudnorm_bit_for_bit(name, rate):
    audio = signals(rate)[name]
    assert same(analyze.gated_loudness(audio, rate), pyloudnorm.Meter(rate).integrated_loudness(audio))


def test_gated_loudness_refuses_what_pyloudnorm_refuses():
    for audio in (np.zeros((RATE // 4, 2)), np.zeros((RATE, 2), dtype=int), np.zeros((RATE, 6))):
        with pytest.raises(ValueError):
            pyloudnorm.Meter(RATE).integrated_loudness(audio)
        with pytest.raises(ValueError):
            analyze.gated_loudness(audio, RATE)


def reference_matched_master(result, target):
    """matched_master before this change: pyloudnorm per pass, and every pass probed."""
    def loudness(audio):
        try:
            return pyloudnorm.Meter(result.rate).integrated_loudness(audio)
        except ValueError:
            return float("-inf")

    def trial(gain_db):
        master_in, out = result.master(result.mix * 10 ** (gain_db / 20))
        return master_in, out, loudness(out) - target

    tried = {0.0: trial(0.0)}
    low, high, gain = -np.inf, np.inf, 0.0
    for _ in range(evidence.MATCH_ROUNDS):
        error = tried[gain][2]
        if not np.isfinite(error) or abs(error) <= evidence.MATCH_LU:
            break
        low, high = (low, min(high, gain)) if error > 0 else (max(low, gain), high)
        gain = gain - error
        if not low < gain < high:
            gain = (low + high) / 2
        tried[gain] = trial(gain)
    best = min(tried, key=lambda g: abs(tried[g][2]) if np.isfinite(tried[g][2]) else np.inf)
    master_in, out, error = tried[best]
    return {"drive_change_db": round(best, 2), "residual_lu": evidence.measure.rounded(error),
            "matched": bool(np.isfinite(error) and abs(error) <= evidence.MATCH_LU)}, master_in, out


LIMITERS = {"Limiter": {"threshold_db": -3}}
if ZL.exists():
    LIMITERS["ZL Limiter"] = json.loads((Path(__file__).parent / "prepared_settings.json").read_text())["ZL Limiter"]


def master_song(song, kind, limiter):
    rng = np.random.default_rng(3)
    drums = rng.standard_normal(2 * RATE) * 0.005
    for start in range(0, len(drums), RATE // 2):
        drums[start : start + 2000] += np.exp(-np.arange(min(2000, len(drums) - start)) / 300)
    bass = {"noise": rng.standard_normal((2 * RATE, 2)) * 0.05, "silent": np.zeros((2 * RATE, 2)),
            "quiet": rng.standard_normal((2 * RATE, 2)) * 1e-4}[kind]
    soundfile.write(song / "stems" / "Drums.wav", drums if kind != "silent" else drums * 0, RATE, subtype="FLOAT")
    soundfile.write(song / "stems" / "Bass.wav", bass, RATE, subtype="FLOAT")
    return {"stems": {"Drums.wav": {}, "Bass.wav": {}}, "steps": [
        {"id": "drive", "target": "mix", "plugin": "Gain", "stage": "master", "settings": {"gain_db": 0}},
        {"id": "comp", "target": "mix", "plugin": "Compressor", "stage": "master", "settings": {"threshold_db": -20, "ratio": 3}},
        {"id": "limit", "target": "mix", "plugin": limiter, "stage": "master", "settings": dict(LIMITERS[limiter])},
    ]}


PROBES = {"drive": outcomes.gain_change, "comp": outcomes.duck_depth, "limit": outcomes.engagement}


def made(recipe, song, boundary, matcher, target):
    result = render.Result(recipe, song, boundary=boundary, probes=PROBES)
    numbers, master_in, out, *level = matcher(result, target)
    return result, numbers, master_in, out, level


@pytest.mark.parametrize("limiter", list(LIMITERS))
@pytest.mark.parametrize("kind, target, boundary", [
    ("noise", -9.0, None), ("noise", -14.0, None), ("noise", -6.0, None), ("noise", -30.0, "comp"),
    ("quiet", -9.0, None), ("silent", -9.0, None),
])
def test_matched_master_is_unchanged(song, limiter, kind, target, boundary):
    recipe = master_song(song, kind, limiter)
    old = made(recipe, song, boundary, reference_matched_master, target)
    new = made(recipe, song, boundary, evidence.matched_master, target)
    assert pickle.dumps(new[1]) == pickle.dumps(old[1])
    for a, b in ((new[2], old[2]), (new[3], old[3])):
        assert (a.dtype, a.shape, a.flags.f_contiguous, a.tobytes()) == (b.dtype, b.shape, b.flags.f_contiguous, b.tobytes())
    # The probes the last pass left, in the same order, and the loudness the battery reads.
    assert pickle.dumps(list(new[0].mixer.probed.items())) == pickle.dumps(list(old[0].mixer.probed.items()))
    assert same(new[4][0], evidence.loudness(old[3], RATE))
