import copy
import json
import shutil

import numpy as np
import pytest
import soundfile

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
    recipe = song_recipe(song)
    _, numbers, variants = measure(recipe, song)
    new, changes, left = defaults.decide(recipe, numbers, variants)
    return recipe, numbers, new, changes, left


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
    changed = {i for i, r in enumerate(recipe["moves"]) if any(r is c for c, _ in changes)}
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
    again, changes, _ = defaults.decide(new, numbers, variants)
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
    assert out.count("applied, round 1") == 3 and "round 2" not in out
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
    new, changes, left = defaults.decide(recipe, numbers, Recipes({}))
    assert changes == [] and new == recipe
    assert "shares ['toms-comp'] with ['toms.compress on tom2']" in left[0][1]


def test_a_step_every_claimant_drops_goes():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", source, "kept", "dropped", needed="false") for source in ("tom1", "tom2")]}
    new, changes, _ = defaults.decide(recipe, numbers, Recipes({}))
    assert new["steps"] == [] and [r["decision"] for r in new["moves"]] == ["dropped", "dropped"] and len(changes) == 2


def test_two_trials_with_one_step_id_keep_only_the_first():
    recipe = bare()
    recipe["moves"] = [{"move": "toms.clean", "source": source, "decision": "dropped", "note": "n"} for source in ("tom1", "tom2")]
    trials = {source: dict(recipe, steps=recipe["steps"] + [step("gate", f"Tom{n}.wav", "NoiseGate", threshold_db=-40)])
              for n, source in ((1, "tom1"), (2, "tom2"))}
    numbers = {"contracts": [contract("toms.clean", source, "dropped", "kept", with_key=source) for source in ("tom1", "tom2")]}
    new, changes, left = defaults.decide(recipe, numbers, Recipes(trials))
    assert [s["id"] for s in new["steps"]] == ["toms-comp", "gate"] and new["steps"][1]["target"] == "Tom1.wav"
    assert len(changes) == 1 and "reuses names" in left[0][1]


def test_a_record_whose_default_flips_after_it_was_applied_is_left_and_listed():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", needed="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left = defaults.decide(recipe, numbers, Recipes({}), frozen={("toms.compress", "tom1")})
    assert new == recipe and changes == [] and "flipped" in left[0][1]


def test_an_override_is_never_touched():
    recipe = bare()
    recipe["moves"][0]["override"] = {"reason": "r", "leaves": ["needed"]}
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", needed="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left = defaults.decide(recipe, numbers, Recipes({}))
    assert new == recipe and changes == [] and all(r["source"] != "tom1" for r, _ in left)


def test_a_worked_if_failure_on_a_move_without_a_starting_trial_is_left():
    recipe = bare()
    numbers = {"contracts": [contract("toms.compress", "tom1", "kept", "dropped", worked="false"),
                             contract("toms.compress", "tom2", "kept", "kept")]}
    new, changes, left = defaults.decide(recipe, numbers, Recipes({}))
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
    new, changes, _ = defaults.decide(recipe, numbers, cached)
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
