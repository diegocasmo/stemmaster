"""A short copy of a song to tune moves on: every recipe stem cut from the same raw sample, so offsets keep their
alignment, with sections and Ride segments shifted into the window and clipped to it."""
import copy
import json
import math
import re
import shutil
import sys
from pathlib import Path

import soundfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".agents" / "skills" / "render"))
import native
import render

USAGE = ("usage: uv run .agents/skills/check/cut.py songs/<id>/recipe.json <first_section> [<last_section>]\n"
         "       uv run .agents/skills/check/cut.py songs/<id>/recipe.json <start_s> <end_s>")
AUDIO = {".wav", ".flac", ".aif", ".aiff"}
# The check reads a section in 3 s windows, so a shorter clipped part says nothing about it.
MIN_SECTION_S = 3.0
# The cut's name is a folder that gets emptied, so it must stay one plain path part inside renders/cuts.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def window(recipe, args):
    sections = recipe.get("sections", {})
    if all(arg in sections for arg in args) and len(args) in (1, 2):
        first, last = sections[args[0]], sections[args[-1]]
        return first["start_s"], last["end_s"], "-".join(dict.fromkeys(args))
    try:
        start, end = (float(arg) for arg in args)
    except ValueError:
        sys.exit(f"{' '.join(args)}: name one or two sections of {sorted(sections)}, or give start and end seconds\n{USAGE}")
    return start, end, f"{start:g}-{end:g}"


def clip_ride(label, settings, sections, start, seconds):
    ramp_ms = settings.get("ramp_ms", native.DEFAULT_RAMP_MS)
    segments = []
    for segment in settings["segments"]:
        # Named segments become seconds, so dropping a short section never drops the gain it carries.
        a, b, gain = native.segment_times(segment, sections)
        if b <= start or a >= start + seconds:
            continue
        # Only the start is clipped: a segment may end past the audio, so the window's end ramps as in the whole song.
        a, b = round(max(a, start) - start, 6), round(b - start, 6)
        if b <= a or (b - a) * 1000 < ramp_ms:
            print(f"{label}: segment {segment} ends within its ramp of the cut's start, so the cut drops it and its "
                  f"first {b + ramp_ms / 2000:g} s differ from the whole song", file=sys.stderr)
            continue
        segments.append([a, b, gain])
    # A Ride needs a segment; one at 0 dB keeps the step, and the check reports it as doing nothing in this cut.
    return dict(settings, segments=segments or [[0, (math.ceil(ramp_ms) + 1) / 1000, 0.0]])


def cut_recipe(recipe, start, seconds):
    out = copy.deepcopy(recipe)
    # A cut is never reviewed or mastered.
    out.pop("reviews", None)
    sections = recipe.get("sections", {})
    steps = out.get("steps", []) + [s for r in out.get("moves", []) for s in (r.get("trial") or {}).get("steps", [])]
    for step in steps:
        if step.get("plugin") == "Ride":
            step["settings"] = clip_ride(f"step {step.get('id')}", step["settings"], sections, start, seconds)
    if "sections" in recipe:
        out["sections"] = {}
        for name, section in sections.items():
            a, b = max(section["start_s"], start) - start, min(section["end_s"] - start, seconds)
            if b - a >= MIN_SECTION_S:
                out["sections"][name] = dict(section, start_s=round(a, 6), end_s=min(round(b, 6), seconds))
    return out


def cut_file(source, target, first, last):
    info = soundfile.info(source)
    audio, rate = soundfile.read(source, start=min(first, info.frames), stop=min(last, info.frames), always_2d=True)
    soundfile.write(target, audio, rate, subtype=info.subtype)


def cut(recipe_path, args):
    recipe_path = Path(recipe_path)
    song = recipe_path.parent
    recipe = json.loads(recipe_path.read_text())
    start, end, name = window(recipe, args)
    if not SAFE_NAME.fullmatch(name):
        sys.exit(f"{name}: a cut's name must use only letters, digits and . _ + -; give start and end seconds instead")
    cuts = song / "renders" / "cuts"
    out = cuts / name
    if out.resolve().parent != cuts.resolve():
        sys.exit(f"{out} resolves outside {cuts}")
    infos = {stem: soundfile.info(song / "stems" / stem) for stem in recipe["stems"]}
    rates = {info.samplerate for info in infos.values()}
    if len(rates) != 1:
        sys.exit(f"stems must share one sample rate, got {sorted(rates)}")
    rate = rates.pop()
    offsets = {stem: recipe["stems"][stem].get("offset_samples", 0) for stem in infos}
    length = max(info.frames + offsets[stem] for stem, info in infos.items())
    limit = f"the window must satisfy 0 <= start < end <= {length / rate:.3f} s"
    if not 0 <= start < end:
        sys.exit(limit)
    first, last = round(start * rate), min(round(min(end, length / rate) * rate), length)
    if not first < last:
        sys.exit(limit)
    # A stem placed early needs samples past the window, or the cut's timeline ends before the window does.
    spans = {stem: (first, last - min(offsets[stem], 0)) for stem in infos}
    frames = {stem: max(0, min(b, infos[stem].frames) - min(a, infos[stem].frames)) for stem, (a, b) in spans.items()}
    timeline = max(frames[stem] + offsets[stem] for stem in infos) + round(recipe.get("tail_seconds", 0) * rate)
    part = cut_recipe(recipe, first / rate, (last - first) / rate)
    try:
        render.check(part, rate, timeline)
    except SystemExit as error:
        sys.exit(f"the cut {name} would not render, so nothing was written: {error}")
    for folder in ("stems", "references"):
        shutil.rmtree(out / folder, ignore_errors=True)
    (out / "stems").mkdir(parents=True)
    # Only recipe stems: files the recipe excludes (DIs, vocals) are never read.
    for stem, (a, b) in spans.items():
        cut_file(song / "stems" / stem, out / "stems" / stem, a, b)
    references = sorted(p for p in (song / "references").glob("*") if p.suffix.lower() in AUDIO) \
        if (song / "references").is_dir() else []
    if references:
        (out / "references").mkdir()
    for path in references:
        own = soundfile.info(path).samplerate
        cut_file(path, out / "references" / path.name, round(first / rate * own), round(last / rate * own))
    (out / "recipe.json").write_text(json.dumps(part, indent=2) + "\n")
    print(f"window {first / rate:g}-{last / rate:g} s, sections {sorted(part.get('sections', {}))}")
    return out / "recipe.json"


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(USAGE)
    print(cut(sys.argv[1], sys.argv[2:]))
