import math

import pytest

import contracts
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


def leaf_state(recipe, song):
    """The blend leaf as a contract reads it, so a return's doubt can't hide a shortfall that already fails."""
    item = contracts.leaf("best option", "blend_shortfall", "<=", 0.3, band=(20, 20000))
    return outcomes.evaluate_leaf(item, {"source": "snare"}, {"base": view(recipe, song)}, None)["state"]


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

    assert leaf_state(recipe, song) == outcomes.FALSE


def test_returns_that_cancel_the_blend_do_not_pass(song):
    recipe = snare(song, top=1, bottom=1)
    recipe["buses"].update({"top-verb": {}, "bottom-verb": {}})
    recipe["stems"]["SnareTop.wav"]["sends"] = [{"bus": "top-verb"}]
    recipe["stems"]["SnareBottom.wav"]["sends"] = [{"bus": "bottom-verb"}]
    recipe["steps"] += [gain("top-verb", "top-verb", 60), gain("bottom-verb", "bottom-verb", 60),
                        {"id": "flip", "target": "bottom-verb", "plugin": "Invert", "settings": {}}]

    assert leaf_state(recipe, song) == outcomes.REVIEW


def test_every_mic_sent_apart_to_one_return_is_still_a_question(song):
    recipe = snare(song, top=1, bottom=1)
    recipe["buses"]["verb"] = {}
    for stem, gain_db in (("SnareTop.wav", -100), ("SnareBottom.wav", 0), ("SnareSample.wav", -100)):
        recipe["stems"][stem]["sends"] = [{"bus": "verb", "gain_db": gain_db}]
    recipe["steps"] += [gain("verb-gain", "verb", 6.02), {"id": "flip", "target": "verb", "plugin": "Invert", "settings": {}}]

    assert leaf_state(recipe, song) == outcomes.REVIEW


def test_a_return_fed_by_every_mic_reads_the_blend_as_without_it(song):
    recipe = snare(song, top=1, bottom=-1)
    recipe["stems"]["SnareTop.wav"]["bus"] = recipe["stems"]["SnareBottom.wav"]["bus"] = "snare"
    recipe["stems"]["SnareSample.wav"]["bus"] = "snare"
    recipe["buses"] |= {"snare": {"sends": [{"bus": "verb"}]}, "verb": {}}

    assert shortfall(recipe, song) == pytest.approx(OPPOSITE_SHORTFALL_DB, abs=0.01)
