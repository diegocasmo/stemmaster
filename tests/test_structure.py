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
