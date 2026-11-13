import json
import os

import pytest

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
    assert out["steps"][0]["settings"]["segments"] == [[3.4 - 3, 1, 2], [1, 6, 1]]
    assert out["moves"][0]["trial"]["steps"][0]["settings"]["segments"] == [[0, 0.011, 0.0]]
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


def ride(segments, **settings):
    return {"id": "r", "target": "mix", "plugin": "Ride", "settings": dict(settings, segments=segments)}


def test_a_cut_never_writes_outside_its_folder(song):
    write(song, "kick.wav", noise(10))
    (song / "references").mkdir()
    (song / "references" / "keep.wav").write_bytes(b"")
    (song / "renders" / "cuts").mkdir(parents=True)
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick.wav": {}}, "sections": {"../..": {"start_s": 0, "end_s": 4}}}))

    with pytest.raises(SystemExit, match="name must use only"):
        cut.cut(song / "recipe.json", ["../.."])

    assert (song / "stems" / "kick.wav").exists() and (song / "references" / "keep.wav").exists()


def test_a_cut_holds_its_sections_when_a_stem_is_placed_early(song):
    write(song, "kick.wav", noise(10))
    recipe = {"stems": {"kick.wav": {"offset_samples": -480}}, "sections": {"verse": {"start_s": 2, "end_s": 6}}}
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", ["verse"])

    whole, part = render.Result(recipe, song).mix, render.Result(json.loads(path.read_text()), path.parent).mix
    np.testing.assert_array_equal(part, whole[2 * RATE : 6 * RATE])


def test_a_cut_keeps_the_gain_of_a_ride_on_a_short_section(song):
    write(song, "kick.wav", noise(10))
    recipe = {"stems": {"kick.wav": {}}, "sections": {"short": {"start_s": 4, "end_s": 6}}, "steps": [ride([["short", 6]])]}
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", ["1", "9"])

    out = json.loads(path.read_text())
    assert out["sections"] == {} and out["steps"][0]["settings"]["segments"] == [[3, 5, 6]]
    np.testing.assert_allclose(render.Result(out, path.parent).mix, render.Result(recipe, song).mix[RATE : 9 * RATE])


@pytest.mark.parametrize("segments, settings, window", [
    ([[1, 5, -2], [6, 7, 1]], {"ramp_ms": 0}, ["5", "8"]),
    ([[1, 3, -2]], {"ramp_ms": 0}, ["5", "8"]),
    ([["verse", -3]], {"ramp_ms": 8000}, ["6", "10"]),
])
def test_a_cut_of_a_ride_renders(song, segments, settings, window):
    write(song, "kick.wav", noise(10))
    recipe = {"stems": {"kick.wav": {}}, "sections": {"verse": {"start_s": 0, "end_s": 10}}, "steps": [ride(segments, **settings)]}
    render.check(recipe, *render.timeline(recipe, song))
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", window)

    render.Result(json.loads(path.read_text()), path.parent)


@pytest.mark.parametrize("key", ["absolute", "../stems/kick.wav", "sub/kick.wav", ".."])
def test_a_cut_never_writes_a_stem_outside_its_folder(song, key):
    write(song, "kick.wav", noise(10))
    key = str(song / "stems" / "kick.wav") if key == "absolute" else key
    (song / "recipe.json").write_text(json.dumps({"stems": {key: {}}}))

    with pytest.raises(SystemExit, match="outside"):
        cut.cut(song / "recipe.json", ["2", "6"])

    assert soundfile.info(song / "stems" / "kick.wav").frames == 10 * RATE and not (song / "renders").exists()


def test_a_cut_keeps_a_ride_whose_ramp_reaches_into_its_end(song):
    write(song, "kick.wav", noise(10))
    recipe = {"stems": {"kick.wav": {}}, "steps": [ride([[8, 10, 6]], ramp_ms=2000)]}
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", ["2", "8"])

    out = json.loads(path.read_text())
    assert out["steps"][0]["settings"]["segments"] == [[6, 8, 6]]
    np.testing.assert_allclose(render.Result(out, path.parent).mix, render.Result(recipe, song).mix[2 * RATE : 8 * RATE])


def test_a_cut_keeps_an_interior_ride_as_long_as_its_ramp(song):
    write(song, "kick.wav", noise(20))
    recipe = {"stems": {"kick.wav": {}}, "steps": [ride([[16, 17.1, 6]], ramp_ms=1100)]}
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", ["0.9", "20"])

    out = json.loads(path.read_text())
    whole = render.Result(recipe, song).mix
    np.testing.assert_allclose(render.Result(out, path.parent).mix, whole[round(0.9 * RATE) :], atol=1e-12)


def test_a_cut_says_which_ride_segment_it_drops_and_why(song, capsys):
    write(song, "kick.wav", noise(10))
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick.wav": {}}, "steps": [ride([[1, 3.5, -2]], ramp_ms=1000)]}))

    cut.cut(song / "recipe.json", ["3", "8"])

    assert "segment [1, 3.5, -2] starts before the cut, and the 500 ms left in it are under its ramp_ms 1000" in capsys.readouterr().err


def test_a_cut_replaces_a_hard_link_to_the_songs_recipe(song):
    write(song, "kick.wav", noise(10))
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick.wav": {}}, "sections": {"a": {"start_s": 2, "end_s": 6}}}))
    original = (song / "recipe.json").read_bytes()
    (song / "renders" / "cuts" / "2-6").mkdir(parents=True)
    os.link(song / "recipe.json", song / "renders" / "cuts" / "2-6" / "recipe.json")

    cut.cut(song / "recipe.json", ["2", "6"])

    assert (song / "recipe.json").read_bytes() == original


def test_a_cut_keeps_a_stems_format_without_an_extension(song):
    soundfile.write(song / "stems" / "kick", noise(10), RATE, subtype="FLOAT", format="WAV")
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick": {}}}))

    path = cut.cut(song / "recipe.json", ["2", "6"])

    assert soundfile.info(path.parent / "stems" / "kick").format == "WAV"
    render.Result(json.loads(path.read_text()), path.parent)


def test_a_cut_keeps_exact_ride_times(song):
    write(song, "kick.wav", noise(10))
    recipe = {"stems": {"kick.wav": {}}, "steps": [ride([[4.0000001, 6, 6], [6, 7, -3]], ramp_ms=0)]}
    (song / "recipe.json").write_text(json.dumps(recipe))

    path = cut.cut(song / "recipe.json", ["2", "8"])

    out = json.loads(path.read_text())
    (_, b, _), (a, _, _) = out["steps"][0]["settings"]["segments"]
    assert a == b
    np.testing.assert_allclose(render.Result(out, path.parent).mix, render.Result(recipe, song).mix[2 * RATE : 8 * RATE], atol=1e-12)


@pytest.mark.parametrize("segments, ramp_ms, window, error", [
    ([[26.7, 27.0, 3], [27.0, 28.0, 3]], 300, ["8.6", "30"], "the cut 8.6-30 would not render"),
    ([[20.2, 20.21, 6], [20.21, 20.23, -6]], 10, ["4.1", "25"], "the cut 4.1-25 would not render"),
    ([[4, 5, 6]], 2000, ["2", "8"], "the song's recipe does not render"),
])
def test_a_cut_whose_shifted_ride_would_not_render_writes_nothing(song, segments, ramp_ms, window, error):
    write(song, "kick.wav", noise(30))
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick.wav": {}}, "steps": [ride(segments, ramp_ms=ramp_ms)]}))

    with pytest.raises(SystemExit, match=error):
        cut.cut(song / "recipe.json", window)

    assert not (song / "renders").exists()
