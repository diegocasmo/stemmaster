import copy
import json
import shutil

import pytest

import evidence
import structure
from helpers import noise, write

STEMS = ("BassAmp.wav", "BassSub.wav")


@pytest.fixture
def bass(song):
    write(song, STEMS[0], noise(1, seed=1))
    write(song, STEMS[1], noise(1, seed=2) * 0.5)
    return {
        "stems": {STEMS[0]: {"bus": "bass", "gain_db": -3}, STEMS[1]: {"bus": "bass"}},
        "buses": {"bass": {}},
        "sources": {"bass": {"chain": "bass", "stems": list(STEMS)}},
        "steps": [{"id": "hpf", "target": "bass", "plugin": "HighpassFilter", "settings": {"cutoff_frequency_hz": 40},
                   "reason": "rumble", "review": "kept"}],
        "moves": [
            {"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["hpf"], "fields": [], "note": "cuts rumble"},
            {"move": "bass.mud-cut", "source": "bass", "decision": "dropped", "steps": [], "fields": [], "note": "no mud"},
        ],
    }


@pytest.fixture
def check(song, monkeypatch):
    """Runs the report and counts its renders, so a full cache hit shows as zero."""
    renders = []
    real = evidence.render.Result

    def counted(*args, **kwargs):
        renders.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(evidence.render, "Result", counted)

    def run(recipe):
        renders.clear()
        nodes = structure.nodes(recipe, structure.Routing(recipe), [])
        numbers = evidence.report(recipe, song, nodes)[1]
        return numbers, len(renders)

    return run


def by_record(numbers):
    found = copy.deepcopy(numbers)
    for key in ("records", "contracts"):
        found[key] = sorted(found[key], key=lambda r: (r["move"], r["source"]))
    return found


def edit_notes(recipe):
    recipe["moves"][0]["note"] = "a new note"
    recipe["steps"][0]["reason"] = "a new reason"
    recipe["steps"][0]["review"] = "a new review"


def reorder_moves(recipe):
    recipe["moves"].reverse()


def reorder_stems(recipe):
    recipe["stems"] = dict(reversed(recipe["stems"].items()))


def change_setting(recipe):
    recipe["steps"][0]["settings"]["cutoff_frequency_hz"] = 60


def change_decision(recipe):
    recipe["moves"][1]["decision"] = "kept"
    recipe["moves"][1]["steps"] = ["hpf"]


@pytest.mark.parametrize("change", [edit_notes, reorder_moves], ids=["note", "move-order"])
def test_text_and_move_order_reuse_the_cache(bass, check, change):
    first, renders = check(bass)
    assert renders > 0
    changed = copy.deepcopy(bass)
    change(changed)

    second, renders = check(changed)

    assert renders == 0
    assert by_record(second) == by_record(first)


@pytest.mark.parametrize("change", [reorder_stems, change_setting, change_decision],
                         ids=["stem-order", "setting", "decision"])
def test_what_can_change_a_number_misses_the_cache(bass, check, change):
    check(bass)
    changed = copy.deepcopy(bass)
    change(changed)

    assert check(changed)[1] > 0


def test_a_tap_another_record_asks_for_misses_the_contract_cache(bass, check, monkeypatch):
    check(bass)
    planned = evidence.contract_taps
    monkeypatch.setattr(evidence, "contract_taps", lambda ctx, records: planned(ctx, records) | {STEMS[0]})

    assert check(bass)[1] > 0


def test_an_override_reason_comes_from_the_current_recipe(bass, check):
    bass["moves"][0]["override"] = {"reason": "old", "leaves": []}
    check(bass)
    bass["moves"][0]["override"]["reason"] = "new"

    numbers, renders = check(bass)

    assert renders == 0
    assert next(c for c in numbers["contracts"] if c["move"] == "bass.hpf")["override"]["reason"] == "new"


def test_an_override_without_a_reason_is_not_the_same_key_as_no_override():
    record = {"move": "bass.hpf", "override": {}}

    assert evidence.record_part(record) != evidence.record_part({"move": "bass.hpf"})
    assert evidence.record_part(record) != evidence.record_part({"move": "bass.hpf", "override": {"reason": ""}})


def test_the_evidence_fingerprint_binds_settings_order():
    settings = {"threshold_db": -20, "ratio": 4}

    assert evidence.fingerprint(settings) != evidence.fingerprint(dict(reversed(settings.items())))


def test_a_note_edit_gives_the_same_report_bytes(bass, song, check):
    for name, seed in (("GtrL.wav", 3), ("GtrR.wav", 4)):
        write(song, name, noise(1, seed=seed))
    bass["stems"] |= {"GtrL.wav": {"bus": "guitars", "pan": -1}, "GtrR.wav": {"bus": "guitars", "pan": 1}}
    bass["buses"]["guitars"] = {}
    bass["sources"]["guitars"] = {"chain": "guitars", "stems": ["GtrL.wav", "GtrR.wav"]}
    bass["moves"].append({"move": "guitars.align", "source": "guitars", "decision": "dropped", "steps": [], "fields": []})
    report = song / "renders" / "check"
    cold, _ = check(bass)
    files = [(report / name).read_bytes() for name in ("report.json", "report.md")]
    edit_notes(bass)

    warm, renders = check(bass)

    assert renders == 0
    assert list(cold["notes"]) == ["bass", "guitars"]
    assert [(report / name).read_bytes() for name in ("report.json", "report.md")] == files


def master_recipe(profile, first):
    """A master Compressor before a Limiter, with step ids that make the contract named `first` run first."""
    for index in range(100):
        recipe = {
            "profile": str(profile),
            "stems": {"Noise.wav": {}},
            "sources": {},
            "steps": [
                {"id": f"comp{index}", "target": "mix", "stage": "master", "plugin": "Compressor",
                 "settings": {"threshold_db": -20, "ratio": 4, "attack_ms": 1, "release_ms": 30}},
                {"id": f"limit{index}", "target": "mix", "stage": "master", "plugin": "Limiter",
                 "settings": {"threshold_db": -1, "release_ms": 100}},
            ],
            "moves": [
                {"move": "master.compress", "source": "master", "decision": "kept", "steps": [f"comp{index}"], "fields": []},
                {"move": "master.limit", "source": "master", "decision": "kept", "steps": [f"limit{index}"], "fields": []},
            ],
        }
        if min(recipe["moves"], key=lambda record: evidence.off_key(recipe, record))["move"] == first:
            return recipe
    raise AssertionError(f"no step ids put {first} first")


@pytest.mark.parametrize("first", ["master.limit", "master.compress"])
def test_master_probes_do_not_depend_on_contract_order_or_the_cache(song, first):
    write(song, "Noise.wav", noise(1, seed=11) * 0.1)
    profile = song / "target"
    profile.mkdir()
    reference = {field: -12 for field in evidence.measure.PROFILE_WHOLE_FIELDS + evidence.measure.PROFILE_LOUD_FIELDS}
    reference["bands_db"] = {str(band): -20 for band in evidence.outcome_tools.BAND_CENTERS_HZ}
    (profile / "profile.json").write_text(json.dumps({"sample_rate": 48000, "master_integrated_lufs": -12,
                                                      "references": [reference] * 3}))
    recipe = master_recipe(profile, first)
    cache = song / "renders" / "check"

    def compressor():
        numbers = evidence.report(copy.deepcopy(recipe), song, {}, master_mode=True)[1]
        found = next(c for c in numbers["contracts"] if c["move"] == "master.compress")
        return list(found["engaged"].values()), found["fails"], [(cache / n).read_bytes() for n in ("report.json", "report.md")]

    cold = compressor()
    recipe["moves"][0]["override"] = {"reason": "measured", "leaves": []}
    partial = compressor()
    shutil.rmtree(cache)
    cold_override = compressor()

    # At the matched drive the compressor works; at the quiet actual drive it reads as idle (about -150 dB).
    assert cold[0][0] > -60
    assert partial[0] == cold_override[0] == cold[0]
    assert partial == cold_override


def test_a_failed_note_read_is_unresolved_and_left_out_of_the_notes(song):
    # Too short for the note-onset STFT window, so note detection raises.
    write(song, "Bass.wav", noise(1024 / 48000, seed=1))
    recipe = {
        "stems": {"Bass.wav": {"bus": "bass"}},
        "buses": {"bass": {}},
        "sources": {"bass": {"chain": "bass", "stems": ["Bass.wav"]}},
        "steps": [],
        "moves": [{"move": "bass.hpf", "source": "bass", "decision": "dropped", "steps": [], "fields": []}],
    }
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])

    numbers = evidence.report(recipe, song, nodes)[1]

    assert numbers["notes"] == {}
    assert numbers["contracts"][0]["needed"] == "unresolved"
