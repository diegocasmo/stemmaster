import gc
import json
import pickle
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

import evidence
import measure
import memo


@pytest.fixture(autouse=True)
def fresh_memo(monkeypatch):
    cache = memo.MeasurementMemo()
    monkeypatch.setattr(memo, "CACHE", cache)
    return cache


@pytest.fixture
def signal():
    rate = 8000
    audio = np.random.default_rng(11).normal(0, 0.03, (4 * rate, 2))
    times = np.array([1600, 6400, 12800, 19200, 25600])
    sections = {"a": {"start_s": 0, "end_s": 4, "group": "verse", "loudness_lu": 0}}
    return audio, rate, times, sections


def test_battery_json_is_byte_identical_to_main(signal):
    audio, rate, times, sections = signal
    expected = (Path(__file__).parent / "fixtures" / "measurement-battery.json").read_bytes()
    points = {"source.kick": ("kick", times), "source.bass": ("bass", None)}

    for _ in range(2):
        result = SimpleNamespace(rate=rate, mix=audio.copy(), tapped={"kick": audio.copy(), "bass": audio * 0.7},
                                 master_steps=[{}], master=lambda signal: (signal.copy(), signal * 0.8))
        numbers = evidence.battery({"sections": sections}, result, points, {"kick": times},
                                   {"kick": ["kick"], "bass": ["bass"]}, evidence.Master(result, None))
        assert json.dumps(numbers, separators=(",", ":")).encode() == expected


@pytest.mark.parametrize("changed", ["audio", "nested"])
def test_changed_inputs_invalidate_the_reading(changed):
    @memo.memoized
    def reading(audio, sections):
        return float(audio.sum()) + sections["a"]["end_s"]

    audio = np.zeros((4, 2))
    sections = {"a": {"end_s": 1}}
    before = reading(audio, sections)
    if changed == "audio":
        audio[0, 0] = 1
    else:
        sections["a"]["end_s"] = 2

    assert reading(audio, sections) != before


def test_profile_file_edits_invalidate_the_reading(signal, tmp_path, monkeypatch):
    audio, rate, _, _ = signal
    directory = tmp_path / "profiles" / "test"
    directory.mkdir(parents=True)
    path = directory / "profile.json"
    reference = {field: -20 for field in measure.PROFILE_WHOLE_FIELDS + measure.PROFILE_LOUD_FIELDS}
    profile = {"sample_rate": rate, "references": [reference | {"bands_db": {}}]}
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(measure, "ROOT", tmp_path)
    before = measure.profile_range(audio, rate, "test")
    profile["references"][0]["integrated_lufs"] = -12
    path.write_text(json.dumps(profile))

    after = measure.profile_range(audio, rate, "test")

    assert after["integrated_lufs"]["min"] == -12
    assert after != before


def test_cached_results_are_independent_copies():
    calls = Mock()

    @memo.memoized
    def reading(audio, rate):
        calls()
        return {"nested": [{"value": np.array([-0.0, np.nan, np.inf])}]}

    audio = np.ones((4, 2))
    first = reading(audio, 8000)
    expected = pickle.dumps(first, protocol=5)
    first["nested"][0]["value"][:] = 123
    second = reading(audio.copy(), 8000)
    assert pickle.dumps(second, protocol=5) == expected
    second["nested"].clear()
    assert pickle.dumps(reading(audio, 8000), protocol=5) == expected
    calls.assert_called_once()


@pytest.mark.parametrize("max_bytes, max_entries", [(512, 2), (110, 10)], ids=["entry-bound", "byte-bound"])
def test_memory_is_bounded_and_audio_is_not_retained(monkeypatch, max_bytes, max_entries):
    cache = memo.MeasurementMemo(max_bytes=max_bytes, max_entries=max_entries)
    monkeypatch.setattr(memo, "CACHE", cache)

    @memo.memoized
    def reading(audio):
        return float(audio.sum())

    audio = np.ones((16, 2))
    reference = weakref.ref(audio)
    for gain in range(5):
        reading(audio * gain)
        assert len(cache.entries) <= max_entries
        assert cache.size <= max_bytes
    reading(audio)
    del audio
    gc.collect()
    assert reference() is None

    cache.clear()
    cache.max_result_bytes = 16
    reading(np.ones((4, 2)))
    assert not cache.entries
    assert cache.size == 0
