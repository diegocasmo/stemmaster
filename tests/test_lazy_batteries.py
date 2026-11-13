import hashlib
from collections import Counter

import numpy as np
import pytest
import soundfile

import contracts
import evidence
import render
import structure

SECONDS = 2
# The committed profile is measured at 44.1 kHz, and the trial's range is read against it.
RATE = 44100


def noise(seconds, seed):
    return np.random.default_rng(seed).standard_normal(round(seconds * RATE)) * 0.1


def write(song, name, audio):
    soundfile.write(song / "stems" / name, audio, RATE, subtype="FLOAT")


def hits(seconds, every, seed):
    audio = noise(seconds, seed) * 0.05
    for start in range(0, len(audio), round(every * RATE)):
        audio[start : start + 2000] += np.exp(-np.arange(min(2000, len(audio) - start)) / 300)
    return audio


@pytest.fixture(autouse=True)
def built_in_default_trial(monkeypatch):
    monkeypatch.setitem(contracts.DEFAULT_TRIALS, "kick.saturate", {"core": {"plugin": "Distortion", "settings": {"drive_db": 18}}})


def song_with(song, master_moves):
    """Kick, snare and bass into a drums bus and the mix, with a trial master and kept, dropped and trialled moves."""
    write(song, "Kick.wav", hits(SECONDS, 0.5, 1))
    write(song, "Snare.wav", hits(SECONDS, 0.75, 2))
    write(song, "Bass.wav", noise(SECONDS, 3))
    recipe = {
        "profile": "instrumental-prog-metal",
        "stems": {"Kick.wav": {"bus": "drums"}, "Snare.wav": {"bus": "drums"}, "Bass.wav": {}},
        "buses": {"drums": {}},
        "sources": {"kick": {"chain": "kick", "stems": ["Kick.wav"]}, "snare": {"chain": "snare", "stems": ["Snare.wav"]},
                    "bass": {"chain": "bass", "stems": ["Bass.wav"]}},
        "steps": [
            {"id": "kick-comp", "target": "Kick.wav", "plugin": "Compressor", "settings": {"threshold_db": -20, "ratio": 4}},
            {"id": "bass-hpf", "target": "Bass.wav", "plugin": "HighpassFilter", "settings": {"cutoff_frequency_hz": 60}},
            {"id": "drums-comp", "target": "drums", "plugin": "Compressor", "settings": {"threshold_db": -18, "ratio": 2}},
            {"id": "master-drive", "target": "mix", "plugin": "Gain", "stage": "master", "settings": {"gain_db": 0}},
            {"id": "master-limit", "target": "mix", "plugin": "Limiter", "stage": "master", "settings": {"threshold_db": -3}},
        ],
        "moves": [
            {"move": "kick.compress", "source": "kick", "decision": "kept", "steps": ["kick-comp"]},
            {"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["bass-hpf"]},
            {"move": "drum-bus.compress", "source": "drum-bus", "decision": "kept", "steps": ["drums-comp"]},
            {"move": "snare.compress", "source": "snare", "decision": "dropped", "trial": {"steps": [
                {"id": "snare-comp", "target": "Snare.wav", "plugin": "Compressor", "settings": {"threshold_db": -24, "ratio": 6}}]}},
            {"move": "bass.limit", "source": "bass", "decision": "dropped", "trial": {"steps": [
                {"id": "bass-limit", "target": "Bass.wav", "plugin": "Limiter", "settings": {"threshold_db": -12}}]}},
            {"move": "kick.hpf", "source": "kick", "decision": "dropped"},
            # A default trial: its Gain is matched to equal loudness before it is judged.
            {"move": "kick.saturate", "source": "kick", "decision": "dropped"},
        ],
    }
    if master_moves:
        recipe["moves"] += [
            {"move": "mix-bus.trial-master", "source": "mix-bus", "decision": "kept", "steps": ["master-drive", "master-limit"]},
            {"move": "master.compress", "source": "master", "decision": "dropped", "trial": {"steps": [
                {"id": "master-comp", "target": "mix", "plugin": "Compressor", "stage": "master", "before": "master-limit",
                 "settings": {"threshold_db": -20, "ratio": 3}}]}},
        ]
    return recipe


def run(recipe, song, variants_class, cold=True):
    """Runs the whole check and counts its renders, per-tap observations and master passes."""
    counts = Counter()

    def counted(name, inner):
        return lambda *a, **k: counts.update([name]) or inner(*a, **k)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evidence, "observe", counted("observe", evidence.observe))
        patch.setattr(render.Result, "__init__", counted("render", render.Result.__init__))
        patch.setattr(render.Result, "master", counted("master", render.Result.master))
        patch.setattr(evidence, "Variants", variants_class)
        numbers = report(recipe, song, cold)
    return numbers, counts


def report(recipe, song, cold):
    cache = song / "renders" / "check"
    if cold and cache.exists():
        for path in cache.iterdir():
            path.unlink()
    nodes = structure.nodes(recipe, structure.Routing(recipe), [])
    evidence.report(recipe, song, nodes, master_mode=True)
    return (cache / "report.json").read_text()


# Each digest is of the report.json main's eager check wrote for the same fixture.
@pytest.mark.parametrize("master_moves, observed, passes, digest", [
    (False, 16, 10, "0f50b288ba425ceeb54e184236a6b84e1a7727e5b9671571a5866c8fde19de47"),
    (True, 20, 22, "e7029298b3da4aa95534bc2c2ec613e2178b2686c5153ca84b081498ffda10cc"),
], ids=["mix-moves", "master-moves"])
def test_only_published_batteries_and_read_masters_are_measured(song, master_moves, observed, passes, digest):
    """main measured every rendered variant's battery: 32 and 40 observations, 17 and 29 master passes."""
    text, counts = run(song_with(song, master_moves), song, evidence.Variants)

    assert hashlib.sha256(text.encode()).hexdigest() == digest
    assert dict(counts) == {"render": 8 + 2 * master_moves, "observe": observed, "master": passes}


def test_every_published_battery_is_cached(song):
    recipe = song_with(song, True)
    run(recipe, song, evidence.Variants)

    assert run(recipe, song, evidence.Variants, cold=False)[1]["render"] == 0


def bad_master(song):
    recipe = song_with(song, False)
    recipe["moves"].append({"move": "mix-bus.rides", "source": "mix-bus", "decision": "dropped", "trial": {"steps": [
        {"id": "ride", "target": "mix", "plugin": "Ride", "settings": {"segments": [[0, 1, -12]], "ramp_ms": 0}},
        {"id": "bad", "target": "mix", "plugin": "Gain", "stage": "master", "before": "master-limit", "settings": {"bogus": 1}}]}})
    return recipe


def lost_tap(song):
    """A trial that reroutes both kick mics off the implicit group bus the kick is observed at."""
    for name in ("Kick1.wav", "Kick2.wav"):
        write(song, name, hits(SECONDS, 0.5, 1))
    return {
        "stems": {"Kick1.wav": {"bus": "group"}, "Kick2.wav": {"bus": "group"}},
        "sources": {"kick": {"chain": "kick", "stems": ["Kick1.wav", "Kick2.wav"]}},
        "moves": [{"move": "kick.gate", "source": "kick", "decision": "dropped", "trial": {
            "buses": {"trial-group": {}}, "routing": {"Kick1.wav": "trial-group", "Kick2.wav": "trial-group"},
            "steps": [{"id": "trial-gate", "target": "trial-group", "plugin": "NoiseGate", "settings": {"threshold_db": -30, "ratio": 4}}]}}],
    }


@pytest.mark.parametrize("recipe, error", [(bad_master, TypeError), (lost_tap, KeyError)], ids=["bad-master-setting", "lost-tap"])
def test_an_unmeasured_trial_still_fails_as_a_measured_one(song, recipe, error):
    with pytest.raises(error):
        run(recipe(song), song, evidence.Variants)
