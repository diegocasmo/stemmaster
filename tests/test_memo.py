import builtins
import copy
import importlib.util
import os
import platform
import py_compile
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile

import render
from helpers import RATE, noise, write


def step(step_id, target, plugin, **settings):
    return {"id": step_id, "target": target, "plugin": plugin, "settings": settings}


def song_recipe():
    """Four stems, a drums bus, a bass send to a reverb return, a bass duck keyed by the kick, a ride and a master."""
    return {
        "stems": {
            "Kick.wav": {"bus": "drums"},
            "Bass.wav": {"gain_db": -3, "sends": [{"bus": "verb", "gain_db": -12}]},
            "Gtr.wav": {"pan": -0.3, "offset_samples": 100},
            "Snare.wav": {},
        },
        "buses": {"drums": {}, "verb": {}},
        "sections": {"verse": {"start_s": 0, "end_s": 0.5}, "chorus": {"start_s": 0.5, "end_s": 1.0}},
        "steps": [
            step("k-hp", "Kick.wav", "HighpassFilter", cutoff_frequency_hz=40),
            step("k-comp", "Kick.wav", "Compressor", threshold_db=-20, ratio=4),
            step("b-dist", "Bass.wav", "Distortion", drive_db=12),
            step("b-comp", "Bass.wav", "Compressor", threshold_db=-18, ratio=3),
            step("b-duck", "Bass.wav", "Duck", key="Kick.wav", threshold_db=-30, ratio=4, range_db=6, attack_ms=1, release_ms=80),
            step("b-eq", "Bass.wav", "PeakFilter", cutoff_frequency_hz=200, gain_db=2, q=1),
            step("g-eq", "Gtr.wav", "LowShelfFilter", cutoff_frequency_hz=150, gain_db=-3, q=0.7),
            step("g-ride", "Gtr.wav", "Ride", segments=[["chorus", 2.0]]),
            step("d-comp", "drums", "Compressor", threshold_db=-15, ratio=2),
            step("v-rev", "verb", "Reverb", dry_level=0),
            # Float64 throughout, so the order of the mix's sum survives into its bytes.
            step("m-ride", "mix", "Ride", segments=[["verse", -1.0]]),
            dict(step("m-gain", "mix", "Gain", gain_db=3), stage="master"),
            dict(step("m-lim", "mix", "Limiter", threshold_db=-1), stage="master"),
        ],
    }


MIX_STEPS = 11


@pytest.fixture
def stems(song):
    write(song, "Kick.wav", noise(1, seed=1))
    write(song, "Bass.wav", noise(1, seed=2))
    write(song, "Gtr.wav", np.stack([noise(0.9, seed=3), noise(0.9, seed=4)], axis=1))
    write(song, "Snare.wav", noise(1, seed=5))
    return song


def nodes(song, cap=render.MEMO_BYTES, sightings=1):
    """Stores on first sight unless told otherwise, so most tests show reuse in two renders, not three."""
    return render.Memo(song / "renders" / "check" / "nodes", cap, sightings)


def stored(memo):
    return sum(path.stat().st_size for path in memo.folder.iterdir())


def changed(recipe, step_id, **settings):
    variant = copy.deepcopy(recipe)
    next(s for s in variant["steps"] if s["id"] == step_id)["settings"].update(settings)
    return variant


def swapped():
    recipe = song_recipe()
    recipe["stems"] = {name: recipe["stems"][name] for name in ("Snare.wav", "Gtr.wav", "Kick.wav", "Bass.wav")}
    return recipe


def variants():
    recipe = song_recipe()
    send = copy.deepcopy(recipe)
    send["stems"]["Bass.wav"]["sends"][0]["gain_db"] = -6
    moved = copy.deepcopy(recipe)
    moved["stems"]["Gtr.wav"]["offset_samples"] = 200
    panned = copy.deepcopy(recipe)
    panned["stems"]["Gtr.wav"]["pan"] = 0.3
    dropped = copy.deepcopy(recipe)
    dropped["steps"] = [s for s in dropped["steps"] if s["id"] != "b-comp"]
    sections = copy.deepcopy(recipe)
    sections["sections"]["chorus"]["start_s"] = 0.6
    sections["sections"]["verse"]["end_s"] = 0.6
    return [recipe, changed(recipe, "b-eq", gain_db=-4), changed(recipe, "k-comp", ratio=8), swapped(), send, moved,
            panned, dropped, sections, changed(recipe, "m-lim", threshold_db=-2), recipe]


def capture(before, after, rate):
    return before.tobytes(), after.tobytes(), rate


def outputs(recipe, song, memo, probes=("k-comp", "b-comp", "b-duck", "g-ride")):
    result = render.Result(recipe, song, sorted(render.sources(recipe)), probes={i: capture for i in probes}, memo=memo)
    master_in, master = result.master(result.mix)
    # Memory order too: a later sum over the array can depend on it.
    return {
        "mix": (result.mix.flags.f_contiguous, result.mix.tobytes()),
        "tapped": [(name, audio.shape, audio.flags.f_contiguous, audio.tobytes()) for name, audio in result.tapped.items()],
        "keyed": [(name, audio.shape, audio.flags.f_contiguous, audio.tobytes()) for name, audio in result.mixer.keyed.items()],
        "probed": list(result.mixer.probed.items()),
        "master": (master_in.tobytes(), master.tobytes()),
    }


@pytest.mark.parametrize("sightings", [1, 2])
@pytest.mark.parametrize("cap", [render.MEMO_BYTES, 3 * RATE * 2 * 8, 0])
def test_every_output_is_byte_identical_with_and_without_the_memo(stems, cap, sightings):
    memo = nodes(stems, cap, sightings)
    for recipe in variants():
        assert outputs(recipe, stems, memo) == outputs(recipe, stems, None)
        assert stored(memo) <= cap


def run(recipe, song, memo, probes=()):
    memo.ran = memo.reused = 0
    render.Result(recipe, song, probes={i: capture for i in probes}, memo=memo)
    return memo.ran, memo.reused


@pytest.mark.parametrize("variant, ran, reused", [
    # The kick, the guitar and the drums bus are whole hits; the bass resumes after its duck.
    (changed(song_recipe(), "b-eq", gain_db=-4), 3, 8),
    # The kick keeps its high-pass; the bass keeps distortion and compression but its duck key moved.
    (changed(song_recipe(), "k-comp", ratio=8), 6, 5),
    # Only the mix sum order changes.
    (swapped(), 1, 10),
    (changed(song_recipe(), "m-lim", threshold_db=-2), 0, MIX_STEPS),
])
def test_a_variant_runs_only_the_steps_its_change_reaches(stems, variant, ran, reused):
    memo = nodes(stems)
    assert run(song_recipe(), stems, memo) == (MIX_STEPS, 0)
    assert run(variant, stems, memo) == (ran, reused)


def test_a_probe_inside_a_reused_prefix_still_gets_its_before_and_after(stems):
    memo = nodes(stems)
    run(song_recipe(), stems, memo)
    assert run(changed(song_recipe(), "b-eq", gain_db=-4), stems, memo, probes=["b-comp"]) == (3, 8)


def test_each_variant_rendered_twice_reuses_everything_under_a_cap_the_current_render_fills(stems):
    # Every step output of the current render: kick 2 and bass 4 mono; guitar 2, drums, verb and mix 1 each, stereo.
    # Each file adds a 128-byte header and a 32-byte hash.
    memo = nodes(stems, (2 + 4 + 2 * (2 + 1 + 1 + 1)) * RATE * 8 + MIX_STEPS * 160)
    run(song_recipe(), stems, memo)
    for variant in (changed(song_recipe(), "b-eq", gain_db=-4), changed(song_recipe(), "k-comp", ratio=8)):
        run(variant, stems, memo)
        assert run(variant, stems, memo) == (0, MIX_STEPS)


def test_a_stem_rewritten_at_the_same_size_and_mtime_is_read_again(stems):
    memo = nodes(stems)
    before = outputs(song_recipe(), stems, memo)
    stat = (stems / "stems" / "Kick.wav").stat()
    write(stems, "Kick.wav", noise(1, seed=9))
    os.utime(stems / "stems" / "Kick.wav", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = outputs(song_recipe(), stems, memo)
    assert after == outputs(song_recipe(), stems, None)
    assert after["mix"] != before["mix"]


def test_a_failing_step_fails_the_same_way_before_a_key_it_pulls(stems):
    recipe = song_recipe()
    # The bass ducks from the guitar, rendered after it; both carry a bad setting, and the bass's must surface first.
    next(s for s in recipe["steps"] if s["id"] == "b-duck")["settings"]["key"] = "Gtr.wav"
    next(s for s in recipe["steps"] if s["id"] == "b-comp")["settings"]["knee_db"] = 1
    next(s for s in recipe["steps"] if s["id"] == "g-eq")["settings"]["slope"] = 1
    errors = []
    for memo in (None, nodes(stems)):
        with pytest.raises(TypeError) as error:
            render.Result(recipe, stems, memo=memo)
        errors.append(str(error.value))
    assert errors[0] == errors[1]
    assert "knee_db" in errors[0]


def test_changing_a_render_in_place_does_not_change_the_next(stems):
    memo = nodes(stems)
    # The first render's arrays went into the memo, the second's came out of it.
    for _ in range(2):
        result = render.Result(song_recipe(), stems, sorted(render.sources(song_recipe())), memo=memo)
        for audio in [result.mix, *result.tapped.values(), *result.mixer.keyed.values()]:
            audio *= 2
    assert outputs(song_recipe(), stems, memo) == outputs(song_recipe(), stems, None)


def test_a_later_step_whose_key_fails_still_fails_after_an_earlier_failing_step(stems):
    recipe = song_recipe()
    next(s for s in recipe["steps"] if s["id"] == "k-hp")["settings"]["knee_db"] = 1
    del next(s for s in recipe["steps"] if s["id"] == "k-comp")["settings"]
    for memo in (None, nodes(stems), nodes(stems, 0)):
        with pytest.raises(TypeError, match="knee_db"):
            render.Result(recipe, stems, memo=memo)


def test_a_bundle_added_after_a_built_in_render_is_loaded(stems, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    memo = nodes(stems)
    outputs(song_recipe(), stems, memo)
    (tmp_path / "plugins" / "Compressor.vst3").mkdir(parents=True)
    (tmp_path / "plugins" / "Compressor.vst3" / "binary").write_bytes(b"not a plugin")
    for cache in (None, memo):
        with pytest.raises(ImportError):
            render.Result(song_recipe(), stems, memo=cache)


def test_a_bundle_rewritten_at_the_same_size_changes_its_key(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path)
    binary = tmp_path / "X.vst3" / "binary"
    binary.parent.mkdir()
    binary.write_bytes(b"one")
    memo = nodes(tmp_path)
    first = memo.plugin("X")
    stat = binary.stat()
    binary.write_bytes(b"two")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert memo.plugin("X") != first


def test_a_bundle_counts_the_files_under_its_linked_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    (tmp_path / "plugins" / "X.vst3").mkdir(parents=True)
    (tmp_path / "binary").mkdir()
    (tmp_path / "binary" / "X").write_bytes(b"one")
    (tmp_path / "plugins" / "X.vst3" / "MacOS").symlink_to(tmp_path / "binary", target_is_directory=True)
    (tmp_path / "binary" / "loop").symlink_to(tmp_path / "plugins", target_is_directory=True)
    first = nodes(tmp_path).plugin("X")
    (tmp_path / "binary" / "X").write_bytes(b"two")
    assert nodes(tmp_path).plugin("X") != first


def test_a_rewritten_impulse_is_read_again_by_its_step_and_everything_after(stems):
    impulse = stems / "impulse.wav"
    soundfile.write(impulse, np.r_[1.0, np.zeros(511)], RATE, subtype="FLOAT")
    recipe = song_recipe()
    recipe["steps"].insert(0, step("k-ir", "Kick.wav", "Convolution", impulse_response_filename=str(impulse), mix=1.0))
    # Under the kick's level but short of the duck's range, so the new impulse changes the bass duck too.
    next(s for s in recipe["steps"] if s["id"] == "b-duck")["settings"]["threshold_db"] = -36
    memo = nodes(stems)
    before = outputs(recipe, stems, memo)
    stat = impulse.stat()
    soundfile.write(impulse, np.r_[1.0, np.zeros(255), 0.5, np.zeros(255)], RATE, subtype="FLOAT")
    os.utime(impulse, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = outputs(recipe, stems, memo)
    assert after == outputs(recipe, stems, None)
    assert after["mix"] != before["mix"]


def test_a_later_run_reuses_the_files_of_an_earlier_one(stems):
    assert run(song_recipe(), stems, nodes(stems)) == (MIX_STEPS, 0)
    memo = nodes(stems)
    assert run(song_recipe(), stems, memo) == (0, MIX_STEPS)
    variant = changed(song_recipe(), "b-eq", gain_db=-4)
    assert run(variant, stems, memo) == (3, 8)
    assert outputs(variant, stems, nodes(stems)) == outputs(variant, stems, None)


@pytest.mark.parametrize("damage", [
    lambda data: data[: len(data) // 2],
    lambda data: data[:-1] + bytes([data[-1] ^ 1]),
    lambda data: data[:200] + bytes([data[200] ^ 1]) + data[201:],
    # A header that asks for far more samples than the file holds.
    lambda data: data.replace(b"'shape': (", b"'shape': (999999999999, "),
    lambda data: b"",
])
def test_a_damaged_file_is_a_miss(stems, damage):
    memo = nodes(stems)
    run(song_recipe(), stems, memo)
    for path in memo.folder.iterdir():
        path.write_bytes(damage(path.read_bytes()))
    assert run(song_recipe(), stems, memo) == (MIX_STEPS, 0)
    for path in memo.folder.iterdir():
        path.write_bytes(damage(path.read_bytes()))
    assert outputs(song_recipe(), stems, memo) == outputs(song_recipe(), stems, None)


def test_changed_render_code_misses(stems):
    run(song_recipe(), stems, nodes(stems))
    memo = nodes(stems)
    memo.code = [*memo.code, "changed"]
    assert run(song_recipe(), stems, memo) == (MIX_STEPS, 0)


AUDIO = np.zeros((100, 2))
FILE = AUDIO.nbytes + 160


# Keys as the memo makes them: 64 hex digits, here one repeated so kept() can name them by it.
K = {c: c * 64 for c in "abcdef"}


def kept(memo):
    return sorted(re.sub(r"([0-9a-f])\1{63}", r"\1", path.name.removeprefix(f"{memo.tag}-"))
                  for path in memo.folder.iterdir())


def days_ago(path, days):
    os.utime(path, ns=(0, time.time_ns() - days * render.MEMO_STALE_NS))


def test_the_least_recently_used_file_goes_first_after_files_of_other_code_unused_for_a_day(tmp_path):
    memo, other = nodes(tmp_path, 3 * FILE), nodes(tmp_path, 3 * FILE)
    other.tag = "0" * 16
    memo.put(K["a"], AUDIO)
    memo.put(K["b"], AUDIO)
    other.put(K["e"], AUDIO)
    for key in "ab":
        days_ago(memo.path(K[key]), 3)
    days_ago(other.path(K["e"]), 2)
    memo.put(K["c"], AUDIO)
    assert kept(memo) == ["a.npy", "b.npy", "c.npy"]
    # Other code used within the day ranks by use alone, like this code's files.
    other.put(K["f"], AUDIO)
    assert kept(memo) == ["0000000000000000-f.npy", "b.npy", "c.npy"]
    assert memo.get(K["b"]) is not None
    memo.put(K["d"], AUDIO)
    assert kept(memo) == ["0000000000000000-f.npy", "b.npy", "d.npy"]


def test_eviction_skips_what_it_must_not_or_cannot_remove(tmp_path):
    memo = nodes(tmp_path, FILE)
    (memo.folder / "sub").mkdir()
    (memo.folder / "notes.txt").write_text("x")
    os.utime(memo.folder / "sub", (0, 0))
    memo.put(K["a"], AUDIO)
    memo.put(K["b"], AUDIO)
    assert kept(memo) == ["b.npy", "notes.txt", "sub"]


def test_a_file_over_the_cap_is_not_stored_and_evicts_nothing(tmp_path):
    memo = nodes(tmp_path, AUDIO.nbytes)
    memo.put(K["a"], AUDIO[:10].copy())
    memo.put(K["b"], AUDIO)
    assert kept(memo) == ["a.npy"]


def test_opening_the_memo_enforces_its_cap_on_old_and_stale_files(tmp_path):
    memo = nodes(tmp_path, 10 * FILE)
    memo.put(K["a"], AUDIO)
    memo.put(K["b"], AUDIO)
    stale = memo.folder / f"{memo.tag}-{K['e']}.123.tmp"
    stale.write_bytes(bytes(FILE))
    os.utime(stale, (0, 0))
    assert kept(nodes(tmp_path, 2 * FILE)) == ["a.npy", "b.npy"]
    assert kept(nodes(tmp_path, FILE)) == ["b.npy"]


def test_no_store_when_the_disk_is_nearly_full(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "MEMO_SPARE_BYTES", shutil.disk_usage(tmp_path).free)
    memo = nodes(tmp_path)
    memo.put(K["a"], AUDIO)
    assert memo.get(K["a"]) is None and kept(memo) == []


def test_a_file_another_process_evicts_is_a_miss(tmp_path):
    first, second = nodes(tmp_path), nodes(tmp_path, FILE)
    first.put(K["a"], AUDIO)
    second.put(K["b"], AUDIO)
    assert first.get(K["a"]) is None
    assert first.get(K["b"]) is not None


def header(flip=False, shape=None):
    def damage(path):
        with path.open("rb") as file:
            np.lib.format.read_magic(file)
            found = np.lib.format.read_array_header_1_0(file)
            payload = file.read()
        with path.open("wb") as file:
            np.lib.format.write_array_header_1_0(file, {"descr": "<f8", "fortran_order": found[1] != flip,
                                                         "shape": shape(found[0]) if shape else found[0]})
            file.write(payload)
    return damage


@pytest.mark.parametrize("damage", [
    header(flip=True),
    header(shape=lambda shape: (shape[0], True, shape[1])),
    header(shape=lambda shape: (-shape[0], -shape[1])),
    header(shape=lambda shape: (shape[0] * shape[1] // 2, 2) if shape[1] == 1 else (shape[0] * 2, 1)),
])
def test_a_damaged_header_is_a_miss(stems, damage):
    memo = nodes(stems)
    run(song_recipe(), stems, memo)
    for path in memo.folder.iterdir():
        damage(path)
    assert outputs(song_recipe(), stems, memo) == outputs(song_recipe(), stems, None)
    assert memo.reused == 0


def test_a_bad_memo_size_fails_only_when_the_memo_is_made(monkeypatch):
    import evidence
    for value in ("1.5", "\u00b2"):
        monkeypatch.setenv("STEMMASTER_NODE_MEMO_MB", value)
        with pytest.raises(SystemExit, match="whole number"):
            evidence.node_memo()
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_MB", "0")
    assert evidence.node_memo() is None


@pytest.mark.parametrize("audio", [np.arange(20.0).reshape(10, 2), np.asfortranarray(np.arange(20.0).reshape(10, 2))])
def test_a_stored_array_comes_back_with_its_bytes_and_order(tmp_path, audio):
    memo = nodes(tmp_path)
    memo.put(K["a"], audio)
    found = memo.get(K["a"])
    assert (found.tobytes(), found.flags.f_contiguous) == (audio.tobytes(), audio.flags.f_contiguous)


@pytest.mark.parametrize("folder", ["read-only/nodes", "file"])
def test_a_memo_folder_that_cant_be_made_turns_the_memo_off(tmp_path, monkeypatch, capsys, folder):
    import evidence
    (tmp_path / "read-only").mkdir(mode=0o500)
    (tmp_path / "file").write_text("x")
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(tmp_path / folder))
    assert evidence.node_memo() is None
    assert capsys.readouterr().err.startswith("warning: node memo off")


def test_every_single_bit_header_change_is_a_miss(tmp_path):
    memo = nodes(tmp_path)
    memo.put(K["a"], AUDIO)
    path = memo.path(K["a"])
    original = path.read_bytes()
    for offset in range(10 + int.from_bytes(original[8:10], "little")):
        for bit in range(8):
            path.write_bytes(original[:offset] + bytes([original[offset] ^ 1 << bit]) + original[offset + 1 :])
            assert memo.get(K["a"]) is None


def test_a_bundle_link_moved_to_another_folder_changes_its_key(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    bundle = tmp_path / "plugins" / "X.vst3"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "X").write_bytes(b"old")
    (bundle / "Contents" / "X").write_bytes(b"new")
    (bundle / "Contents" / "MacOS").symlink_to("..", target_is_directory=True)
    first = nodes(tmp_path).plugin("X")
    (bundle / "Contents" / "MacOS").unlink()
    (bundle / "Contents" / "MacOS").symlink_to(".", target_is_directory=True)
    assert nodes(tmp_path).plugin("X") != first


def test_a_step_whose_bundle_changes_before_it_finishes_is_not_kept_nor_anything_built_on_it(stems, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    apply = render.apply

    def swapping(audio, rate, step, context):
        out = apply(audio, rate, step, context)
        if step["id"] == "b-eq":
            (tmp_path / "plugins" / "PeakFilter.vst3").mkdir(parents=True)
        return out

    monkeypatch.setattr(render, "apply", swapping)
    run(song_recipe(), stems, nodes(stems))
    monkeypatch.setattr(render, "apply", apply)
    (tmp_path / "plugins" / "PeakFilter.vst3").rmdir()
    # The bass's EQ, the reverb its send feeds, and the mix ride.
    assert run(song_recipe(), stems, nodes(stems)) == (3, MIX_STEPS - 3)


def test_a_bundle_gone_before_its_load_and_back_after_still_fails_to_load(song, tmp_path, monkeypatch):
    write(song, "Noise.wav", noise(0.1))
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    bundle = tmp_path / "plugins" / "Gain.vst3"
    bundle.mkdir(parents=True)
    (bundle / "binary").write_bytes(b"not a plugin")
    recipe = {"stems": {"Noise.wav": {}}, "steps": [step("g", "Noise.wav", "Gain", gain_db=-2),
                                                     step("l", "Noise.wav", "LowpassFilter", cutoff_frequency_hz=8000),
                                                     step("d", "mix", "Distortion", drive_db=3)]}
    apply = render.apply

    def removing(audio, rate, step, context):
        if bundle.exists():
            shutil.rmtree(bundle)
        return apply(audio, rate, step, context)

    monkeypatch.setattr(render, "apply", removing)
    render.Result(recipe, song, memo=nodes(song))
    monkeypatch.setattr(render, "apply", apply)
    bundle.mkdir()
    (bundle / "binary").write_bytes(b"not a plugin")
    for memo in (None, nodes(song)):
        with pytest.raises(ImportError):
            render.Result(recipe, song, memo=memo)


def test_a_cache_folder_gone_after_a_write_is_only_a_lost_cache(stems, monkeypatch):
    memo = nodes(stems)
    replace = os.replace

    def moving(source, target):
        replace(source, target)
        if memo.folder.exists():
            memo.folder.rename(memo.folder.with_name("moved"))

    monkeypatch.setattr(render.os, "replace", moving)
    assert outputs(song_recipe(), stems, memo) == outputs(song_recipe(), stems, None)


def test_a_folder_at_the_temporary_path_is_only_a_lost_write(tmp_path):
    memo = nodes(tmp_path)
    path = memo.path(K["a"])
    path.with_name(f"{path.stem}.{os.getpid()}.tmp").mkdir()
    memo.put(K["a"], AUDIO)
    assert memo.get(K["a"]) is None


def test_bundle_files_with_the_same_bytes_in_a_row_but_split_differently_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    bundle = tmp_path / "plugins" / "X.vst3"
    bundle.mkdir(parents=True)
    keys = []
    for a, b in ((b"Xb\0Y", b"Z"), (b"X", b"Yb\0Z")):
        (bundle / "a").write_bytes(a)
        (bundle / "b").write_bytes(b)
        keys.append(nodes(tmp_path).plugin("X"))
    assert keys[0] != keys[1]


def test_a_file_over_the_cap_is_a_miss_before_its_samples_are_read(tmp_path, monkeypatch):
    memo = nodes(tmp_path, FILE)
    memo.put(K["a"], AUDIO)
    with memo.path(K["a"]).open("ab") as file:
        file.write(b"\0")
    monkeypatch.setattr(render, "bytearray", lambda size: pytest.fail("allocated"), raising=False)
    assert memo.get(K["a"]) is None


def test_a_file_dated_in_the_future_goes_before_a_new_one(tmp_path):
    memo = nodes(tmp_path, 2 * FILE)
    memo.put(K["a"], AUDIO)
    memo.put(K["b"], AUDIO)
    for key in "ab":
        os.utime(memo.path(K[key]), ns=(0, time.time_ns() + 10**15))
    memo.put(K["c"], AUDIO)
    assert kept(memo) == ["b.npy", "c.npy"]


def test_the_code_tag_is_fixed_at_import_and_holds_the_os_build(tmp_path, monkeypatch):
    before = nodes(tmp_path)
    monkeypatch.setattr(render.Path, "read_bytes", lambda self: b"edited")
    assert nodes(tmp_path).tag == before.tag
    assert {platform.platform(), os.uname().version} <= set(before.code)


def junk_bundle(bundle):
    bundle.mkdir(parents=True)
    (bundle / "binary").write_bytes(b"not a plugin")


def gain_recipe(song):
    write(song, "Noise.wav", noise(0.1))
    return {"stems": {"Noise.wav": {}}, "steps": [step("g", "Noise.wav", "Gain", gain_db=-2),
                                                   step("d", "mix", "Distortion", drive_db=3)]}


def test_a_bundle_gone_right_after_its_loader_is_taken_is_not_cached(song, tmp_path, monkeypatch):
    recipe = gain_recipe(song)
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    bundle = tmp_path / "plugins" / "Gain.vst3"
    junk_bundle(bundle)
    loader = render.Memo.loader

    def removing(memo, step):
        found = loader(memo, step)
        if bundle.exists():
            shutil.rmtree(bundle)
        return found

    monkeypatch.setattr(render.Memo, "loader", removing)
    render.Result(recipe, song, memo=nodes(song))
    monkeypatch.setattr(render.Memo, "loader", loader)
    junk_bundle(bundle)
    for memo in (None, nodes(song)):
        with pytest.raises(ImportError):
            render.Result(recipe, song, memo=memo)


def test_a_bundle_gone_during_its_step_and_back_with_the_same_bytes_is_not_cached(song, tmp_path, monkeypatch):
    recipe = gain_recipe(song)
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    bundle = tmp_path / "plugins" / "Gain.vst3"
    junk_bundle(bundle)
    apply = render.apply

    def swapping(audio, rate, step, context):
        if step["id"] != "g":
            return apply(audio, rate, step, context)
        shutil.rmtree(bundle)
        try:
            return apply(audio, rate, step, context)
        finally:
            junk_bundle(bundle)

    monkeypatch.setattr(render, "apply", swapping)
    render.Result(recipe, song, memo=nodes(song))
    monkeypatch.setattr(render, "apply", apply)
    for memo in (None, nodes(song)):
        with pytest.raises(ImportError):
            render.Result(recipe, song, memo=memo)


def test_a_damaged_file_whose_header_matches_its_size_is_a_miss_before_its_samples_get_memory(tmp_path, monkeypatch):
    memo = nodes(tmp_path)
    memo.put(K["a"], AUDIO)
    path = memo.path(K["a"])
    header = path.read_bytes()[: -AUDIO.nbytes - 32]
    big = header.replace(b"(100, 2)", b"(400, 2)")
    assert len(big) == len(header)
    with path.open("wb") as file:
        file.write(big)
        file.truncate(len(big) + 400 * 2 * 8 + 32)
    monkeypatch.setattr(render, "MEMO_CHUNK_BYTES", 1000)
    monkeypatch.setattr(render, "bytearray", lambda size: pytest.fail("allocated") if size > 1000 else builtins.bytearray(size),
                        raising=False)
    assert memo.get(K["a"]) is None


def test_a_bundle_with_an_unreadable_folder_has_no_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "PLUGINS", tmp_path)
    (tmp_path / "X.vst3" / "Contents").mkdir(parents=True)
    (tmp_path / "X.vst3" / "Contents").chmod(0)
    try:
        assert nodes(tmp_path).loader(step("x", "mix", "X")) is None
    finally:
        (tmp_path / "X.vst3" / "Contents").chmod(0o700)


def test_foreign_files_in_the_memo_folder_survive_eviction(tmp_path):
    folder = tmp_path / "nodes"
    folder.mkdir()
    foreign = ["measurements.npy", "editor.tmp", "0123456789abcdef-short.npy", f"{'A' * 16}-{K['a']}.npy",
               f"{'0' * 16}-{K['a']}.npy.bak", f"{'0' * 16}-{K['a']}.x.tmp", "notes.seen", f"{'A' * 16}-{K['a']}.seen"]
    for name in foreign:
        np.save(folder / "data.npy", AUDIO)
        (folder / "data.npy").rename(folder / name)
        days_ago(folder / name, 3)
    memo = render.Memo(folder, FILE, sightings=1)
    memo.put(K["a"], AUDIO)
    memo.put(K["b"], AUDIO)
    assert sorted(path.name for path in folder.iterdir()) == sorted(foreign + [memo.path(K["b"]).name])


def test_code_moved_from_one_file_to_the_next_changes_the_code_digest(tmp_path):
    for name, native, airwindows in (("a", "x = 1\nR = 0\n", "y = 2\n"), ("b", "x = 1\n", "R = 0\ny = 2\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "native.py").write_text(native)
        (tmp_path / name / "airwindows.py").write_text(airwindows)
    assert len({render.code_digest([tmp_path / name / "native.py", tmp_path / name / "airwindows.py"])
                for name in "ab"}) == 2


def test_a_bundle_made_after_its_loader_is_taken_but_before_a_hit_is_read_drops_the_hit(song, tmp_path, monkeypatch):
    recipe = gain_recipe(song)
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    render.Result(recipe, song, memo=nodes(song))
    get = render.Memo.get

    def making(memo, key):
        if not render.PLUGINS.exists():
            junk_bundle(tmp_path / "plugins" / "Gain.vst3")
        return get(memo, key)

    monkeypatch.setattr(render.Memo, "get", making)
    with pytest.raises(ImportError):
        render.Result(recipe, song, memo=nodes(song))


def test_no_home_folder_turns_the_memo_off(monkeypatch, capsys):
    import evidence

    def homeless():
        raise RuntimeError("no home")

    monkeypatch.delenv("STEMMASTER_NODE_MEMO_DIR", raising=False)
    monkeypatch.setattr(render.Path, "home", homeless)
    assert evidence.node_memo() is None
    assert "no home" in capsys.readouterr().err


def test_a_stale_pyc_turns_the_memo_off(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    for name in ("render.py", "native.py", "airwindows.py", "prepared.py"):
        shutil.copyfile(Path(render.__file__).with_name(name), code / name)
    native = code / "native.py"
    stat = native.stat()
    py_compile.compile(str(native), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    # Same size and mtime, so import still runs the old bytecode.
    native.write_text(native.read_text().replace("DEFAULT_RAMP_MS = 10", "DEFAULT_RAMP_MS = 20"))
    os.utime(native, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    script = (f"import sys; sys.path[:0] = [{str(code)!r}, {str(Path(render.__file__).parents[1] / 'analyze')!r}]; "
              "import native, render; print(native.DEFAULT_RAMP_MS, render.CODE is None)")

    def imported():
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.split()

    assert imported() == ["10", "True"]
    os.unlink(importlib.util.cache_from_source(str(native)))
    assert imported() == ["20", "False"]


def test_an_output_is_stored_on_its_second_computation_and_reused_on_its_third(stems):
    suffixes = []
    for ran, reused in ((MIX_STEPS, 0), (MIX_STEPS, 0), (0, MIX_STEPS)):
        memo = nodes(stems, sightings=2)
        assert outputs(song_recipe(), stems, memo) == outputs(song_recipe(), stems, None)
        assert (memo.ran, memo.reused) == (ran, reused)
        suffixes.append(sorted({path.suffix for path in memo.folder.iterdir()}))
    assert suffixes == [[".seen"], [".npy", ".seen"], [".npy", ".seen"]]


def test_seen_markers_count_toward_the_cap_and_go_least_recently_used_first(tmp_path):
    memo = render.Memo(tmp_path, 2 * render.MEMO_BLOCK_BYTES)
    for key in "abc":
        memo.put(K[key], AUDIO)
    assert kept(memo) == ["b.seen", "c.seen"]
    memo.put(K["a"], AUDIO)
    assert memo.get(K["a"]) is None and kept(memo) == ["a.seen", "c.seen"]


def test_markers_alone_keep_the_folder_under_its_cap(tmp_path):
    cap = 10 * render.MEMO_BLOCK_BYTES
    memo = render.Memo(tmp_path, cap)
    for index in range(30):
        memo.put(f"{index:064x}", AUDIO)
    assert len(list(tmp_path.iterdir())) * render.MEMO_BLOCK_BYTES <= cap + min(cap // 100, render.MEMO_MARKER_BUDGET_BYTES)


@pytest.mark.parametrize("make", [os.mkdir, os.mkfifo])
def test_a_folder_or_fifo_at_a_cache_path_is_a_miss_that_keeps_no_descriptor(tmp_path, make):
    memo = nodes(tmp_path)
    make(memo.path(K["a"]))
    descriptors = len(os.listdir("/dev/fd"))

    class Hung(BaseException):
        pass

    def hung(*_):
        raise Hung

    # A BaseException, so get()'s own except can't turn a hang into a miss.
    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    try:
        assert all(memo.get(K["a"]) is None for _ in range(50))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert len(os.listdir("/dev/fd")) == descriptors


def test_sightings_are_one_or_two(tmp_path):
    with pytest.raises(ValueError):
        render.Memo(tmp_path, sightings=3)


def test_a_marker_is_charged_its_real_size(tmp_path):
    memo = render.Memo(tmp_path, 2 * render.MEMO_BLOCK_BYTES)
    memo.put(K["a"], AUDIO)
    memo.path(K["a"]).with_suffix(".seen").write_bytes(bytes(4 * render.MEMO_BLOCK_BYTES))
    assert kept(render.Memo(tmp_path, 2 * render.MEMO_BLOCK_BYTES)) == []


def test_a_stored_or_reused_file_keeps_its_marker_newer_than_it(tmp_path):
    memo = render.Memo(tmp_path, FILE + render.MEMO_BLOCK_BYTES)
    for _ in range(2):
        memo.put(K["a"], AUDIO)
    days_ago(memo.path(K["a"]).with_suffix(".seen"), 2)
    assert memo.get(K["a"]) is not None
    stats = [memo.path(K["a"]).with_suffix(suffix).stat().st_mtime_ns for suffix in (".npy", ".seen")]
    assert stats[1] >= stats[0]


def test_nothing_the_memo_creates_or_reads_follows_a_planted_link(song, tmp_path, monkeypatch):
    recipe = gain_recipe(song)
    monkeypatch.setattr(render, "PLUGINS", tmp_path / "plugins")
    memo = nodes(song, sightings=2)
    render.Result(recipe, song, memo=memo)
    for path in memo.folder.iterdir():
        path.unlink()
        path.symlink_to(tmp_path / "plugins" / "Gain.vst3")
        path.with_name(f"{path.stem}.{os.getpid()}.tmp").symlink_to(tmp_path / "elsewhere")
    stored = nodes(song, sightings=1)
    stored.put(K["a"], AUDIO)
    stored.path(K["b"]).symlink_to(stored.path(K["a"]))
    for memo in (nodes(song, sightings=2), nodes(song, sightings=1)):
        assert outputs(recipe, song, memo, probes=()) == outputs(recipe, song, None, probes=())
    assert not (tmp_path / "plugins").exists() and not (tmp_path / "elsewhere").exists()
    assert stored.get(K["b"]) is None


def test_a_temp_file_left_under_this_pid_does_not_block_a_store(tmp_path):
    memo = nodes(tmp_path)
    path = memo.path(K["a"])
    path.with_name(f"{path.stem}.{os.getpid()}.tmp").write_bytes(b"left by a killed process")
    memo.put(K["a"], AUDIO)
    assert memo.get(K["a"]) is not None
