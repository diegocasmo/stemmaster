import copy
import json

import check
import defaults
import evidence
import structure
from test_apply_defaults import chain_recipe, measure, outcomes, song_recipe, step


def leaf(leaf_id, value, test, state, measure="band"):
    return {"id": leaf_id, "value": value, "test": test, "state": state, "measure": measure, "text": leaf_id}


def saturate(band, dip):
    """kick.saturate: (band rises > 0.5 or dip falls < -0.5) and attack holds."""
    state = lambda ok: "true" if ok else "false"
    return {"move": "kick.saturate", "source": "kick", "needed": "true", "worked": "true",
            "leaves": [{"id": "needed", "state": "true", "value": None, "text": "always tried"},
                       leaf("worked.0.0", band, "> 0.5", state(band > 0.5)), leaf("worked.0.1", dip, "< -0.5", state(dip < -0.5), "dip"),
                       leaf("worked.1", 0.0, ">= -0.5", "true", "attack_p50")]}


def test_clearance_is_in_the_leafs_units_and_positive_on_its_passing_side():
    assert defaults.clearance(leaf("x", 0.547, "> 0.5", "true")) == 0.047
    assert defaults.clearance(leaf("x", -0.33, "< -0.5", "false")) == -0.17
    assert defaults.clearance(leaf("x", 0, "== 0", "true")) is None
    assert defaults.clearance(leaf("x", 0.004, "<= 0.005", "true", "loudness_error")) is None


def test_a_strong_branch_of_an_any_hides_a_weak_one():
    assert defaults.fragile(saturate(band=2.0, dip=-0.52)) == []
    [(weak, gap)] = defaults.fragile(saturate(band=0.2, dip=-0.52))
    assert (weak["id"], gap) == ("worked.0.1", 0.02)


def test_a_record_reading_what_a_change_touched_is_provisional():
    before = chain_recipe()
    after = copy.deepcopy(before)
    after["steps"] = [s for s in after["steps"] if s["id"] != "s-eq"]
    after["moves"][3].update(decision="dropped", steps=[])
    found = {(r["move"], r["source"]) for r in defaults.provisional(before, after, [before["moves"][3]])}
    # The snare keys the overheads' Duck, and every change reaches the mix and its trial master; tom 1's spread and
    # peaks read only its own stem.
    assert found == {("overheads.snare-control", "cymbals"), ("overheads.hpf", "cymbals"), ("mix-bus.compress", "mix-bus"),
                     ("toms.saturate-clip", "tom1")}


class Fake:
    def __init__(self, recipes):
        self.recipes, self.views, self.batteries = recipes, {}, {}


def test_a_default_that_flips_back_stops_on_the_repeated_state_and_lists_its_rounds(tmp_path, monkeypatch, capsys):
    recipe = {"stems": {"Tom1.wav": {}}, "buses": {}, "sources": {"tom1": {"chain": "toms", "stems": ["Tom1.wav"]}},
              "steps": [step("t-comp", "Tom1.wav", "Compressor")],
              "moves": [{"move": "toms.compress", "source": "tom1", "decision": "kept", "steps": ["t-comp"], "note": "n"}]}
    original = copy.deepcopy(recipe)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe, indent=2) + "\n")
    monkeypatch.setattr(check, "structural", lambda r, s, m: ({}, [], []))
    monkeypatch.setattr(check, "renders", lambda r, s, m: [])
    monkeypatch.setattr(check, "release", lambda variants: None)

    def measured(recipe, song, nodes, master):
        kept = recipe["moves"][0]["decision"] == "kept"
        spread = 2.9 if kept else 3.2
        c = {"move": "toms.compress", "source": "tom1", "decision": recipe["moves"][0]["decision"],
             "outcome": "dropped" if kept else "kept", "needed": "false" if kept else "true", "worked": None if kept else "true",
             "with": "trial", "leaves": [leaf("needed", spread, "> 3", "false" if kept else "true", "spread")]
             + ([] if kept else [leaf("worked.0", -1.0, "< -0.5", "true", "spread")]),
             "fails": ["the default outcome is dropped; drop it, or give an override with a measured reason" if kept
                       else "the trial worked; keep it, or give an override with a measured reason"]}
        return tmp_path / "report.md", {"contracts": [c]}, Fake({"trial": original})

    monkeypatch.setattr(check, "measured", measured)
    _, _, _, numbers = check.apply_defaults(path, recipe, tmp_path, False)
    out = capsys.readouterr().out
    assert "applied, round 1: toms.compress on tom1: dropped" in out and "applied, round 2" not in out
    assert "stopped with 1 defaults not applied: its changes would bring back the recipe checked in round 1" in out
    assert ("flips: toms.compress on tom1: round 1: kept, default dropped (needed 2.9 > 3 (clears -0.1)); "
            "round 2: dropped, default kept (worked.0 -1.0 < -0.5 (clears 0.5))") in out
    # The file holds the recipe the last round checked, and the numbers are that round's.
    assert json.loads(path.read_text())["moves"][0]["decision"] == "dropped" == numbers["contracts"][0]["decision"]


def test_an_override_edit_reuses_every_contract_and_the_report_is_a_cold_checks(tmp_path, song):
    recipe = song_recipe(song)
    measure(recipe, song)
    cache = song / "renders" / "check"
    files = {p.name: p.stat().st_mtime_ns for p in cache.glob("contract-*.json")}
    edited = copy.deepcopy(recipe)
    edited["moves"][2]["override"] = {"reason": "another reason", "leaves": ["worked.1"]}
    _, numbers, _ = measure(edited, song)
    assert {p.name: p.stat().st_mtime_ns for p in cache.glob("contract-*.json")} == files
    warm = [(cache / n).read_bytes() for n in ("report.json", "report.md")]
    for p in cache.glob("*.json"):
        p.unlink()
    _, cold_numbers, _ = measure(edited, song)
    assert [(cache / n).read_bytes() for n in ("report.json", "report.md")] == warm
    # The narrower override no longer names every leaf it contradicts, and the cache doesn't hide that.
    assert outcomes(numbers)[("snare.reverb", "snare")]["fails"] == outcomes(cold_numbers)[("snare.reverb", "snare")]["fails"]
