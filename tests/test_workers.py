import json
import shutil

import numpy as np
import pytest
import soundfile

import evidence
import structure
import workers

RATE = 44100


def noise(seconds, seed):
    return np.random.default_rng(seed).standard_normal(round(seconds * RATE)) * 0.1


def hits(seconds, every, seed):
    audio = noise(seconds, seed) * 0.05
    for start in range(0, len(audio), round(every * RATE)):
        audio[start : start + 2000] += np.exp(-np.arange(min(2000, len(audio) - start)) / 300)
    return audio


def step(step_id, target, plugin, **settings):
    return {"id": step_id, "target": target, "plugin": plugin, "settings": settings}


def band(song, seconds):
    """Kick and snare into a drums bus, a bass the kick ducks, a guitar pair with a reverb send, and a trial master."""
    (song / "stems").mkdir(parents=True, exist_ok=True)
    for name, audio in {"Kick.wav": hits(seconds, 0.5, 1), "Snare.wav": hits(seconds, 0.75, 2), "Bass.wav": noise(seconds, 3),
                        "GtrL.wav": noise(seconds, 4), "GtrR.wav": noise(seconds, 5)}.items():
        soundfile.write(song / "stems" / name, audio, RATE, subtype="FLOAT")
    return {
        "profile": "instrumental-prog-metal",
        "stems": {"Kick.wav": {"bus": "drums"}, "Snare.wav": {"bus": "drums", "sends": [{"bus": "snare-verb", "gain_db": -10}]},
                  "Bass.wav": {}, "GtrL.wav": {"bus": "guitars", "pan": -1}, "GtrR.wav": {"bus": "guitars", "pan": 1}},
        "buses": {"drums": {}, "snare-verb": {"bus": "drums"}, "guitars": {}},
        "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]},
                    "bass": {"chain": "bass", "stems": ["Bass.wav"]}, "guitars": {"chain": "guitars", "stems": ["GtrL.wav", "GtrR.wav"]}},
        "steps": [
            step("kick-comp", "Kick.wav", "Compressor", threshold_db=-20, ratio=4),
            step("snare-verb", "snare-verb", "Reverb", dry_level=0, room_size=0.3),
            step("bass-hpf", "Bass.wav", "HighpassFilter", cutoff_frequency_hz=60),
            step("bass-duck", "Bass.wav", "Duck", key="Kick.wav", threshold_db=-30, ratio=4, range_db=6, attack_ms=1,
                 release_ms=80, band_hz=[None, 200]),
            step("gtr-hpf", "guitars", "HighpassFilter", cutoff_frequency_hz=90),
            step("drums-comp", "drums", "Compressor", threshold_db=-18, ratio=2),
            dict(step("master-drive", "mix", "Gain", gain_db=0), stage="master"),
            dict(step("master-limit", "mix", "Limiter", threshold_db=-3, release_ms=100), stage="master"),
        ],
        "moves": [
            {"move": "kick.compress", "source": "kick", "decision": "kept", "steps": ["kick-comp"]},
            {"move": "kick.duck-bass", "source": "kick", "decision": "kept", "steps": ["bass-duck"]},
            {"move": "snare.reverb", "source": "snare", "decision": "kept", "steps": ["snare-verb"],
             "fields": ["stems.Snare.wav.sends.snare-verb"]},
            {"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["bass-hpf"]},
            {"move": "guitars.filters", "source": "guitars", "decision": "kept", "steps": ["gtr-hpf"]},
            {"move": "drum-bus.compress", "source": "drum-bus", "decision": "kept", "steps": ["drums-comp"]},
            {"move": "mix-bus.trial-master", "source": "mix-bus", "decision": "kept", "steps": ["master-drive", "master-limit"]},
            {"move": "snare.compress", "source": "snare", "decision": "dropped", "trial": {"steps": [
                step("snare-trial", "Snare.wav", "Compressor", threshold_db=-24, ratio=6)]}},
            {"move": "bass.limit", "source": "bass", "decision": "dropped", "trial": {"steps": [
                step("bass-trial", "Bass.wav", "Limiter", threshold_db=-12)]}},
            {"move": "kick.hpf", "source": "kick", "decision": "dropped"},
            {"move": "guitars.align", "source": "guitars", "decision": "dropped"},
            # Default trials: a plugin matched to equal loudness, and a send through plugins to a bus of its own.
            {"move": "kick.saturate", "source": "kick", "decision": "dropped"},
            {"move": "bass.chorus", "source": "bass", "decision": "dropped"},
        ],
    }


def mastered(song, seconds):
    recipe = band(song, seconds)
    # Without the plugin default trials, so two workers and this process stay under 1 GB.
    dropped = {"mix-bus.trial-master", "kick.saturate", "bass.chorus"}
    recipe["moves"] = [m for m in recipe["moves"] if m["move"] not in dropped] + [
        {"move": "master.limit", "source": "master", "decision": "kept", "steps": ["master-drive", "master-limit"]},
        {"move": "master.compress", "source": "master", "decision": "dropped", "trial": {"steps": [
            dict(step("master-comp", "mix", "Compressor", threshold_db=-20, ratio=3), stage="master", before="master-limit")]}},
    ]
    return recipe


def broken(song, seconds):
    """A trial with a setting its plugin doesn't take: the check raises, in one process or with workers."""
    recipe = band(song, seconds)
    recipe["moves"].append({"move": "mix-bus.compress", "source": "mix-bus", "decision": "dropped", "trial": {"steps": [
        step("bad", "mix", "Compressor", bogus=1)]}})
    return recipe


def check(song, recipe, master_mode, worker_count, monkeypatch, memo):
    monkeypatch.setenv(workers.ENV, str(worker_count))
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(memo))
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    try:
        evidence.report(json.loads(json.dumps(recipe)), song, nodes, master_mode)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    cache = song / "renders" / "check"
    return [(cache / name).read_bytes() for name in ("report.json", "report.md")]


@pytest.mark.parametrize("build, master_mode", [(band, False), (mastered, True), (broken, False)],
                         ids=["mix", "master", "error"])
def test_workers_give_the_same_report_bytes_as_one_process(tmp_path, monkeypatch, build, master_mode):
    monkeypatch.setattr(workers, "free_fraction", lambda: 0.9)
    one, two = tmp_path / "one", tmp_path / "two"
    recipe = build(one, 2)
    shutil.copytree(one / "stems", two / "stems")
    started = []
    real = workers.futures.ProcessPoolExecutor
    monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", lambda *a, **k: started.append(a[0]) or real(*a, **k))

    # Workers first, while this process is small: the whole test stays under 1 GB.
    shared = check(two, recipe, master_mode, 2, monkeypatch, tmp_path / "memo-two")
    alone = check(one, recipe, master_mode, 1, monkeypatch, tmp_path / "memo-one")

    assert started == [2]
    assert shared == alone
    if build is not broken:
        assert (two / "renders" / "check" / workers.PEAKS).exists()


def test_a_killed_worker_leaves_the_check_to_finish_the_rest(tmp_path, monkeypatch):
    """Free memory under the floor stops every worker; the check computes what they didn't cache, with the same bytes."""
    one, two = tmp_path / "one", tmp_path / "two"
    recipe = band(one, 2)
    shutil.copytree(one / "stems", two / "stems")
    alone = check(one, recipe, False, 1, monkeypatch, tmp_path / "memo-one")
    monkeypatch.setattr(workers, "free_fraction", lambda: 0.1)

    assert check(two, recipe, False, 2, monkeypatch, tmp_path / "memo-two") == alone


def test_a_warm_check_starts_no_workers(tmp_path, monkeypatch):
    recipe = band(tmp_path, 2)
    check(tmp_path, recipe, False, 1, monkeypatch, tmp_path / "memo")
    monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", None)

    check(tmp_path, recipe, False, 2, monkeypatch, tmp_path / "memo")


GB = 1 << 30


@pytest.mark.parametrize("free, need, cpus, expected", [
    (0.60, 2 * GB, 8, 2),   # (60% - 30%) of 16 GB is 4.8 GB: two 2 GB workers
    (0.90, 1 * GB, 8, 8),   # memory for 9, but 8 cores
    (0.90, 1 * GB, 4, 4),
    (0.40, 2 * GB, 8, 1),   # 1.6 GB spare: one process
    (0.25, 1 * GB, 8, 1),   # already under the floor
], ids=["memory", "cores", "fewer-cores", "one", "under-floor"])
def test_the_worker_count_follows_free_memory_and_cores(monkeypatch, free, need, cpus, expected):
    monkeypatch.delenv(workers.ENV)

    assert workers.count(20, need, free=free, total=16 * GB, cpus=cpus) == expected


def test_the_worker_count_is_never_more_than_the_parts(monkeypatch):
    monkeypatch.delenv(workers.ENV)

    assert workers.count(3, GB, free=0.9, total=16 * GB, cpus=8) == 3


def test_unknown_free_memory_runs_one_process(monkeypatch):
    monkeypatch.delenv(workers.ENV)
    monkeypatch.setattr(workers, "free_fraction", lambda: None)

    assert workers.count(20, GB, total=16 * GB, cpus=8) == 1


@pytest.mark.parametrize("value, expected", [("4", 4), ("1", 1), ("0", 1), ("50", 20)])
def test_the_env_overrides_the_worker_count(monkeypatch, value, expected):
    monkeypatch.setenv(workers.ENV, value)

    assert workers.count(20, 100 * GB, free=0.1, total=16 * GB, cpus=8) == expected


def test_a_bad_env_value_stops_the_check(monkeypatch):
    monkeypatch.setenv(workers.ENV, "many")

    with pytest.raises(SystemExit, match=workers.ENV):
        workers.count(20, GB, free=0.9, total=16 * GB, cpus=8)


def test_the_estimate_uses_the_largest_measured_peak(tmp_path):
    signal, taps = 10 << 20, 20
    model = workers.estimate(tmp_path, signal, taps)
    workers.record(tmp_path, signal, taps, [workers.BASE_BYTES + 2 * signal * taps])
    measured = workers.estimate(tmp_path, signal, taps)
    workers.record(tmp_path, signal, taps, [workers.BASE_BYTES + signal * taps])

    assert model == workers.BASE_BYTES + workers.TAP_SIGNALS * signal * taps
    assert measured == workers.estimate(tmp_path, signal, taps) == workers.BASE_BYTES + 2 * signal * taps
