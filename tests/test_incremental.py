import copy
import json
import os
import shutil

import pytest

import evidence
import incremental
import structure
from test_workers import band


def check(song, recipe, memo, monkeypatch):
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(memo))
    reused = []
    monkeypatch.setattr(evidence, "progress", lambda message: reused.append(message) if "reused" in message else None)
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    try:
        evidence.report(json.loads(json.dumps(recipe)), song, nodes)
    except BaseException as error:
        return f"{type(error).__name__}: {error}", reused
    return [(song / "renders" / "check" / name).read_bytes() for name in ("report.json", "report.md")], reused


def setting(step_id, key, value):
    def edit(recipe):
        next(step for step in recipe["steps"] if step["id"] == step_id)["settings"][key] = value
    return edit


def stem(name, key, value):
    def edit(recipe):
        recipe["stems"][name][key] = value
    return edit


def send_gain(recipe):
    recipe["stems"]["Snare.wav"]["sends"][0]["gain_db"] = -16


def trial_setting(recipe):
    record = next(r for r in recipe["moves"] if r["move"] == "snare.compress")
    record["trial"]["steps"][0]["settings"]["ratio"] = 3


def drop_guitar_filter(recipe):
    recipe["steps"] = [step for step in recipe["steps"] if step["id"] != "gtr-hpf"]
    recipe["moves"] = [r for r in recipe["moves"] if r["move"] != "guitars.filters"]


EDITS = {
    "compressor setting": setting("kick-comp", "ratio", 6),
    "duck setting": setting("bass-duck", "range_db", 9),
    "guitar filter": setting("gtr-hpf", "cutoff_frequency_hz", 120),
    "master limiter": setting("master-limit", "threshold_db", -4),
    "stem gain": stem("Bass.wav", "gain_db", -2),
    "pan": stem("GtrL.wav", "pan", -0.6),
    "polarity": stem("GtrR.wav", "invert", True),
    "send level": send_gain,
    "record trial": trial_setting,
    "step and record removed": drop_guitar_filter,
}


@pytest.mark.parametrize("edit", list(EDITS), ids=list(EDITS))
def test_a_recheck_after_an_edit_gives_the_cold_checks_bytes(tmp_path, monkeypatch, edit):
    base = tmp_path / "base"
    recipe = band(base, 1)
    check(base, recipe, tmp_path / "memo", monkeypatch)
    edited = copy.deepcopy(recipe)
    EDITS[edit](edited)
    fresh = tmp_path / "fresh"
    (fresh / "stems").mkdir(parents=True)
    for path in (base / "stems").iterdir():
        os.link(path, fresh / "stems" / path.name)

    again, _ = check(base, edited, tmp_path / "memo", monkeypatch)
    cold, _ = check(fresh, edited, tmp_path / "memo-fresh", monkeypatch)

    assert again == cold


def test_an_edit_reuses_the_contracts_that_never_read_what_it_changed(tmp_path, monkeypatch):
    recipe = band(tmp_path, 1)
    check(tmp_path, recipe, tmp_path / "memo", monkeypatch)
    EDITS["guitar filter"](recipe)

    _, reused = check(tmp_path, recipe, tmp_path / "memo", monkeypatch)

    # The kick, snare and bass contracts read nothing the guitar bus feeds but the mix.
    assert reused and int(reused[0].split()[0]) >= 3


def cold_copy(base, folder):
    (folder / "stems").mkdir(parents=True)
    for path in (base / "stems").iterdir():
        os.link(path, folder / "stems" / path.name)
    return folder


def truncated(text):
    return text[: len(text) // 2]


def bit_flipped(text):
    # The last digit is a number in the payload, so the flip leaves valid JSON with another value.
    at = max(i for i, c in enumerate(text) if c.isdigit())
    return text[:at] + chr(ord(text[at]) ^ 1) + text[at + 1:]


@pytest.mark.parametrize("damage", [truncated, bit_flipped])
def test_a_damaged_trace_or_contract_file_is_a_miss(tmp_path, monkeypatch, damage):
    base = tmp_path / "base"
    recipe = band(base, 1)
    check(base, recipe, tmp_path / "memo", monkeypatch)
    EDITS["guitar filter"](recipe)
    cache = base / "renders" / "check"
    for path in list(cache.glob("trace-*.json")) + list(cache.glob("contract-*.json")):
        path.write_text(damage(path.read_text()))

    again, reused = check(base, recipe, tmp_path / "memo", monkeypatch)
    cold, _ = check(cold_copy(base, tmp_path / "fresh"), recipe, tmp_path / "memo-fresh", monkeypatch)

    assert reused == [] and again == cold


def test_an_edit_whose_keys_cant_be_computed_fails_as_a_cold_check_does(tmp_path, monkeypatch):
    base = tmp_path / "base"
    recipe = band(base, 1)
    check(base, recipe, tmp_path / "memo", monkeypatch)
    setting("bass-duck", "key", "")(recipe)

    again, _ = check(base, recipe, tmp_path / "memo", monkeypatch)
    cold, _ = check(cold_copy(base, tmp_path / "fresh"), recipe, tmp_path / "memo-fresh", monkeypatch)

    assert isinstance(cold, str) and again == cold


def test_a_master_that_reads_a_file_is_measured_again_when_the_file_changes(tmp_path, monkeypatch):
    import numpy as np
    import soundfile

    base = tmp_path / "base"
    recipe = band(base, 1)
    impulse = tmp_path / "ir.wav"
    rng = np.random.default_rng(42)
    soundfile.write(impulse, rng.normal(0, 0.02, (1024, 1)), 44100, subtype="FLOAT")
    recipe["steps"].insert(next(i for i, s in enumerate(recipe["steps"]) if s.get("stage") == "master"),
                           {"id": "master-ir", "target": "mix", "plugin": "Convolution", "stage": "master",
                            "settings": {"impulse_response_filename": str(impulse), "mix": 1.0}})
    next(r for r in recipe["moves"] if r["move"] == "mix-bus.trial-master")["steps"].append("master-ir")
    check(base, recipe, tmp_path / "memo", monkeypatch)
    # Only a trace can reuse a contract once its exact cache files are gone (they don't key the file's contents either).
    for path in (base / "renders" / "check").glob("[0-9a-fc]*.json"):
        path.unlink()
    soundfile.write(impulse, rng.normal(0, 0.9, (1024, 1)), 44100, subtype="FLOAT")

    again, _ = check(base, recipe, tmp_path / "memo", monkeypatch)
    cold, _ = check(cold_copy(base, tmp_path / "fresh"), recipe, tmp_path / "memo-fresh", monkeypatch)

    assert isinstance(cold, list) and again == cold


def test_a_trace_whose_recorded_value_changed_does_not_hold(tmp_path, monkeypatch):
    recipe = band(tmp_path, 1)
    check(tmp_path, recipe, tmp_path / "memo", monkeypatch)
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    planned = evidence.plan(recipe, tmp_path, nodes)
    variants, ctx, _ = evidence.prepare(recipe, tmp_path, nodes, planned, evidence.inputs(tmp_path, recipe))
    records, entries = evidence.contract_entries(recipe, variants, ctx, False)
    traced = [(entry, incremental.unseal(path)) for entry in entries
              if (path := evidence.trace_path(variants, False, entry["record"])).exists()]
    entry, trace = next((entry, trace) for entry, trace in traced if trace["values"])
    static = evidence.trace_static(recipe, variants, ctx, False, records, entry)
    assert incremental.holds(trace, variants.signal_keys, entry["keys"], variants, static)

    trace["values"][0][-1] = "something else"

    assert not incremental.holds(trace, variants.signal_keys, entry["keys"], variants, static)


def test_reads_outside_a_contract_and_on_written_keys_are_not_recorded():
    recipe = incremental.track({"stems": {"A.wav": {"bus": "drums", "gain_db": -3}}, "steps": []}, "current")
    entry = recipe["stems"]["A.wav"]
    entry.get("gain_db")
    with incremental.recording() as trace:
        entry.get("bus")
        copied = copy.deepcopy(recipe)["stems"]["A.wav"]
        copied["gain_db"] = 0
        copied.get("gain_db")
        with incremental.quiet():
            entry.get("gain_db")
    assert trace.values == {} and trace.complex is None

    with incremental.recording() as trace:
        entry.get("gain_db")
        "pan" in entry
        entry["gain_db"] = 1
    assert list(trace.values.values()) == [-3, False]
    assert trace.complex


def test_apply_defaults_with_traces_writes_what_it_writes_without_them(tmp_path, monkeypatch, capsys):
    import check as check_cli
    from test_apply_defaults import song_recipe

    monkeypatch.setattr(check_cli, "structural", lambda r, s, m: (structure.nodes(r, structure.Routing(r), []), [], []))
    written = {}
    for name in ("traced", "untraced"):
        song = tmp_path / name
        (song / "stems").mkdir(parents=True)
        recipe = song_recipe(song)
        path = song / "recipe.json"
        path.write_text(json.dumps(recipe, indent=2) + "\n")
        monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(tmp_path / f"memo-{name}"))
        with monkeypatch.context() as patch:
            if name == "untraced":
                patch.setattr(evidence, "promote", lambda *args: None)
            check_cli.apply_defaults(path, recipe, song, False)
        written[name] = [path.read_text(), capsys.readouterr().out.replace(str(song), "song")] + \
            [(song / "renders" / "check" / n).read_bytes() for n in ("report.json", "report.md")]

    assert written["traced"] == written["untraced"]
