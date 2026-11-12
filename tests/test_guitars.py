import math

import numpy as np
import pyloudnorm
import pytest
from scipy import signal

import outcomes
from helpers import RATE, noise, view, write

SECONDS = 4
LOPSIDED_DB = 6.0
# The lead's power over both hard-panned rhythms', all from one noise, with the lead raised LEAD_DB.
LEAD_DB = 6.0
LEAD_GAP_DB = LEAD_DB - 10 * math.log10(2)


def guitars(song, left=None, right=None):
    """Two hard-panned rhythms and a centred lead on one guitars bus, plus a kick, all written from one noise."""
    tone = noise(SECONDS)
    for name, audio in (("GtrL.wav", left), ("GtrR.wav", right), ("GtrLead.wav", None), ("Kick.wav", None)):
        write(song, name, tone if audio is None else audio)
    return {
        "stems": {
            "GtrL.wav": {"pan": -1, "bus": "gtr"},
            "GtrR.wav": {"pan": 1, "bus": "gtr"},
            "GtrLead.wav": {"gain_db": LEAD_DB, "bus": "gtr"},
            "Kick.wav": {},
        },
        "buses": {"gtr": {}},
        "sources": {
            "guitars": {"chain": "guitars", "stems": ["GtrL.wav", "GtrR.wav", "GtrLead.wav"]},
            "kick": {"chain": "kick", "stems": ["Kick.wav"]},
        },
    }


def lopsided(recipe):
    recipe["stems"]["GtrR.wav"]["gain_db"] = -LOPSIDED_DB
    return recipe


def lr(recipe, song, measure="lr_loudness"):
    return outcomes.MEASURES[measure](view(recipe, song), "guitars", {"point": "rhythm"})


def not_a_pass(found):
    """The pair contract passes under 1 dB: a measured fail or a review question both keep it from passing."""
    return isinstance(found, outcomes.Review) or found >= 1


def lead_gap(recipe, song):
    return outcomes.m_lead_gap(view(recipe, song), "guitars", {"band": (1000, 4000)})


def test_lopsided_pair_reads_its_level_not_its_tone(song):
    recipe = lopsided(guitars(song))

    assert lr(recipe, song) == pytest.approx(LOPSIDED_DB, abs=0.01)
    assert lr(recipe, song, "lr_band_diff") == pytest.approx(0, abs=0.01)


SHELF = {"id": "shelf", "target": "gtr", "plugin": "HighShelfFilter", "settings": {"cutoff_frequency_hz": 4000, "gain_db": -3}}


def test_linear_shared_filter_over_matching_tones_keeps_the_reading(song):
    recipe = lopsided(guitars(song))
    recipe["steps"] = [SHELF]

    assert lr(recipe, song) == pytest.approx(LOPSIDED_DB, abs=0.01)


def test_linear_shared_filter_over_different_tones_does_not_pass(song):
    meter = pyloudnorm.Meter(RATE)
    # Equally loud before the shelf, so only the shelf, cutting the bright side more, makes them lopsided.
    sides = [signal.sosfilt(signal.butter(4, cutoff, kind, fs=RATE, output="sos"), noise(SECONDS))
             for cutoff, kind in ((500, "lowpass"), (4000, "highpass"))]
    dark, bright = (side * 10 ** ((-30 - meter.integrated_loudness(np.repeat(side[:, None], 2, axis=1))) / 20) for side in sides)
    recipe = guitars(song, left=dark, right=bright)
    recipe["steps"] = [SHELF]

    assert not_a_pass(lr(recipe, song))


# Clipping only scales a pulse train of 0.2 down to the -18 dB threshold.
CLIPPED_DB = 20 * math.log10(0.2) + 18


def clipped_pair(song):
    """Rhythms equally loud before the shared bus: a square under the clip and a pulse train over it."""
    meter = pyloudnorm.Meter(RATE)
    lufs = lambda x: meter.integrated_loudness(np.repeat(x[:, None], 2, axis=1))
    pulses = np.tile([0.2] * 6 + [0] * 18 + [-0.2] * 6 + [0] * 18, SECONDS * RATE // 48)
    square = np.where(np.arange(SECONDS * RATE) // 24 % 2, -1.0, 1.0)
    square *= 10 ** ((lufs(pulses) - lufs(square)) / 20)
    recipe = guitars(song, left=square, right=pulses)
    recipe["steps"] = [{"id": "clip", "target": "gtr", "plugin": "Clipping", "settings": {"threshold_db": -18}}]
    return recipe


def test_shared_bus_dynamics_do_not_pass(song):
    assert not_a_pass(lr(clipped_pair(song), song))


@pytest.mark.xfail(raises=AssertionError,
                   reason="#6: processing on a bus shared with the lead makes a lopsided pair a review question, not a fail")
def test_shared_bus_dynamics_are_measured(song):
    assert lr(clipped_pair(song), song) == pytest.approx(CLIPPED_DB, abs=0.01)


@pytest.mark.xfail(raises=AssertionError, reason="#5: a rhythm send to a reverb the lead shares leaves the pair unresolved")
@pytest.mark.parametrize("measure, expected", [("lr_loudness", LOPSIDED_DB), ("lr_band_diff", 0)])
def test_rhythm_send_to_shared_reverb_reads_the_dry_pair(song, measure, expected):
    recipe = lopsided(guitars(song))
    recipe["buses"]["verb"] = {}
    for name in ("GtrL.wav", "GtrR.wav", "GtrLead.wav"):
        recipe["stems"][name]["sends"] = [{"bus": "verb", "gain_db": -18}]

    assert lr(recipe, song, measure) == pytest.approx(expected, abs=0.01)


def test_lead_gap_with_a_lead_only_delay(song):
    recipe = guitars(song)
    recipe["buses"]["delay"] = {}
    recipe["stems"]["GtrLead.wav"]["sends"] = [{"bus": "delay", "gain_db": -6}]

    assert lead_gap(recipe, song) == pytest.approx(LEAD_GAP_DB, abs=0.01)


def test_lead_gap_with_a_plate_shared_with_the_kick_is_a_review(song):
    recipe = guitars(song)
    recipe["buses"]["plate"] = {}
    for name in ("GtrLead.wav", "Kick.wav"):
        recipe["stems"][name]["sends"] = [{"bus": "plate", "gain_db": -6}]

    found = lead_gap(recipe, song)

    assert isinstance(found, outcomes.Review)
    assert found.value == pytest.approx(LEAD_GAP_DB, abs=0.01)


@pytest.mark.xfail(raises=AssertionError, reason="#7: a delay shared by the rhythms and the lead leaves the lead gap unresolved")
def test_lead_gap_with_a_delay_shared_by_the_rhythms_is_read(song):
    recipe = guitars(song)
    recipe["buses"]["delay"] = {}
    for name in ("GtrL.wav", "GtrR.wav", "GtrLead.wav"):
        recipe["stems"][name]["sends"] = [{"bus": "delay", "gain_db": -6}]

    found = lead_gap(recipe, song)
    value = found.value if isinstance(found, outcomes.Review) else found

    # The rhythms' sends only add to them, so a reading above the dry gap leaves them out.
    assert value is not None and value <= LEAD_GAP_DB + 0.01
