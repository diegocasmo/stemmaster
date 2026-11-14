import copy
import gc
import json
import shutil
import weakref

import numpy as np
import pytest
import soundfile

import chains
import check
import defaults
import evidence
import render
import structure

RATE = 44100


def noise(seconds, seed):
    return np.random.default_rng(seed).standard_normal(round(seconds * RATE)) * 0.1


def hits(seconds, every, seed, loud_every):
    """Low thumps every `every` seconds, each `loud_every`-th 12 dB over the rest."""
    audio = noise(seconds, seed) * 0.005
    for n, start in enumerate(range(0, len(audio), round(every * RATE))):
        k = np.arange(min(4000, len(audio) - start))
        audio[start : start + len(k)] += (1.0 if n % loud_every == 0 else 0.25) * np.exp(-k / 600) * np.sin(2 * np.pi * 60 * k / RATE + 1)
    return audio


def step(step_id, target, plugin, **settings):
    return {"id": step_id, "target": target, "plugin": plugin, "settings": settings}


def song_recipe(song, seconds=4):
    for name, audio in {"Kick.wav": hits(seconds, 0.25, 1, 5), "Snare.wav": hits(seconds, 0.5, 2, 7), "Bass.wav": noise(seconds, 3)}.items():
        soundfile.write(song / "stems" / name, audio, RATE, subtype="FLOAT")
    return {
        "profile": "instrumental-prog-metal",
        "stems": {"Kick.wav": {"bus": "drums"}, "Snare.wav": {"bus": "drums", "sends": [{"bus": "snare-verb", "gain_db": -10}]},
                  "Bass.wav": {}},
        "buses": {"drums": {}, "snare-verb": {"bus": "drums"}},
        "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]},
                    "bass": {"chain": "bass", "stems": ["Bass.wav"]}},
        "steps": [
            step("bass-duck", "Bass.wav", "Duck", key="Kick.wav", threshold_db=-30, ratio=4, range_db=6, attack_ms=1,
                 release_ms=80, band_hz=[None, 200]),
            step("snare-verb", "snare-verb", "Reverb", dry_level=0, room_size=0.3),
            step("snare-hpf", "Snare.wav", "HighpassFilter", cutoff_frequency_hz=80),
            dict(step("master-drive", "mix", "Gain", gain_db=0), stage="master"),
            dict(step("master-limit", "mix", "Limiter", threshold_db=-3, release_ms=100), stage="master"),
        ],
        "moves": [
            {"move": "kick.duck-bass", "source": "kick", "decision": "kept", "steps": ["bass-duck"], "note": "ducks the bass"},
            {"move": "kick.limit", "source": "kick", "decision": "dropped", "note": "tried",
             "trial": {"steps": [step("kick-lim", "Kick.wav", "Limiter", threshold_db=-6, release_ms=20)]}},
            {"move": "snare.reverb", "source": "snare", "decision": "kept", "steps": ["snare-verb"],
             "fields": ["stems.Snare.wav.sends.snare-verb"], "note": "room",
             "override": {"reason": "the tail is the point", "leaves": ["worked.1", "worked.2"]}},
            {"move": "snare.hpf-lpf", "source": "snare", "decision": "kept", "steps": ["snare-hpf"], "note": "rumble"},
            {"move": "mix-bus.trial-master", "source": "mix-bus", "decision": "kept", "steps": ["master-drive", "master-limit"],
             "note": "trial limiter"},
            {"move": "kick.compress", "source": "kick", "decision": "dropped", "note": "not tried"},
        ],
    }


def measure(recipe, song):
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    return evidence.measured(copy.deepcopy(recipe), song, nodes, False)


def outcomes(numbers):
    return {(c["move"], c["source"]): c for c in numbers["contracts"]}


def applied(song):
    """The song's recipe with its defaults applied round by round until none changes, every change, and the last left."""
    recipe = song_recipe(song)
    _, numbers, variants = measure(recipe, song)
    new, changes, left = recipe, [], []
    for _ in range(4):
        if changes:
            _, round_numbers, round_variants = measure(new, song)
        else:
            round_numbers, round_variants = numbers, variants
        new, more, left, _ = defaults.decide(new, round_numbers, round_variants)
        if not more:
            break
        changes += more
    return recipe, numbers, new, changes, left


def test_a_round_applies_one_change_per_source_and_defers_the_rest(song):
    recipe = song_recipe(song)
    _, numbers, variants = measure(recipe, song)
    _, changes, _, deferred = defaults.decide(recipe, numbers, variants)
    # kick.limit comes before kick.duck-bass in kick.md, and snare has a node of its own.
    assert sorted(r["move"] for r, _ in changes) == ["kick.limit", "snare.hpf-lpf"]
    assert [(r["move"], why) for r, why in deferred] == [("kick.duck-bass", "waits for a change before it in signal order")]


APPLIED = {("kick.duck-bass", "kick"): "dropped", ("snare.hpf-lpf", "snare"): "dropped", ("kick.limit", "kick"): "kept"}


def test_applied_defaults_pass_the_records_they_decide(song):
    recipe, numbers, new, changes, left = applied(song)
    before = outcomes(numbers)
    assert all(before[key]["fails"] for key in APPLIED)
    assert {(r["move"], r["source"]): text.split(":")[0] for r, text in changes} == APPLIED
    assert [(r["move"], why) for r, why in left] == [("kick.compress", "default unresolved")]
    after = outcomes(measure(new, song)[1])
    for key, decision in APPLIED.items():
        assert (after[key]["decision"], after[key]["outcome"], after[key]["fails"]) == (decision, decision, [])
    # Records it left keep their result: the override still passes, the unresolved one still fails.
    for key in set(before) - set(APPLIED):
        assert after[key]["fails"] == before[key]["fails"]
    # The partial recipe misses most moves, so only errors the apply adds would count.
    assert set(structure_errors(new, song)) <= set(structure_errors(recipe, song))


def structure_errors(recipe, song):
    errors = []
    structure.coverage(recipe, structure.Routing(recipe), False, errors, [], song)
    return errors


def test_only_the_decided_records_and_their_steps_change(song):
    recipe, _, new, changes, _ = applied(song)
    text = json.dumps(recipe, indent=2) + "\n"
    write = defaults.formatted(recipe, text)
    keys = {(c["move"], c["source"]) for c, _ in changes}
    changed = {i for i, r in enumerate(recipe["moves"]) if (r["move"], r["source"]) in keys}
    assert new["moves"][2] == recipe["moves"][2]
    assert [s["id"] for s in new["steps"]] == ["snare-verb", "master-drive", "master-limit", "kick-lim"]
    assert new["moves"][1] == {"move": "kick.limit", "source": "kick", "decision": "kept", "note": "tried", "steps": ["kick-lim"]}
    assert new["moves"][0] == {"move": "kick.duck-bass", "source": "kick", "decision": "dropped", "steps": [], "note": "ducks the bass"}
    reverted = copy.deepcopy(new)
    reverted["steps"] = recipe["steps"]
    for i in changed:
        reverted["moves"][i] = recipe["moves"][i]
    assert write(reverted) == text


def test_applying_twice_changes_nothing(song):
    _, _, new, _, _ = applied(song)
    _, numbers, variants = measure(new, song)
    again, changes, _, _ = defaults.decide(new, numbers, variants)
    assert changes == [] and json.dumps(again) == json.dumps(new)


def test_a_hand_written_recipe_with_those_decisions_gives_the_same_report_bytes(tmp_path, song):
    _, _, new, _, _ = applied(song)
    measure(new, song)
    by_hand = tmp_path / "by-hand"
    shutil.copytree(song / "stems", by_hand / "stems")
    recipe = song_recipe(by_hand)
    recipe["steps"] = [s for s in recipe["steps"] if s["id"] != "bass-duck"] + [
        step("kick-lim", "Kick.wav", "Limiter", threshold_db=-6, release_ms=20)]
    recipe["moves"][0] = {"move": "kick.duck-bass", "source": "kick", "decision": "dropped", "note": "ducks the bass"}
    recipe["moves"][1] = {"move": "kick.limit", "source": "kick", "decision": "kept", "steps": ["kick-lim"], "note": "tried"}
    recipe["moves"][3] = {"move": "snare.hpf-lpf", "source": "snare", "decision": "dropped", "note": "rumble"}
    recipe["steps"] = [s for s in recipe["steps"] if s["id"] != "snare-hpf"]
    measure(recipe, by_hand)
    for name in ("report.json", "report.md"):
        assert (by_hand / "renders" / "check" / name).read_bytes() == (song / "renders" / "check" / name).read_bytes()


def test_the_loop_writes_once_then_finds_a_fixed_point(song, monkeypatch, capsys):
    recipe = song_recipe(song)
    path = song / "recipe.json"
    path.write_text(json.dumps(recipe, indent=2) + "\n")
    # The partial recipe can't pass the full structural check; the apply itself is what's under test.
    monkeypatch.setattr(check, "structural", lambda r, s, m: (structure.nodes(r, structure.Routing(r), []), [], []))
    _, _, _, numbers = check.apply_defaults(path, recipe, song, False)
    out = capsys.readouterr().out
    assert out.count("applied, round 1") == 2 and out.count("applied, round 2") == 1 and "round 3" not in out
    assert "left: kick.compress on kick: default unresolved" in out
    written = path.read_text()
    assert {(c["move"], c["decision"]) for c in numbers["contracts"] if c["fails"]} == {("kick.compress", "dropped")}
    check.apply_defaults(path, json.loads(written), song, False)
    assert "applied" not in capsys.readouterr().out and path.read_text() == written


def test_a_recipe_not_in_the_repos_form_is_refused(song):
    recipe = song_recipe(song)
    assert defaults.formatted(recipe, json.dumps(recipe)) is None
    assert defaults.formatted(recipe, json.dumps(recipe, indent=2) + "\n") is not None


class Recipes:
    def __init__(self, recipes):
        self.recipes = recipes


def contract(move, source, decision, outcome, needed="true", worked="true", with_key=None):
    return {"move": move, "source": source, "decision": decision, "outcome": outcome, "needed": needed, "worked": worked,
            "fails": ["contradicts"], "leaves": [], "with": with_key}


def bare():
    return {"stems": {"Tom1.wav": {"bus": "toms"}, "Tom2.wav": {"bus": "toms"}}, "buses": {"toms": {}},
            "steps": [step("toms-comp", "toms", "Compressor", threshold_db=-20)],
            "moves": [{"move": "toms.compress", "source": "tom1", "decision": "kept", "steps": ["toms-comp"], "note": "a"},
                      {"move": "toms.compress", "source": "tom2", "decision": "kept", "steps": ["toms-comp"], "note": "b"}]}


def test_a_step_another_kept_record_claims_is_left_in_place():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", needed="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert changes == [] and new == recipe
    assert "shares ['toms-comp'] with ['toms.compress on tom2']" in left[0][1]


def test_a_step_every_claimant_drops_goes():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", source, "kept", "dropped", needed="false") for source in ("tom1", "tom2")]}
    new, changes, _, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert new["steps"] == [] and [r["decision"] for r in new["moves"]] == ["dropped", "dropped"] and len(changes) == 2


def test_two_trials_with_one_step_id_keep_only_the_first():
    recipe = bare()
    recipe["moves"] = [{"move": "toms.clean", "source": source, "decision": "dropped", "note": "n"} for source in ("tom1", "tom2")]
    trials = {source: dict(recipe, steps=recipe["steps"] + [step("gate", f"Tom{n}.wav", "NoiseGate", threshold_db=-40)])
              for n, source in ((1, "tom1"), (2, "tom2"))}
    numbers = {"contracts": [contract("toms.clean", source, "dropped", "kept", with_key=source) for source in ("tom1", "tom2")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes(trials))
    assert [s["id"] for s in new["steps"]] == ["toms-comp", "gate"] and new["steps"][1]["target"] == "Tom1.wav"
    assert len(changes) == 1 and "reuses names" in left[0][1]


def chain_recipe():
    """One tom with three kept moves on its own stem, a snare keying a Duck on the overheads, and a mix-bus move."""
    steps = [step("t-comp", "Tom1.wav", "Compressor"), step("t-sat", "Tom1.wav", "Distortion"),
             step("t-lim", "Tom1.wav", "Limiter"), step("s-eq", "Snare.wav", "PeakFilter"),
             step("oh-duck", "OH.wav", "Duck", key="Snare.wav"), step("oh-eq", "OH.wav", "PeakFilter"),
             step("mix-comp", "mix", "Compressor")]
    moves = [("toms.limit", "tom1", "t-lim"), ("toms.compress", "tom1", "t-comp"), ("toms.saturate-clip", "tom1", "t-sat"),
             ("snare.ring-cut", "snare", "s-eq"), ("overheads.snare-control", "cymbals", "oh-duck"),
             ("overheads.hpf", "cymbals", "oh-eq"), ("mix-bus.compress", "mix-bus", "mix-comp")]
    return {"stems": {"Tom1.wav": {}, "Snare.wav": {}, "OH.wav": {}}, "buses": {},
            "sources": {"cymbals": {"chain": "overheads", "stems": ["OH.wav"]}, "tom1": {"chain": "toms", "stems": ["Tom1.wav"]},
                        "snare": {"chain": "snare", "stems": ["Snare.wav"]}},
            "steps": steps,
            "moves": [{"move": move, "source": source, "decision": "kept", "steps": [step_id], "note": "n"}
                      for move, source, step_id in moves]}


def dropping(recipe, *keys):
    return {"contracts": [contract(r["move"], r["source"], "kept", "dropped" if (r["move"], r["source"]) in keys else "kept",
                                   needed="false") for r in recipe["moves"]]}


def test_within_a_source_the_first_move_in_chain_order_changes_first():
    recipe = chain_recipe()
    numbers = dropping(recipe, ("toms.limit", "tom1"), ("toms.saturate-clip", "tom1"))
    new, changes, _, deferred = defaults.decide(recipe, numbers, Recipes({}))
    assert [r["move"] for r, _ in changes] == ["toms.saturate-clip"] and [r["move"] for r, _ in deferred] == ["toms.limit"]
    assert [s["id"] for s in new["steps"]] == [s["id"] for s in recipe["steps"] if s["id"] != "t-sat"]


def test_a_source_waits_while_the_source_keying_its_duck_changes():
    recipe = chain_recipe()
    numbers = dropping(recipe, ("snare.ring-cut", "snare"), ("overheads.hpf", "cymbals"))
    _, changes, _, deferred = defaults.decide(recipe, numbers, Recipes({}))
    assert [r["move"] for r, _ in changes] == ["snare.ring-cut"] and [r["move"] for r, _ in deferred] == ["overheads.hpf"]
    # The overheads change on their own once the snare is settled.
    _, changes, _, _ = defaults.decide(recipe, dropping(recipe, ("overheads.hpf", "cymbals")), Recipes({}))
    assert [r["move"] for r, _ in changes] == ["overheads.hpf"]


def test_a_higher_level_waits_for_a_lower_ones_changes():
    recipe = chain_recipe()
    numbers = dropping(recipe, ("mix-bus.compress", "mix-bus"), ("toms.limit", "tom1"))
    _, changes, _, deferred = defaults.decide(recipe, numbers, Recipes({}))
    assert [r["move"] for r, _ in changes] == ["toms.limit"]
    assert [(r["move"], why) for r, why in deferred] == [("mix-bus.compress", "waits for a lower level's changes")]


def test_a_change_that_cant_go_lets_the_next_in_order_go():
    recipe = chain_recipe()
    numbers = dropping(recipe, ("toms.compress", "tom1"), ("toms.limit", "tom1"))
    broken = lambda r: ["broken"] if all(s["id"] != "t-comp" for s in r["steps"]) else []
    _, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}), validate=broken)
    assert [r["move"] for r, _ in changes] == ["toms.limit"] and "broken" in left[0][1]


def test_an_override_is_never_touched():
    recipe = bare()
    recipe["moves"][0]["override"] = {"reason": "r", "leaves": ["needed"]}
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", needed="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert new == recipe and changes == []
    # The check fails it, so it is listed, as written, with what it cites.
    assert [why for r, why in left if r["source"] == "tom1"] == ["its override is as written, citing ['needed']; contradicts; values: "]


def test_a_stale_override_lists_what_it_cites_and_what_the_check_asks_for():
    record = {"move": "snare.blend", "source": "snare", "override": {"reason": "r", "leaves": ["worked.1"]}}
    c = {"fails": ["override must name the leaves it contradicts: ['worked.0.0', 'worked.1']"],
         "leaves": [{"id": "needed", "state": "true", "value": 3.0, "test": "> 1"},
                    {"id": "worked.0.0", "state": "true", "value": 0.0, "test": "<= 0.3"},
                    {"id": "worked.1", "state": "true", "value": 0.547, "test": "> 0.5"}]}
    assert defaults.override_report(record, c) == (
        "its override is as written, citing ['worked.1']; override must name the leaves it contradicts: ['worked.0.0', "
        "'worked.1']; values: worked.0.0 0.0 (<= 0.3), worked.1 0.547 (> 0.5)")


def test_a_worked_if_failure_on_a_move_without_a_starting_trial_is_left():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", worked="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert new == recipe and "Worked if fails" in left[0][1]


@pytest.mark.skipif(not all((render.PLUGINS / f"{name}.vst3").exists() for name in ("T-Saturator", "ZL Equalizer 2", "Airwindows Consolidated")),
                    reason="needs the local plugin bundles")
@pytest.mark.parametrize("move, source", [("kick.saturate", "kick"), ("bass.chorus", "bass")])
def test_a_kept_starting_trial_is_written_as_measured_even_from_the_cache(song, move, source):
    recipe = song_recipe(song)
    recipe["moves"].append({"move": move, "source": source, "decision": "dropped", "note": "n"})
    key = f"trial:{move}:{source}"
    _, numbers, variants = measure(recipe, song)
    trial = variants.recipes[key]
    match = next((s for s in trial["steps"] if s["id"].endswith("-match")), None)
    assert match is None or match["settings"]["gain_db"] != 0
    _, numbers, cached = measure(recipe, song)
    assert key not in cached.views and json.dumps(cached.recipes[key]) == json.dumps(trial)
    # Forced: the point is what a kept starting trial writes, whatever this noise makes of it.
    for c in numbers["contracts"]:
        c["outcome"], c["fails"] = ("kept", ["contradicts"]) if c["move"] == move else (c["decision"], [])
    new, changes, _, _ = defaults.decide(recipe, numbers, cached)
    record = next(r for r in new["moves"] if r["move"] == move)
    assert record["steps"] == [s["id"] for s in trial["steps"] if s["id"].startswith(f"trial-{move.replace('.', '-')}")]
    if move == "bass.chorus":
        bus = f"trial-bass-chorus-bass-bus"
        assert record["fields"] == [f"stems.Bass.wav.sends.{bus}"] and record["tool_reason"] == defaults.TOOL_REASON
    assert evidence.fingerprint(evidence.numeric(new)) == evidence.fingerprint(evidence.numeric(trial))
    # Its off render, which reads its Needed if, is the recipe the trial was measured against, except that a send trial's
    # bus stays, fed by nothing.
    expected = copy.deepcopy(recipe)
    if move == "bass.chorus":
        expected["stems"]["Bass.wav"]["sends"] = []
        expected["buses"][bus] = {}
    assert evidence.fingerprint(evidence.numeric(evidence.off_variant(new, record))) == evidence.fingerprint(evidence.numeric(expected))



def room_song(song, extra=False):
    """Kick, snare and a room mic, every move recorded: the room makes snare.reverb's Needed if false, so its default
    drops the send that alone feeds its reverb return."""
    for i, name in enumerate(("Kick.wav", "Snare.wav", "Room.wav")):
        soundfile.write(song / "stems" / name, np.random.default_rng(i).normal(0, 0.03, (RATE, 2)), RATE, subtype="FLOAT")
    recipe = {
        "stems": {"Kick.wav": {"bus": "drums"}, "Snare.wav": {"bus": "drums", "sends": [{"bus": "snare-verb", "gain_db": -10}]},
                  "Room.wav": {"bus": "drums"}},
        "buses": {"drums": {}, "snare-verb": {"bus": "drums"}},
        "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]},
                    "room": {"chain": "overheads", "stems": ["Room.wav"]}},
        "steps": [step("verb", "snare-verb", "Reverb", dry_level=0, room_size=0.3),
                  step("kick-hpf", "Kick.wav", "HighpassFilter", cutoff_frequency_hz=30),
                  dict(step("limit", "mix", "Limiter", threshold_db=-3), stage="master")],
        "moves": [],
    }
    if extra:
        recipe["steps"].insert(1, step("verb-eq", "snare-verb", "HighpassFilter", cutoff_frequency_hz=300))
    docs = chains.parse()[0]
    for source, chain in structure.instances(recipe):
        if chain != "master":
            recipe["moves"] += [{"move": move, "source": source, "decision": "dropped", "note": "n"}
                                for move in structure.moves_for(recipe, source, chain, docs)]
    claims = {"snare.reverb": (["verb"], ["stems.Snare.wav.sends.snare-verb"]), "mix-bus.trial-master": (["limit"], []),
              "kick.hpf": (["kick-hpf"], []), "snare.hpf-lpf": (["verb-eq"] if extra else None, [])}
    for record in recipe["moves"]:
        if record["move"] in claims and claims[record["move"]][0]:
            steps, fields = claims[record["move"]]
            record.update(decision="kept", steps=steps, fields=fields, tool_reason="built-in stand-in")
    return recipe


def room_decide(song, extra):
    recipe = room_song(song, extra)
    (song / "renders" / "check").mkdir(parents=True)
    assert check.renders(recipe, song, False) == []
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    variants, ctx, _ = evidence.prepare(recipe, song, nodes, evidence.plan(recipe, song, nodes), ["apply-defaults-test"])
    found = evidence.contract_report(recipe, song, nodes, variants, ctx, False, only={("snare.reverb", "snare")})
    assert (found[0]["needed"], found[0]["outcome"]) == ("false", "dropped")
    # kick.hpf stands in for any other record whose default can be applied.
    found.append(contract("kick.hpf", "kick", "kept", "dropped", needed="false"))
    return recipe, defaults.decide(recipe, {"contracts": found}, variants, validate=lambda r: check.renders(r, song, False))


def test_dropping_the_last_send_to_a_return_removes_the_unfed_return(song):
    recipe, (new, changes, left, _) = room_decide(song, extra=False)
    assert [r["move"] for r, _ in changes] == ["kick.hpf", "snare.reverb"] and left == []
    assert "snare-verb" not in new["buses"] and "sends" not in new["stems"]["Snare.wav"]
    assert check.renders(new, song, False) == []
    nodes = structure.nodes(new, structure.Routing(new), [])
    variants, ctx, _ = evidence.prepare(new, song, nodes, evidence.plan(new, song, nodes), ["apply-defaults-test"])
    again = evidence.contract_report(new, song, nodes, variants, ctx, False, only={("snare.reverb", "snare")})
    assert (again[0]["decision"], again[0]["outcome"], again[0]["fails"]) == ("dropped", "dropped", [])


def test_a_return_another_step_still_uses_is_left_and_the_rest_still_applies(song):
    recipe, (new, changes, left, _) = room_decide(song, extra=True)
    assert [r["move"] for r, _ in changes] == ["kick.hpf"]
    assert [r["move"] for r, _ in left] == ["snare.reverb"] and "buses nothing feeds" in left[0][1]
    assert new["buses"] == recipe["buses"] and check.renders(new, song, False) == []


def test_a_dropped_blend_whose_default_is_kept_is_left_for_the_mixer(song):
    recipe = song_recipe(song)
    soundfile.write(song / "stems" / "Kick2.wav", hits(4, 0.25, 1, 5) * 0.7, RATE, subtype="FLOAT")
    recipe["stems"]["Kick2.wav"] = {"bus": "drums"}
    recipe["sources"]["kick"]["stems"].append("Kick2.wav")
    recipe["moves"].append({"move": "kick.blend", "source": "kick", "decision": "dropped", "note": "n"})
    _, numbers, variants = measure(recipe, song)
    blend = outcomes(numbers)[("kick.blend", "kick")]
    assert (blend["outcome"], blend["with"]) == ("kept", "current")
    new, changes, left, _ = defaults.decide(recipe, numbers, variants)
    assert next(r for r in new["moves"] if r["move"] == "kick.blend") == recipe["moves"][-1]
    assert any(r["move"] == "kick.blend" and "options already in the recipe" in why for r, why in left)
    assert any(r["move"] == "kick.limit" for r, _ in changes)


def test_a_chain_of_shared_claims_never_leaves_a_kept_record_on_a_removed_step():
    recipe = {"stems": {"Tom1.wav": {}}, "buses": {},
              "steps": [step("s1", "Tom1.wav", "Compressor"), step("s2", "Tom1.wav", "Compressor")],
              "moves": [{"move": "toms.compress", "source": "a", "decision": "kept", "steps": ["s1"], "note": "a"},
                        {"move": "toms.compress", "source": "b", "decision": "kept", "steps": ["s1", "s2"], "note": "b"},
                        {"move": "toms.compress", "source": "c", "decision": "kept", "steps": ["s2"], "note": "c"}]}
    numbers = {"contracts": [contract("toms.compress", "a", "kept", "dropped", needed="false"),
                             contract("toms.compress", "b", "kept", "dropped", needed="false"),
                             contract("toms.compress", "c", "kept", "kept")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert new == recipe and changes == [] and sorted(r["source"] for r, why in left if "shares" in why) == ["a", "b"]


def test_a_round_lets_its_renders_go_before_the_next_starts(song, monkeypatch):
    recipe = song_recipe(song)
    path = song / "recipe.json"
    path.write_text(json.dumps(recipe, indent=2) + "\n")
    monkeypatch.setattr(check, "structural", lambda r, s, m: (structure.nodes(r, structure.Routing(r), []), [], []))
    monkeypatch.setattr(check, "renders", lambda r, s, m: [])
    held, rounds = [], []

    def measured(*args):
        gc.collect()
        rounds.append([ref() is None for ref in held])
        out = evidence.measured(*args)
        held[:] = [weakref.ref(view.result) for view in out[2].views.values()] + [weakref.ref(out[2].ctx)]
        return out

    monkeypatch.setattr(check, "measured", measured)
    check.apply_defaults(path, recipe, song, False)
    gc.collect()
    assert len(rounds) == 3 and all(r and all(r) for r in rounds[1:]) and all(ref() is None for ref in held)


def test_a_shared_drop_group_removes_all_its_claims_before_pruning_returns(song):
    """Both toms claim one reverb on S, fed through a chain of returns toms -> R -> S: dropping them together removes
    the chain, where dropping the first alone would prune R before the second's claim on it."""
    for i, stem in enumerate(("Tom1.wav", "Tom2.wav")):
        soundfile.write(song / "stems" / stem, np.random.default_rng(i).normal(0, 0.01, (RATE, 2)), RATE, subtype="FLOAT")
    recipe = {
        "stems": {"Tom1.wav": {"bus": "toms"}, "Tom2.wav": {"bus": "toms"}},
        "sources": {"tom1": {"chain": "toms", "stems": ["Tom1.wav"]}, "tom2": {"chain": "toms", "stems": ["Tom2.wav"]}},
        "buses": {"toms": {"bus": "drums", "sends": [{"bus": "R", "gain_db": -10}]}, "drums": {},
                  "R": {"bus": "drums", "sends": [{"bus": "S", "gain_db": -10}]}, "S": {"bus": "drums"}},
        "steps": [step("rv", "S", "Reverb", dry_level=0, wet_level=0.3)],
        "moves": [],
    }
    docs = chains.parse()[0]
    for source, chain in structure.instances(recipe):
        if chain != "master":
            recipe["moves"] += [{"move": move, "source": source, "decision": "dropped", "note": "n"}
                                for move in structure.moves_for(recipe, source, chain, docs)]
    for record in recipe["moves"]:
        if record["move"] == "toms.reverb":
            record.update(decision="kept", steps=["rv"], fields=["buses.toms.sends.R", "buses.R.sends.S"], tool_reason="built-in stand-in")
    assert check.renders(recipe, song, False) == []
    numbers = {"contracts": [contract("toms.reverb", source, "kept", "dropped", needed="false") for source in ("tom1", "tom2")]}
    new, changes, left, _ = defaults.decide(recipe, numbers, Recipes({}), validate=lambda r: check.renders(r, song, False))
    assert len(changes) == 2 and left == []
    assert new["buses"] == {"toms": {"bus": "drums"}, "drums": {}} and new["steps"] == []
    assert check.renders(new, song, False) == []


def test_a_field_names_only_its_own_stem_or_bus():
    recipe = {"stems": {"Snare.wav": {}, "Snare": {}}, "buses": {"Snare": {}, "verb": {}}}

    assert defaults.field_names(recipe, ["stems.Snare.wav.offset_samples"]) == {"Snare.wav"}
    assert defaults.field_names(recipe, ["buses.Snare.sends.verb", "stems.Snare.pan"]) == {"Snare"}
