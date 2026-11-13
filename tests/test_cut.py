import json

import numpy as np
import soundfile

import cut
import render
from helpers import RATE, noise, write


def recipe():
    return {
        "sections": {"a": {"start_s": 0, "end_s": 4, "group": "verse"}, "b": {"start_s": 4, "end_s": 9, "group": "chorus"},
                     "c": {"start_s": 9, "end_s": 10, "group": "verse"}},
        "stems": {"kick.wav": {"gain_db": -2}, "bass.wav": {"offset_samples": 480, "pan": 0.3}},
        "steps": [{"id": "ride", "target": "bass.wav", "plugin": "Ride",
                   "settings": {"segments": [[1, 3, -2], [3.4, 4, 2], ["b", 1]]}},
                  {"id": "lift", "target": "mix", "plugin": "Gain", "settings": {"gain_db": 1.5}}],
        "moves": [{"move": "bass.rides", "source": "bass", "decision": "dropped",
                   "trial": {"steps": [{"id": "t", "target": "bass.wav", "plugin": "Ride", "settings": {"segments": [[0.5, 2, 3]]}}]}}],
        "reviews": [{"stage": "mix", "fingerprint": "fp"}],
    }


def test_a_cut_keeps_alignment_and_clips_sections_and_rides(song):
    write(song, "kick.wav", noise(10))
    write(song, "bass.wav", noise(10, seed=1))
    write(song, "bass DI.wav", noise(10, seed=2))
    (song / "recipe.json").write_text(json.dumps(recipe()))

    path = cut.cut(song / "recipe.json", ["3", "9"])

    out = json.loads(path.read_text())
    assert path.parent == song / "renders" / "cuts" / "3-9"
    assert sorted(p.name for p in (path.parent / "stems").iterdir()) == ["bass.wav", "kick.wav"]
    assert out["sections"] == {"b": {"start_s": 1, "end_s": 6, "group": "chorus"}}
    assert out["steps"][0]["settings"]["segments"] == [[0.4, 1, 2], ["b", 1]]
    assert out["moves"][0]["trial"]["steps"][0]["settings"]["segments"] == [[0, 0.01, 0.0]]
    assert "reviews" not in out
    # Stateless steps render the window as the whole song does, from the offset's lead-in to the window's end.
    whole, part = render.Result(recipe(), song).mix, render.Result(out, path.parent).mix
    skip = 480 + RATE // 100
    assert part.shape == (6 * RATE + 480, 2)
    np.testing.assert_array_equal(part[skip : 6 * RATE], whole[3 * RATE + skip : 9 * RATE])


def test_a_cut_by_sections_spans_them(song):
    write(song, "kick.wav", noise(10))
    write(song, "bass.wav", noise(10, seed=1))
    (song / "recipe.json").write_text(json.dumps(recipe()))

    path = cut.cut(song / "recipe.json", ["a", "b"])

    assert path.parent.name == "a-b"
    assert soundfile.info(path.parent / "stems" / "kick.wav").frames == 9 * RATE
