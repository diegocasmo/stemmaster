import copy
import json

import check
import defaults
import evidence
import structure
from test_apply_defaults import Recipes, chain_recipe, contract, measure, outcomes, song_recipe, step


def leaf(leaf_id, value, test, state, measure="band"):
    return {"id": leaf_id, "value": value, "test": test, "state": state, "measure": measure, "text": leaf_id}


def saturate(band, dip):
    """kick.saturate: (band rises > 0.5 or dip falls < -0.5) and attack holds."""
    state = lambda ok: "true" if ok else "false"
    return {"move": "kick.saturate", "source": "kick", "needed": "true", "worked": "true", "with": "trial",
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


def two_sources(kick_step, snare_steps, trial_step):
    return {"stems": {"Kick.wav": {}, "Snare.wav": {}}, "buses": {},
            "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]}},
            "steps": [kick_step, *snare_steps],
            "moves": [{"move": "kick.limit", "source": "kick", "decision": "kept", "steps": [kick_step["id"]], "note": "n"},
                      {"move": "snare.compress", "source": "snare", "decision": "dropped", "note": "n",
                       "trial": {"steps": [trial_step]}}]}


def test_a_kept_trial_lands_where_measured_when_another_source_drops_its_anchor_the_same_round():
    """The trial goes before the snare EQ, which trial_patch writes as after the kick's step: dropped first, the trial
    would land after the EQ."""
    for snare_steps, trial in (([step("s-eq", "Snare.wav", "PeakFilter")], dict(step("s-comp", "Snare.wav", "Compressor"), before="s-eq")),
                               ([step("s-clip", "Snare.wav", "Clipping")], dict(step("s-comp", "Snare.wav", "PeakFilter"), before="s-clip"))):
        recipe = two_sources(step("kick-lim", "Kick.wav", "Limiter"), snare_steps, trial)
        variant = structure.apply_patch(recipe, recipe["moves"][1]["trial"])
        numbers = {"contracts": [contract("kick.limit", "kick", "kept", "dropped", needed="false"),
                                 contract("snare.compress", "snare", "dropped", "kept", with_key="v")]}
        new, changes, left, deferred = defaults.decide(recipe, numbers, Recipes({"v": variant}))
        assert sorted(r["move"] for r, _ in changes) == ["kick.limit", "snare.compress"] and left == deferred == []
        assert [s["id"] for s in new["steps"]] == ["s-comp", snare_steps[0]["id"]]


def test_a_trial_that_would_not_sit_where_measured_waits():
    recipe = two_sources(step("kick-lim", "Kick.wav", "Limiter"), [step("s-eq", "Snare.wav", "PeakFilter")],
                         dict(step("s-comp", "Snare.wav", "Compressor"), before="s-eq"))
    variant = structure.apply_patch(recipe, recipe["moves"][1]["trial"])
    assert defaults.placed(variant, variant)
    moved = copy.deepcopy(variant)
    moved["steps"] = [moved["steps"][0], moved["steps"][2], moved["steps"][1]]
    assert not defaults.placed(moved, variant)
    # Steps a drop removed, or another keep added, don't count against it.
    assert defaults.placed({"steps": [variant["steps"][1], variant["steps"][2], step("x", "Snare.wav", "Gain")]}, variant)


def test_a_steady_default_that_only_waits_is_not_sent_for_an_override(tmp_path, monkeypatch, capsys):
    """tom1's compressor cycles while mix-bus.console's default stays kept and waits on the lower level every round."""
    recipe = {"stems": {"Tom1.wav": {}}, "buses": {}, "sources": {"tom1": {"chain": "toms", "stems": ["Tom1.wav"]}},
              "steps": [step("t-comp", "Tom1.wav", "Compressor")],
              "moves": [{"move": "toms.compress", "source": "tom1", "decision": "kept", "steps": ["t-comp"], "note": "n"},
                        {"move": "mix-bus.console", "source": "mix-bus", "decision": "dropped", "note": "n"}]}
    original = copy.deepcopy(recipe)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe, indent=2) + "\n")
    monkeypatch.setattr(check, "structural", lambda r, s, m: ({}, [], []))
    monkeypatch.setattr(check, "renders", lambda r, s, m: [])
    monkeypatch.setattr(check, "release", lambda variants: None)

    def measured(recipe, song, nodes, master):
        kept = recipe["moves"][0]["decision"] == "kept"
        tom = {"move": "toms.compress", "source": "tom1", "decision": recipe["moves"][0]["decision"],
               "outcome": "dropped" if kept else "kept", "needed": "false" if kept else "true", "worked": None if kept else "true",
               "with": "trial", "leaves": [leaf("needed", 2.9 if kept else 3.2, "> 3", "false" if kept else "true", "spread")],
               "fails": ["the default outcome is dropped; drop it, or give an override with a measured reason" if kept
                         else "the trial worked; keep it, or give an override with a measured reason"]}
        console = {"move": "mix-bus.console", "source": "mix-bus", "decision": "dropped", "outcome": "kept", "needed": "true",
                   "worked": "true", "with": "trial",
                   "leaves": [{"id": "needed", "state": "true", "value": None, "text": "always"},
                              leaf("worked.0.1", -0.52, "< -0.5", "true", "dip")],
                   "fails": ["the trial worked; keep it, or give an override with a measured reason"]}
        return tmp_path / "r.md", {"contracts": [tom, console]}, Fake({"trial": original})

    monkeypatch.setattr(check, "measured", measured)
    check.apply_defaults(path, recipe, tmp_path, False)
    out = capsys.readouterr().out
    assert "deferred: mix-bus.console on mix-bus: waits for a lower level's changes" in out
    assert "flips: toms.compress on tom1" in out and "flips: mix-bus.console" not in out


def test_only_the_sources_on_a_duck_cycle_fall_back_to_readmes_order():
    """a and b key each other's Ducks and b keys c's; c is listed before b, but it must still come after its key."""
    recipe = {"stems": {"A.wav": {}, "C.wav": {}, "B.wav": {}}, "buses": {},
              "sources": {name: {"chain": "guitars", "stems": [f"{name.upper()}.wav"]} for name in ("a", "c", "b")},
              "steps": [step("a-to-b", "B.wav", "Duck", key="A.wav"), step("b-to-a", "A.wav", "Duck", key="B.wav"),
                        step("b-to-c", "C.wav", "Duck", key="B.wav")], "moves": []}
    rank = defaults.Order(recipe).source_rank
    assert sorted(rank, key=rank.get) == ["a", "b", "c"]


def test_a_needed_leaf_near_its_bar_that_alone_flips_a_kept_record_is_fragile():
    c = {"move": "toms.limit", "source": "tom1", "needed": "true", "worked": "true", "with": "current",
         "leaves": [leaf("needed", 3.05, "> 3", "true", "level_top"), leaf("worked.0", -4.0, "< -0.5", "true", "level_top"),
                    leaf("worked.1", 0.0, ">= -0.5", "true", "attack_p50")]}
    assert [(found["id"], gap) for found, gap in defaults.fragile(c)] == [("needed", 0.05)]


def test_a_field_change_touches_the_stem_it_names_even_with_dots_in_its_name():
    before = chain_recipe()
    before["moves"][3]["fields"] = ["stems.Snare.wav.offset_samples"]
    before["moves"][3]["steps"] = []
    after = copy.deepcopy(before)
    after["moves"][3].update(decision="dropped", fields=[])
    assert {"Snare.wav", "OH.wav", "mix"} <= defaults.touched(before, after, [before["moves"][3]])


def tom_bus(tom2_target):
    """Two toms routed to one tom bus: tom1's compressor trial goes on the bus, tom2 drops an EQ on `tom2_target`."""
    recipe = {"stems": {"Tom1.wav": {"bus": "toms"}, "Tom2.wav": {"bus": "toms"}}, "buses": {"toms": {}},
              "sources": {"tom1": {"chain": "toms", "stems": ["Tom1.wav"]}, "tom2": {"chain": "toms", "stems": ["Tom2.wav"]}},
              "steps": [step("t2-eq", tom2_target, "PeakFilter")],
              "moves": [{"move": "toms.compress", "source": "tom1", "decision": "dropped", "note": "n",
                         "trial": {"steps": [step("toms-comp", "toms", "Compressor")]}},
                        {"move": "toms.eq", "source": "tom2", "decision": "kept", "steps": ["t2-eq"], "note": "n"}]}
    variant = structure.apply_patch(recipe, recipe["moves"][0]["trial"])
    numbers = {"contracts": [contract("toms.compress", "tom1", "dropped", "kept", with_key="v"),
                             contract("toms.eq", "tom2", "kept", "dropped", needed="false")]}
    return defaults.decide(recipe, numbers, Recipes({"v": variant}))


def test_a_shared_bus_takes_one_change_per_round():
    _, changes, _, deferred = tom_bus("toms")
    assert [r["source"] for r, _ in changes] == ["tom1"]
    assert [(r["source"], why) for r, why in deferred] == [("tom2", "waits for a change before it in signal order")]
    # On its own stem, tom2's change shares no node with tom1's and goes in the same round.
    _, changes, _, deferred = tom_bus("Tom2.wav")
    assert sorted(r["source"] for r, _ in changes) == ["tom1", "tom2"] and deferred == []


def test_a_kept_send_and_a_drop_on_its_return_never_go_in_one_round():
    """snare's trial sends into the shared return `verb`, whose reverb tom1's record claims and drops: written in one
    round, the snare's kept send would feed an empty return it was never measured through."""
    recipe = {"stems": {"Snare.wav": {}, "Tom1.wav": {"sends": [{"bus": "verb", "gain_db": -12}]}},
              "buses": {"verb": {}},
              "sources": {"snare": {"chain": "snare", "stems": ["Snare.wav"]}, "tom1": {"chain": "toms", "stems": ["Tom1.wav"]}},
              "steps": [step("rv", "verb", "Reverb", dry_level=0, wet_level=0.3)],
              "moves": [{"move": "snare.reverb", "source": "snare", "decision": "dropped", "note": "n",
                         "trial": {"sends": {"Snare.wav": [{"bus": "verb", "gain_db": -12}]}}},
                        {"move": "toms.reverb", "source": "tom1", "decision": "kept", "steps": ["rv"],
                         "fields": ["stems.Tom1.wav.sends.verb"], "note": "n"}]}
    variant = structure.apply_patch(recipe, recipe["moves"][0]["trial"])
    numbers = {"contracts": [contract("snare.reverb", "snare", "dropped", "kept", with_key="v"),
                             contract("toms.reverb", "tom1", "kept", "dropped", needed="false")]}
    new, changes, _, deferred = defaults.decide(recipe, numbers, Recipes({"v": variant}))
    assert [r["move"] for r, _ in changes] == ["snare.reverb"] and [r["move"] for r, _ in deferred] == ["toms.reverb"]
    assert [s["id"] for s in new["steps"]] == ["rv"] and new["stems"]["Snare.wav"]["sends"] == [{"bus": "verb", "gain_db": -12}]


def test_a_shared_drop_group_waits_for_the_key_of_a_members_duck():
    """a and c share one EQ on their rhythm bus; b keys a Duck on c. The group sorts at a, but it must not change c
    before b."""
    recipe = {"stems": {"A.wav": {"bus": "rh"}, "B.wav": {}, "C.wav": {"bus": "rh"}}, "buses": {"rh": {}},
              "sources": {name: {"chain": "guitars", "stems": [f"{name.upper()}.wav"]} for name in ("a", "b", "c")},
              "steps": [step("shared-eq", "rh", "PeakFilter"), step("b-eq", "B.wav", "PeakFilter"),
                        step("c-duck", "C.wav", "Duck", key="B.wav")],
              "moves": [{"move": "guitars.mud-cut", "source": name, "decision": "kept", "steps": [ids], "note": "n"}
                        for name, ids in (("a", "shared-eq"), ("b", "b-eq"), ("c", "shared-eq"))]}
    numbers = {"contracts": [contract("guitars.mud-cut", name, "kept", "dropped", needed="false") for name in ("a", "b", "c")]}
    _, changes, _, deferred = defaults.decide(recipe, numbers, Recipes({}))
    assert [r["source"] for r, _ in changes] == ["b"] and sorted(r["source"] for r, _ in deferred) == ["a", "c"]
    # With b settled, the group goes.
    numbers["contracts"][1]["outcome"] = "kept"
    _, changes, _, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert sorted(r["source"] for r, _ in changes) == ["a", "c"]
