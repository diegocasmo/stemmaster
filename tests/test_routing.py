import pytest

import outcomes
import structure


def step(step_id, target, plugin="Compressor"):
    return {"id": step_id, "target": target, "plugin": plugin, "settings": {}}


def acting(recipe, step_ids, source):
    routing = structure.Routing(recipe)
    stems = recipe["sources"][source]["stems"]
    node = structure.nodes(recipe, routing, [])[source]
    chain = recipe["sources"][source]["chain"]
    chain_stems = [s for src in recipe["sources"].values() if src["chain"] == chain for s in src["stems"]]
    return outcomes.acting_point(recipe, routing, step_ids, node, stems, chain_stems)


def toms():
    return {
        "stems": {"Tom1.wav": {"bus": "toms"}, "Tom2.wav": {"bus": "toms"}},
        "buses": {"toms": {}},
        "sources": {"tom1": {"chain": "toms", "stems": ["Tom1.wav"]}, "tom2": {"chain": "toms", "stems": ["Tom2.wav"]}},
        "steps": [step("tom1-comp", "Tom1.wav"), step("toms-comp", "toms"), step("toms-gain", "toms", "Gain")],
    }


def guitars():
    return {
        "stems": {"GtrL.wav": {"bus": "rhythms"}, "GtrR.wav": {"bus": "rhythms"}, "GtrLead.wav": {"bus": "gtr"}},
        "buses": {"rhythms": {"bus": "gtr"}, "gtr": {}},
        "sources": {"guitars": {"chain": "guitars", "stems": ["GtrL.wav", "GtrR.wav", "GtrLead.wav"]}},
        "steps": [step("rhythm-eq", "rhythms"), step("gtr-eq", "gtr")],
    }


def kick(verb_feeds_snare):
    """Two kick mics on a kick bus with a parallel return, both into a drums bus that also carries the snare."""
    recipe = {
        "stems": {"KickIn.wav": {"bus": "kick"}, "KickOut.wav": {"bus": "kick"}, "Snare.wav": {"bus": "drums"}},
        "buses": {"kick": {"bus": "drums", "sends": [{"bus": "kick-verb"}]}, "kick-verb": {"bus": "drums"}, "drums": {}},
        "sources": {"kick": {"chain": "kick", "stems": ["KickIn.wav", "KickOut.wav"]},
                    "snare": {"chain": "snare", "stems": ["Snare.wav"]}},
        "steps": [step("kick-comp", "kick"), step("kick-verb", "kick-verb", "Reverb")],
    }
    if verb_feeds_snare:
        recipe["stems"]["Snare.wav"]["sends"] = [{"bus": "kick-verb"}]
    return recipe


@pytest.mark.parametrize("recipe, steps, source, expected", [
    # A tom's own compressor then the shared tom-bus one: read after the last, past the tom's node.
    (toms(), ["tom1-comp", "toms-comp"], "tom1", "toms"),
    (toms(), ["tom1-comp", "toms-gain"], "tom1", None),
    (guitars(), ["rhythm-eq"], "guitars", "rhythms"),
    (guitars(), ["gtr-eq"], "guitars", None),
    # Parallel paths meeting on a drums bus with other parts: read the sum of the kick's own inputs to it.
    (kick(verb_feeds_snare=False), ["kick-comp", "kick-verb"], "kick", ("kick", "kick-verb")),
    # The return carries the snare too, so no point reads the kick's paths alone.
    (kick(verb_feeds_snare=True), ["kick-comp", "kick-verb"], "kick", outcomes.UNREAD),
], ids=["series-past-node", "gain-ignored", "group-inside-node", "on-node", "parallel-own", "parallel-shared"])
def test_acting_point(recipe, steps, source, expected):
    assert acting(recipe, steps, source) == expected
