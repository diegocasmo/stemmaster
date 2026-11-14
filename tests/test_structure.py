import pytest

import chains
import structure

DOCS = chains.parse()[0]


@pytest.mark.parametrize("role, expected", [
    ("bass", {"synths": list(DOCS["synths"]), "bass": list(DOCS["bass"])}),
    ("lead", {"synths": list(DOCS["synths"]), "guitars": ["guitars.lead"]}),
    ("pad", {"synths": list(DOCS["synths"])}),
])
def test_a_synth_gets_its_role_moves(role, expected):
    recipe = {"sources": {"synth": {"chain": "synths", "role": role, "stems": ["Synth.wav"]}}}

    found = {chain: structure.moves_for(recipe, name, chain, DOCS)
             for name, chain in structure.instances(recipe) if name == "synth"}

    assert found == expected


def drum_bus_trial(song, trial):
    from helpers import noise, write

    for name in ("Kick.wav", "Snare.wav"):
        write(song, name, noise(1))
    recipe = {"stems": {"Kick.wav": {"bus": "shells"}, "Snare.wav": {"bus": "shells"}}, "buses": {"shells": {}}, "steps": [],
              "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]}}}
    record = {"move": "drum-bus.distort-parallel", "source": "drum-bus", "decision": "dropped", "trial": trial}
    errors = []
    structure.check_trial(recipe, song, record, DOCS["drum-bus"]["drum-bus.distort-parallel"], "trial", errors, [])
    return errors


def test_a_parallel_trial_with_its_own_steps_is_not_a_reverb_send(song):
    trial = {"buses": {"dist": {}}, "sends": {"shells": [{"bus": "dist", "gain_db": -24}]},
             "steps": [{"id": "trial-dist", "target": "dist", "plugin": "Distortion", "settings": {"drive_db": 20}}]}
    assert drum_bus_trial(song, trial) == []


def test_a_send_only_trial_still_needs_a_reverb_return(song):
    trial = {"buses": {"dist": {}}, "sends": {"shells": [{"bus": "dist", "gain_db": -24}]}}
    assert any("reverb return" in error for error in drum_bus_trial(song, trial))


def test_a_reverb_trial_with_steps_still_needs_a_reverb_return(song):
    trial = {"steps": [{"id": "trial-room", "target": "shells", "plugin": "Reverb", "settings": {}}],
             "sends": {"shells": [{"bus": "fx", "gain_db": -12}]}}
    from helpers import noise, write

    for name in ("Kick.wav", "Snare.wav"):
        write(song, name, noise(1))
    recipe = {"stems": {"Kick.wav": {"bus": "shells"}, "Snare.wav": {"bus": "shells", "sends": [{"bus": "fx"}]}},
              "buses": {"shells": {}, "fx": {}}, "steps": [],
              "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]}}}
    record = {"move": "drum-bus.reverb", "source": "drum-bus", "decision": "dropped", "trial": trial, "tool_reason": "stand-in"}
    errors = []
    structure.check_trial(recipe, song, record, DOCS["drum-bus"]["drum-bus.reverb"], "trial", errors, [])
    assert any("reverb return" in error for error in errors)


def test_a_parallel_return_is_read_with_the_dry_node():
    import outcomes

    recipe = {"stems": {"Bass.wav": {"sends": [{"bus": "growl"}]}, "Kick.wav": {}}, "buses": {"growl": {}},
              "steps": [{"id": "growl-dist", "target": "growl", "plugin": "Distortion", "settings": {}}]}
    found = outcomes.acting_point(recipe, structure.Routing(recipe), ["growl-dist"], "Bass.wav", ["Bass.wav"], parallel=True)
    assert set(found) == {"Bass.wav", "growl"}
