import json
import os
import shutil

import numpy as np
import pytest

import evidence
import memo
import structure
from test_workers import band


def report_bytes(song, recipe, monkeypatch, memo_dir):
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(memo_dir))
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    evidence.report(json.loads(json.dumps(recipe)), song, nodes)
    return [(song / "renders" / "check" / name).read_bytes() for name in ("report.json", "report.md")]


def test_a_recheck_reads_the_plan_from_disk_with_the_same_bytes(tmp_path, monkeypatch):
    recipe = band(tmp_path, 1)
    cold = report_bytes(tmp_path, recipe, monkeypatch, tmp_path / "memo")
    for path in (tmp_path / "renders" / "check").glob("contract-*.json"):
        path.unlink()
    monkeypatch.setattr(evidence, "plan", lambda *args: pytest.fail("plan ran again"))

    assert report_bytes(tmp_path, recipe, monkeypatch, tmp_path / "memo") == cold


@pytest.mark.parametrize("edit, same", [
    (lambda r: r["steps"][0]["settings"].update(ratio=8), True),
    (lambda r: r["stems"]["Bass.wav"].update(gain_db=-6, pan=0.3), True),
    (lambda r: r["stems"]["Kick.wav"].update(offset_samples=10), False),
    (lambda r: r["stems"]["Kick.wav"].update(bus="guitars"), False),
    (lambda r: r["sources"]["kick"].update(timing="Snare.wav"), False),
    (lambda r: r.update(steps=[s for s in r["steps"] if s["id"] not in ("kick-comp", "gtr-hpf")]), True),
    (lambda r: r.update(steps=[s for s in r["steps"] if s["id"] != "drums-comp"]), False),
])
def test_the_plan_key_holds_what_plan_reads(tmp_path, edit, same):
    recipe = band(tmp_path, 1)
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    outside = evidence.inputs(tmp_path, recipe)
    before = evidence.plan_key(recipe, nodes, outside)
    edit(recipe)

    assert (evidence.plan_key(recipe, nodes, outside) == before) == same


def test_a_fresh_song_folder_measures_nothing_twice_and_gives_the_same_bytes(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    recipe = band(first, 1)
    shutil.copytree(first / "stems", second / "stems")
    memo.CACHE.clear()
    cold = report_bytes(first, recipe, monkeypatch, tmp_path / "memo-1")
    memo.CACHE.clear()
    measured = []
    real = memo.MeasurementMemo.get
    monkeypatch.setattr(memo.MeasurementMemo, "get",
                        lambda self, key, compute: real(self, key, lambda: measured.append(key) or compute()))

    again = report_bytes(second, recipe, monkeypatch, tmp_path / "memo-2")

    assert again == cold
    assert measured == []


def readings(folder, cap=1 << 20):
    calls = []
    cache = memo.MeasurementMemo(disk=memo.Disk(folder, cap))

    def reading(audio):
        calls.append(1)
        return {"sum": float(audio.sum()), "peaks": np.abs(audio).max(axis=0)}

    return cache, reading, calls


def test_a_reading_on_disk_comes_back_bit_exact_in_another_process_memo(tmp_path):
    audio = np.random.default_rng(3).normal(0, 0.1, (4800, 2))
    cache, reading, calls = readings(tmp_path)
    first = cache.get(b"key", lambda: reading(audio))
    other, _, other_calls = readings(tmp_path)

    found = other.get(b"key", lambda: reading(audio))

    assert other_calls == [] and len(calls) == 1
    assert found["sum"] == first["sum"] and found["peaks"].tobytes() == first["peaks"].tobytes()


def test_a_damaged_file_or_other_code_is_a_miss(tmp_path, monkeypatch):
    audio = np.ones((10, 2))
    cache, reading, _ = readings(tmp_path)
    cache.get(b"key", lambda: reading(audio))
    path = cache.disk.path(b"key")
    path.write_bytes(path.read_bytes()[:-1])
    other, reading, calls = readings(tmp_path)
    other.get(b"key", lambda: reading(audio))
    assert calls == [1]

    monkeypatch.setattr(memo, "code_tag", lambda: b"other code")
    changed, reading, calls = readings(tmp_path)
    changed.get(b"key", lambda: reading(audio))
    assert calls == [1]


def test_the_disk_keeps_under_its_cap_dropping_the_least_recently_used(tmp_path):
    cache, _, _ = readings(tmp_path, cap=4000)
    for index in range(20):
        cache.get(f"key{index}".encode(), lambda: np.zeros(100))
        os.utime(cache.disk.path(f"key{index}".encode()), ns=(index * 10**9, index * 10**9))
    cache.disk.evict()

    kept = sorted(tmp_path.glob("*.pkl"))
    assert sum(path.stat().st_size for path in kept) <= 4000
    assert cache.disk.path(b"key19") in kept and cache.disk.path(b"key0") not in kept


def test_zero_megabytes_turns_the_disk_off(monkeypatch):
    monkeypatch.setenv("STEMMASTER_MEASURE_MEMO_MB", "0")
    assert memo.Disk.from_env() is None
