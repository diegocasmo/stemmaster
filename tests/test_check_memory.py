import json
import shutil
import tracemalloc
from types import SimpleNamespace

import numpy as np
import pytest

import evidence
import spill
import structure
import workers
from test_workers import RATE, band, mastered


def report_bytes(song, recipe, master_mode, monkeypatch, memo):
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(memo))
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    evidence.report(json.loads(json.dumps(recipe)), song, nodes, master_mode)
    cache = song / "renders" / "check"
    return [(cache / name).read_bytes() for name in ("report.json", "report.md")]


@pytest.mark.parametrize("audio", [
    np.random.default_rng(0).standard_normal((1000, 2)),
    np.asfortranarray(np.random.default_rng(1).standard_normal((1000, 2))),
    np.random.default_rng(2).standard_normal((1000, 1)),
    np.random.default_rng(3).standard_normal(1000),
    np.random.default_rng(4).standard_normal((1000, 2)).astype(np.float32),
], ids=["c", "fortran", "mono", "1d", "float32"])
def test_a_spilled_tap_reads_back_the_same_array(monkeypatch, audio):
    monkeypatch.setattr(spill, "RESIDENT", 0)
    monkeypatch.setattr(spill, "BUDGET", 0)
    taps = spill.Taps()
    taps["a"] = audio.copy(order="K")
    taps["b"] = audio * 2

    assert not taps.resident
    for name, expected in (("a", audio), ("b", audio * 2), ("a", audio)):
        got = taps[name]
        assert got.tobytes(order="A") == expected.tobytes(order="A")
        assert (got.shape, got.dtype, got.flags.c_contiguous, got.flags.f_contiguous) == \
            (expected.shape, expected.dtype, expected.flags.c_contiguous, expected.flags.f_contiguous)
    assert list(taps) == ["a", "b"] and "a" in taps and taps.get("c") is None
    with pytest.raises(KeyError):
        taps["c"]


def test_a_tap_numpy_cant_store_in_its_own_order_stays_in_memory(monkeypatch):
    monkeypatch.setattr(spill, "RESIDENT", 0)
    monkeypatch.setattr(spill, "BUDGET", 0)
    taps = spill.Taps()
    strided = np.random.default_rng(0).standard_normal((1000, 4))[:, ::2]
    taps["a"] = strided

    assert taps["a"] is strided


def test_a_tap_that_cant_be_written_stays_in_memory(monkeypatch):
    def full(*args):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(spill, "RESIDENT", 0)
    monkeypatch.setattr(spill, "BUDGET", 0)
    monkeypatch.setattr(spill.os, "pwrite", full)
    taps = spill.Taps()
    audio = np.ones((10, 2))
    taps["a"] = audio

    assert taps["a"] is audio


def test_only_the_last_used_taps_stay_in_memory(monkeypatch):
    monkeypatch.setattr(spill, "RESIDENT", 2)
    monkeypatch.setattr(spill, "BUDGET", 0)
    first, second = spill.Taps(), spill.Taps()
    for name in "abc":
        first[name] = np.full((10, 2), ord(name), float)
    first["a"]
    second["x"] = np.zeros((10, 2))

    assert set(first.resident) == {"a"} and set(second.resident) == {"x"}


def old_matched_master(result, target):
    """matched_master before it kept only the best trial."""
    def trial(gain_db):
        master_in, out = result.master(result.mix * 10 ** (gain_db / 20))
        return master_in, out, evidence.loudness(out, result.rate) - target

    tried = {0.0: trial(0.0)}
    low, high, gain = -np.inf, np.inf, 0.0
    for _ in range(evidence.MATCH_ROUNDS):
        error = tried[gain][2]
        if not np.isfinite(error) or abs(error) <= evidence.MATCH_LU:
            break
        low, high = (low, min(high, gain)) if error > 0 else (max(low, gain), high)
        gain = gain - error
        if not low < gain < high:
            gain = (low + high) / 2
        tried[gain] = trial(gain)
    best = min(tried, key=lambda g: abs(tried[g][2]) if np.isfinite(tried[g][2]) else np.inf)
    master_in, out, error = tried[best]
    return {"drive_change_db": round(best, 2), "residual_lu": evidence.measure.rounded(error),
            "matched": bool(np.isfinite(error) and abs(error) <= evidence.MATCH_LU)}, master_in, out


def clipper(ceiling, calls):
    audio = np.random.default_rng(0).standard_normal((RATE * 2, 2)) * 0.1

    def master(mix):
        calls.append(float(np.abs(mix).max()))
        return mix, np.clip(mix, -ceiling, ceiling)
    return SimpleNamespace(rate=RATE, mix=audio, master=master)


@pytest.mark.parametrize("ceiling, target", [(1.0, -14), (0.05, -6), (0.05, -60), (1e-9, -14)],
                         ids=["converges", "stalls on the ceiling", "quieter", "silent"])
def test_the_matched_master_is_the_one_every_trial_kept_would_pick(ceiling, target):
    old_calls, new_calls = [], []
    old = old_matched_master(clipper(ceiling, old_calls), target)
    new = evidence.matched_master(clipper(ceiling, new_calls), target)

    assert new_calls == old_calls
    assert new[0] == old[0]
    assert new[1].tobytes() == old[1].tobytes() and new[2].tobytes() == old[2].tobytes()


@pytest.mark.parametrize("build, master_mode", [(band, False), (mastered, True)], ids=["mix", "master"])
def test_taps_on_disk_give_the_same_report_bytes_as_taps_in_memory(tmp_path, monkeypatch, build, master_mode):
    disk, memory = tmp_path / "disk", tmp_path / "memory"
    recipe = build(disk, 2)
    shutil.copytree(disk / "stems", memory / "stems")
    written = []
    real = spill.Taps._spill
    monkeypatch.setattr(spill.Taps, "_spill", lambda self, name: written.append(name) or real(self, name))
    monkeypatch.setattr(spill, "RESIDENT", 0)
    monkeypatch.setenv(workers.BUDGET_ENV, "0")
    spilled = report_bytes(disk, recipe, master_mode, monkeypatch, tmp_path / "memo-disk")
    assert written
    written.clear()
    monkeypatch.setenv(workers.BUDGET_ENV, str(1 << 20))
    held = report_bytes(memory, recipe, master_mode, monkeypatch, tmp_path / "memo-memory")

    assert not written
    assert spilled == held


def traced_peak(song, seconds, monkeypatch, memo):
    recipe = band(song, seconds)
    monkeypatch.setenv(workers.BUDGET_ENV, "0")
    tracemalloc.start()
    try:
        report_bytes(song, recipe, False, monkeypatch, memo)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_taps_stay_in_memory_while_they_fit_the_budget(monkeypatch):
    monkeypatch.setattr(spill, "RESIDENT", 1)
    monkeypatch.setattr(spill, "BUDGET", 3 * 160)
    taps = spill.Taps()
    for name in "abcd":
        taps[name] = np.zeros((10, 2))

    assert set(taps.resident) == {"b", "c", "d"} and set(taps.spilled) == {"a"}


@pytest.mark.parametrize("free, n, expected_gb", [(0.7, 3, 0.5 * (0.4 * 16 / 3 - 2)), (0.7, 1, 0.5 * (0.4 * 16 - 2)),
                                                    (0.35, 1, 0), (None, 1, 0)])
def test_the_budget_is_half_of_what_the_share_above_the_floor_leaves(monkeypatch, free, n, expected_gb):
    monkeypatch.delenv(workers.BUDGET_ENV, raising=False)
    monkeypatch.setattr(workers, "free_fraction", lambda: free)
    GB = 1 << 30

    assert workers.budget(2 * GB, n, total=16 * GB) == pytest.approx(expected_gb * GB, abs=1)


def test_a_checks_peak_grows_by_few_signals_per_second_of_song(tmp_path, monkeypatch):
    # The slope between two lengths leaves out what doesn't grow with the song (code, plugins, hit lists). Measured
    # 43 whole-song signals with every tap held in memory, 12 to 14 with them on disk.
    # A first check also loads code and plugins, which would count against whichever length ran first.
    report_bytes(tmp_path / "warm", band(tmp_path / "warm", 1), False, monkeypatch, tmp_path / "memo-warm")
    short, long = (traced_peak(tmp_path / str(s), s, monkeypatch, tmp_path / f"memo-{s}") for s in (3, 6))
    signal = 3 * RATE * 16

    assert (long - short) / signal < 25
