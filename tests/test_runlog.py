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
