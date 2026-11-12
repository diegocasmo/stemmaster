import math

import pytest

import outcomes
from helpers import view, write
from test_align import hits

# Two loud opposite-polarity copies of one mic plus a 1% sample: flipping one copy turns the 0.01 residue into 2.01.
OPPOSITE_SHORTFALL_DB = 20 * math.log10(2.01 / 0.01)


def snare(song, top, bottom):
    x = hits()
    write(song, "SnareTop.wav", top * x)
    write(song, "SnareBottom.wav", bottom * x)
    write(song, "SnareSample.wav", 0.01 * x)
    return {
        "stems": {"SnareTop.wav": {}, "SnareBottom.wav": {}, "SnareSample.wav": {}},
        "buses": {},
        "sources": {"snare": {"chain": "snare", "stems": ["SnareTop.wav", "SnareBottom.wav", "SnareSample.wav"],
                              "timing": "SnareSample.wav"}},
        "steps": [],
    }


def shortfall(recipe, song):
    return outcomes.m_blend_shortfall(view(recipe, song), "snare", {"band": (20, 20000)})


def gain(step_id, target, gain_db):
    return {"id": step_id, "target": target, "plugin": "Gain", "settings": {"gain_db": gain_db}}


def own_gain_steps(recipe):
    recipe["stems"]["SnareTop.wav"]["gain_db"] = -20
    recipe["steps"].append(gain("top", "SnareTop.wav", 20))


def own_bus_gain_steps(recipe):
    recipe["stems"]["SnareBottom.wav"].update(gain_db=-20, bus="bottom")
    recipe["buses"]["bottom"] = {}
    recipe["steps"].append(gain("bottom", "bottom", 20))


def quiet_sends_to_loud_returns(recipe):
    recipe["buses"]["top-verb"] = {}
    recipe["stems"]["SnareTop.wav"]["sends"] = [{"bus": "top-verb", "gain_db": -100}]
    recipe["steps"].append(gain("top-verb", "top-verb", 60))


@pytest.mark.parametrize("change", [lambda recipe: None, own_gain_steps, own_bus_gain_steps],
                         ids=["plain", "gain-on-stem", "gain-on-own-bus"])
def test_opposite_copies_read_the_same_however_their_level_is_set(song, change):
    recipe = snare(song, top=1, bottom=-1)
    change(recipe)

    assert shortfall(recipe, song) == pytest.approx(OPPOSITE_SHORTFALL_DB, abs=0.01)


def test_opposite_copies_with_a_loud_return_still_fail(song):
    recipe = snare(song, top=1, bottom=-1)
    quiet_sends_to_loud_returns(recipe)

    assert shortfall(recipe, song) > 0.3


@pytest.mark.xfail(raises=AssertionError, reason="#4: the blend score leaves out parallel returns, so a cancelling return reads as a pass")
def test_returns_that_cancel_the_blend_do_not_pass(song):
    recipe = snare(song, top=1, bottom=1)
    recipe["buses"].update({"top-verb": {}, "bottom-verb": {}})
    recipe["stems"]["SnareTop.wav"]["sends"] = [{"bus": "top-verb"}]
    recipe["stems"]["SnareBottom.wav"]["sends"] = [{"bus": "bottom-verb"}]
    recipe["steps"] += [gain("top-verb", "top-verb", 60), gain("bottom-verb", "bottom-verb", 60),
                        {"id": "flip", "target": "bottom-verb", "plugin": "Invert", "settings": {}}]

    found = shortfall(recipe, song)

    assert found is None or isinstance(found, outcomes.Review) or found > 0.3
