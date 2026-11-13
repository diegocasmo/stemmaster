import copy
import json

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
    # notes lists only the sources a contract read this run, so a full cache hit has none (issue #20).
    found.pop("notes")
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
