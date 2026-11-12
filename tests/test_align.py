import math

import numpy as np
import pytest

import align
from helpers import RATE, write

OFFSET = 37
# A mic at half the reference's level, summed in phase: |1 + 0.5|^2 over 1 + 0.5^2.
IN_PHASE_DB = 10 * math.log10(1.5**2 / 1.25)


def hits(seconds=7, every=0.5):
    """Short decaying noise bursts with silence between, so every hit is found."""
    rng = np.random.default_rng(1)
    out, length = np.zeros(round(seconds * RATE)), round(0.03 * RATE)
    for start in np.arange(0.2, seconds - 0.5, every):
        index = round(start * RATE)
        out[index : index + length] += rng.standard_normal(length) * np.exp(-np.arange(length) / (0.005 * RATE))
    return out


@pytest.fixture
def mics(song):
    """A bottom mic that hears the top OFFSET samples late, at half level, in opposite polarity."""
    top = hits()
    bottom = np.zeros_like(top)
    bottom[OFFSET:] = -0.5 * top[:-OFFSET]
    write(song, "top.wav", top)
    write(song, "bottom.wav", bottom)
    return song


def test_pair_recovers_offset_and_polarity(mics):
    recipe = {"stems": {"top.wav": {}, "bottom.wav": {"offset_samples": -OFFSET, "invert": True}}}

    found = align.pair(recipe, mics, "top.wav", "bottom.wav", 20, 20000, align.LIMIT_MS, None)

    inverted = found["inverted"]
    assert inverted["best_offset_samples"] == -OFFSET
    assert inverted["per_hit_best_offset"] == {"p10": -OFFSET, "p50": -OFFSET, "p90": -OFFSET}
    assert inverted["best_sum_gain_db"] == pytest.approx(IN_PHASE_DB, abs=0.01)
    assert inverted["best_correlation"] == 1.0
    assert found["normal"]["best_sum_gain_db"] < inverted["best_sum_gain_db"]
    assert found["recipe_sum_gain_db"] == inverted["best_sum_gain_db"]


@pytest.mark.parametrize("bottom, expected_db", [
    ({"offset_samples": -OFFSET, "invert": True}, IN_PHASE_DB),
    ({"offset_samples": -OFFSET}, 10 * math.log10(0.5**2 / 1.25)),
])
def test_blend_scores_the_recipe_alignment(mics, bottom, expected_db):
    recipe = {"stems": {"top.wav": {}, "bottom.wav": bottom}}

    found = align.blend(recipe, mics, ["top.wav", "bottom.wav"], 20, 20000, None)

    assert found["sum_gain_db"] == pytest.approx(expected_db, abs=0.01)
