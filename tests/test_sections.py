import math

import numpy as np
import pytest

import outcomes
from helpers import RATE, noise, view, write

EDGE = 10


def mask(*runs, length=100):
    out = np.zeros(length, dtype=bool)
    for start, end in runs:
        out[start:end] = True
    return out


@pytest.mark.parametrize("playing, expected", [
    (mask(), None),
    (mask((0, 45)), outcomes.EDGE),
    (mask((55, 100)), outcomes.EDGE),
    # The same short run, but not carried across a boundary.
    (mask((40, 45)), "unread"),
    (mask((0, 40 + EDGE)), "unread"),
], ids=["rest", "ring-out", "pickup", "short-inside", "long-ring-out"])
def test_plays_in(playing, expected):
    found = outcomes.plays_in(playing, 40, 60, EDGE)

    if expected == "unread":
        assert np.isnan(found).all()
    else:
        assert found is expected


def synth(song, audio, sections):
    write(song, "Synth.wav", audio)
    return {"stems": {"Synth.wav": {}}, "sources": {"synth": {"chain": "synths", "role": "pad", "stems": ["Synth.wav"]}},
            "sections": sections}


def test_section_readings_follow_the_level_and_leave_out_rests(song):
    audio = np.concatenate([noise(4), 2 * noise(4), np.zeros(4 * RATE)])
    sections = {"a": {"start_s": 0, "end_s": 4, "group": "verse"}, "b": {"start_s": 4, "end_s": 8, "group": "verse"},
                "rest": {"start_s": 8, "end_s": 12, "group": "verse"}}

    found = outcomes.section_levels(view(synth(song, audio, sections), song), "node", "synth")

    assert found["b"][1] - found["a"][1] == pytest.approx(20 * math.log10(2), abs=0.01)
    assert found["rest"] == ("verse", None)


def test_a_note_dying_into_a_rest_is_not_played_in_the_next_section(song):
    seconds = np.arange(4 * RATE) / RATE
    # 20 dB a second: 40 dB down, under the played floor, 2 s into the next section.
    audio = np.concatenate([noise(4), noise(4, seed=1) * 10 ** (-20 * seconds / 20)])
    sections = {"a": {"start_s": 0, "end_s": 4}, "b": {"start_s": 4, "end_s": 8}}
    v = view(synth(song, audio, sections), song)

    assert outcomes.played_mask(v, "node", "synth")[4 * RATE : 5 * RATE].all()
    assert outcomes.section_readings(v, "node", "synth")["b"] == (None, None)


@pytest.mark.parametrize("then", [noise(2, seed=1), np.concatenate([noise(1, seed=1), noise(1, seed=2) * 10 ** (-12 / 20)])],
                         ids=["steady", "drops-12-db"])
def test_a_part_that_plays_on_then_stops_is_played_in_the_next_section(song, then):
    audio = np.concatenate([noise(4), then, np.zeros(2 * RATE)])
    sections = {"a": {"start_s": 0, "end_s": 4}, "b": {"start_s": 4, "end_s": 8}}
    v = view(synth(song, audio, sections), song)

    assert outcomes.played_mask(v, "node", "synth", 4 * RATE)[4 * RATE : 6 * RATE].all()
