import json
import shutil

import numpy as np
import pytest
import soundfile

import evidence
import render
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


# Default trials: a plugin matched to equal loudness, and a send through plugins to a bus of its own. They need the
# local VST3 bundles, which CI doesn't have.
PLUGIN_TRIALS = [
    {"move": "kick.saturate", "source": "kick", "decision": "dropped"},
    {"move": "bass.chorus", "source": "bass", "decision": "dropped"},
] if all((render.PLUGINS / f"{name}.vst3").exists() for name in ("T-Saturator", "ZL Equalizer 2", "Airwindows Consolidated")) else []


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
        ] + PLUGIN_TRIALS,
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
    samples = iter([0.9, 0.9, 0.9])
    monkeypatch.setattr(workers, "free_fraction", lambda: next(samples, 0.1))

    assert check(two, recipe, False, 2, monkeypatch, tmp_path / "memo-two") == alone


@pytest.mark.parametrize("free", [None, 0.1], ids=["unknown", "under-floor"])
def test_workers_never_start_without_room(tmp_path, monkeypatch, free):
    recipe = band(tmp_path, 2)
    monkeypatch.setattr(workers, "free_fraction", lambda: free)
    pool = NoSubmit()
    monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", lambda *args, **kwargs: pool)
    monkeypatch.setattr(pool, "submit", lambda *args: pytest.fail("a worker started"))

    check(tmp_path, recipe, False, 2, monkeypatch, tmp_path / "memo")


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


def test_a_measured_peak_only_raises_the_estimate(tmp_path):
    signal, taps = 10 << 20, 20
    model = workers.estimate(tmp_path, signal, taps)
    workers.record(tmp_path, signal, taps, [workers.BASE_BYTES + 4 * signal * taps])
    measured = workers.estimate(tmp_path, signal, taps)
    workers.record(tmp_path, signal, taps, [workers.BASE_BYTES + signal * taps])

    assert model == workers.BASE_BYTES + workers.TAP_SIGNALS * signal * taps
    assert measured == workers.estimate(tmp_path, signal, taps) == workers.BASE_BYTES + 4 * signal * taps


def test_a_small_songs_peak_keeps_the_estimate_scaling_with_length(tmp_path, monkeypatch):
    monkeypatch.delenv(workers.ENV)
    signal, taps = round(90 * 44100) * 16, 40
    before = workers.estimate(tmp_path, signal, taps)
    workers.record(tmp_path, 44100 * 16, 1, [172 << 20])

    assert workers.estimate(tmp_path, signal, taps) == before
    assert workers.count(20, before, free=0.8, total=16 * GB, cpus=8) == 2


# Song 000's 90 s cut: 26 tapped signals of 63.8 MB. Its worker peaked at 4.41 GB with every tap held; synthetic
# songs project 1.6 to 1.8 GB with taps on disk.
SONG_SIGNAL, SONG_TAPS = round(90.457 * 44100) * 16, 26


@pytest.mark.parametrize("spill, free, expected", [
    (None, 1.0, 2), (None, 0.87, 2), (None, 0.7, 1),
    ("1", 1.0, 5), ("1", 0.87, 4), ("1", 0.7, 3), ("1", 0.65, 2), ("1", 0.4, 1),
], ids=lambda value: f"{value}")
def test_a_90_s_song_on_16_gb_gets_workers_that_keep_their_taps_unless_they_may_spill(tmp_path, monkeypatch, spill, free,
                                                                                      expected):
    monkeypatch.delenv(workers.ENV)
    if spill is None:
        monkeypatch.delenv(workers.SPILL_ENV, raising=False)
    else:
        monkeypatch.setenv(workers.SPILL_ENV, spill)
    work = workers.estimate(tmp_path, SONG_SIGNAL, SONG_TAPS)
    keep = workers.hold(SONG_SIGNAL, SONG_TAPS)

    assert work > 1.8e9
    assert keep == (0 if spill else 1.5 * SONG_SIGNAL * SONG_TAPS)
    assert workers.count(20, work + keep, free=free, total=16 * GB, cpus=8) == expected


def test_a_peak_measured_with_every_tap_held_is_not_read(tmp_path):
    (tmp_path / workers.PEAKS).write_text(json.dumps({"per_tapped_signal": 2.28, "peak_bytes": 4414734336}))

    assert workers.estimate(tmp_path, SONG_SIGNAL, SONG_TAPS) == workers.BASE_BYTES + workers.TAP_SIGNALS * SONG_SIGNAL * SONG_TAPS


def test_a_peak_that_cant_be_written_never_fails_the_check(tmp_path):
    (tmp_path / workers.PEAKS).mkdir()

    workers.record(tmp_path, 10 << 20, 20, [1 << 30])


def one_bass(song):
    (song / "stems").mkdir(parents=True)
    soundfile.write(song / "stems" / "Bass.wav", noise(1, 11), RATE, subtype="FLOAT")
    return {"stems": {"Bass.wav": {}}, "sources": {"bass": {"chain": "bass", "stems": ["Bass.wav"]}},
            "steps": [step("hpf", "Bass.wav", "HighpassFilter", cutoff_frequency_hz=40)],
            "moves": [{"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["hpf"]},
                      {"move": "bass.mud-cut", "source": "bass", "decision": "dropped"}]}


class NoSubmit:
    def __init__(self, *args, **kwargs):
        pass

    def submit(self, *args, **kwargs):
        raise OSError(11, "Resource temporarily unavailable")

    def kill_workers(self):
        pass

    def shutdown(self, **kwargs):
        pass


def no_pool(*args, **kwargs):
    raise OSError(24, "Too many open files")


def read_only_peaks(cache):
    cache.mkdir(parents=True)
    (cache / workers.PEAKS).write_text(json.dumps({workers.FACTOR: 8, "peak_bytes": 400 << 20}))
    (cache / workers.PEAKS).chmod(0o444)


@pytest.mark.parametrize("break_it", ["constructor", "submit", "record"])
def test_a_broken_pool_or_peak_file_leaves_the_check_to_one_process(tmp_path, monkeypatch, break_it):
    one, two = tmp_path / "one", tmp_path / "two"
    recipe = one_bass(one)
    shutil.copytree(one / "stems", two / "stems")
    alone = check(one, recipe, False, 1, monkeypatch, tmp_path / "memo-one")
    monkeypatch.setattr(workers, "free_fraction", lambda: 0.9)
    if break_it == "constructor":
        monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", no_pool)
    elif break_it == "submit":
        monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", NoSubmit)
    else:
        read_only_peaks(two / "renders" / "check")

    assert check(two, recipe, False, 2, monkeypatch, tmp_path / "memo-two") == alone


def test_workers_refuse_a_stem_changed_after_the_check_hashed_it(tmp_path, monkeypatch):
    song = tmp_path / "song"
    recipe = one_bass(song)
    alone = check(song, recipe, False, 1, monkeypatch, tmp_path / "memo-one")
    shutil.rmtree(song / "renders")
    stem = song / "stems" / "Bass.wav"
    original = stem.read_bytes()
    audio, rate = soundfile.read(stem)
    pool, record = workers.futures.ProcessPoolExecutor, workers.record

    def edit_before_workers(*args, **kwargs):
        soundfile.write(stem, audio * 0.25, rate, subtype="FLOAT")
        return pool(*args, **kwargs)

    def restore_after_workers(*args, **kwargs):
        stem.write_bytes(original)
        return record(*args, **kwargs)

    monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", edit_before_workers)
    monkeypatch.setattr(workers, "record", restore_after_workers)
    monkeypatch.setattr(workers, "free_fraction", lambda: 0.9)

    assert check(song, recipe, False, 2, monkeypatch, tmp_path / "memo-two") == alone


def test_cold_warm_and_partly_cached_checks_with_workers_match_one_process(tmp_path, monkeypatch):
    recipe = one_bass(tmp_path)
    monkeypatch.setattr(workers, "free_fraction", lambda: 0.9)
    alone = check(tmp_path, recipe, False, 1, monkeypatch, tmp_path / "memo-one")
    cache = tmp_path / "renders" / "check"
    shutil.rmtree(cache)

    assert check(tmp_path, recipe, False, 2, monkeypatch, tmp_path / "memo") == alone
    assert check(tmp_path, recipe, False, 2, monkeypatch, tmp_path / "memo") == alone
    for pattern in ("contract-*.json", "[0-9a-f]*.json"):
        next(cache.glob(pattern)).unlink()
        assert check(tmp_path, recipe, False, 2, monkeypatch, tmp_path / "memo") == alone


def test_a_workers_unused_budget_never_lowers_its_recorded_peak(tmp_path):
    # A worker allowed 1 GiB of taps that held none: its whole peak over the base is working set.
    signal, taps = 1 << 20, 100
    workers.record(tmp_path, signal, taps, [workers.BASE_BYTES + 400 * signal])

    assert workers.estimate(tmp_path, signal, taps) == workers.BASE_BYTES + 4 * signal * taps
