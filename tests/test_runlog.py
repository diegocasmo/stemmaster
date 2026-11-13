import json
import subprocess
import sys
from pathlib import Path

import pytest

import runlog
from helpers import noise, write

SKILLS = Path(__file__).resolve().parents[1] / ".agents" / "skills"


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def run(script, *args):
    return subprocess.run([sys.executable, str(SKILLS / script), *map(str, args)], capture_output=True, text=True)


def test_each_render_and_check_run_logs_one_line(song):
    write(song, "kick.wav", noise(1))
    (song / "recipe.json").write_text(json.dumps({"stems": {"kick.wav": {}}}))

    render, check = run("render/render.py", song / "recipe.json", 0, 0.5), run("check/check.py", "--no-report", song / "recipe.json")

    logged = lines(song / "renders" / "timings.jsonl")
    assert (render.returncode, check.returncode) == (0, 1)
    assert [(line["tool"], line["exit"], line["scope"]) for line in logged] == [
        ("render", 0, {"master": False, "taps": [], "clip": ["0", "0.5"]}),
        ("check", 1, {"flags": ["--no-report"], "moves": 0})]
    assert logged[1]["counts"] == {"contracts": None, "contracts_cold": 0, "batteries_cold": 0, "variant_renders": 0}
    assert all(line["wall_s"] >= 0 and line["peak_rss_mb"] > 0 and line["start"] <= line["end"] for line in logged)


def test_a_cut_logs_into_its_song(tmp_path):
    cut = tmp_path / "renders" / "cuts" / "b-c"
    (cut / "stems").mkdir(parents=True)

    with pytest.raises(SystemExit), runlog.logged("check", cut / "recipe.json", lambda: {}):
        sys.exit(2)

    assert [(line["cut"], line["exit"]) for line in lines(tmp_path / "renders" / "timings.jsonl")] == [("b-c", 2)]


def prewarmed(tmp_path, monkeypatch, moves, free=lambda: 0.9):
    """Runs the evidence of a one-bass song with one worker process, which keeps the test small."""
    import evidence
    import structure
    import workers

    song = tmp_path / "song"
    (song / "stems").mkdir(parents=True)
    write(song, "Bass.wav", noise(1))
    recipe = {"stems": {"Bass.wav": {}}, "sources": {"bass": {"chain": "bass", "stems": ["Bass.wav"]}},
              "steps": [{"id": "hpf", "target": "Bass.wav", "plugin": "HighpassFilter", "settings": {"cutoff_frequency_hz": 40}}],
              "moves": moves}
    (song / "recipe.json").write_text(json.dumps(recipe))
    monkeypatch.setenv(workers.ENV, "2")
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(tmp_path / "memo"))
    monkeypatch.setattr(workers, "free_fraction", free)
    monkeypatch.setattr(evidence, "RENDERS", [0])
    monkeypatch.setattr(workers, "RENDERS", [0])
    real = workers.futures.ProcessPoolExecutor
    monkeypatch.setattr(workers.futures, "ProcessPoolExecutor", lambda *a, **k: real(1, **k))
    evidence.report(recipe, song, structure.nodes(recipe, structure.Routing(recipe), []))
    return song


def test_a_check_counts_the_renders_its_workers_did(tmp_path, monkeypatch):
    import check
    import evidence
    import workers

    song = prewarmed(tmp_path, monkeypatch, [{"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["hpf"]},
                                             {"move": "bass.mud-cut", "source": "bass", "decision": "dropped"}])

    assert workers.RENDERS[0] > 0
    assert check.details(song / "recipe.json", [], set())["counts"]["variant_renders"] == evidence.RENDERS[0] + workers.RENDERS[0]


def test_a_failed_worker_part_still_counts_its_renders(tmp_path, monkeypatch):
    import workers

    bad = {"id": "bad", "target": "Bass.wav", "plugin": "Compressor", "settings": {"bogus": 1}}
    with pytest.raises(TypeError):
        prewarmed(tmp_path, monkeypatch, [{"move": "bass.mud-cut", "source": "bass", "decision": "dropped", "trial": {"steps": [bad]}},
                                          {"move": "bass.hpf", "source": "bass", "decision": "dropped"}])

    assert workers.RENDERS[0] > 0


def test_a_memory_stop_still_counts_the_parts_that_finished(tmp_path, monkeypatch):
    import workers

    real, submitted, samples = workers.futures.wait, [], []

    def wait(running, **kwargs):
        submitted.extend(running)
        return real(running, timeout=0)

    def free():
        # The second sample comes after the first part finished but before the check read its result.
        samples.append(None)
        for future in submitted:
            future.result(timeout=60)
        return 0.9 if len(samples) == 1 else 0.1

    monkeypatch.setattr(workers.futures, "wait", wait)
    prewarmed(tmp_path, monkeypatch, [{"move": "bass.hpf", "source": "bass", "decision": "kept", "steps": ["hpf"]},
                                      {"move": "bass.mud-cut", "source": "bass", "decision": "dropped"}], free)

    assert submitted and workers.RENDERS[0] > 0


@pytest.mark.parametrize("moves", [None, 1])
def test_malformed_moves_still_log_the_run(tmp_path, moves):
    import check

    (tmp_path / "stems").mkdir()
    (tmp_path / "recipe.json").write_text(json.dumps({"stems": {}, "moves": moves}))

    with pytest.raises(TypeError), runlog.logged("check", tmp_path / "recipe.json",
                                                 lambda: check.details(tmp_path / "recipe.json", [], set())):
        raise TypeError("the check's own failure")

    assert [(line["scope"]["moves"], line["exit"]) for line in lines(tmp_path / "renders" / "timings.jsonl")] == [(None, 1)]


def test_a_cut_linking_the_songs_recipe_logs_as_the_cut(tmp_path):
    (tmp_path / "recipe.json").write_text("{}")
    cut = tmp_path / "renders" / "cuts" / "phrase"
    (cut / "stems").mkdir(parents=True)
    (cut / "recipe.json").symlink_to(tmp_path / "recipe.json")

    assert runlog.log_path(cut / "recipe.json") == (tmp_path.resolve() / "renders" / "timings.jsonl", "phrase")
