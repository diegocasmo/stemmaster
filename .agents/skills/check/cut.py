"""A short copy of a song to tune moves on: every recipe stem cut at the same raw sample range, so offsets keep their
alignment, with sections and Ride segments shifted into the window and clipped to it."""
import copy
import json
import shutil
import sys
from pathlib import Path

import soundfile

USAGE = ("usage: uv run .agents/skills/check/cut.py songs/<id>/recipe.json <first_section> [<last_section>]\n"
         "       uv run .agents/skills/check/cut.py songs/<id>/recipe.json <start_s> <end_s>")
AUDIO = {".wav", ".flac", ".aif", ".aiff"}
# The check reads a section in 3 s windows, so a shorter clipped part says nothing about it.
MIN_SECTION_S = 3.0


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


def clip_ride(settings, start, end, kept_sections):
    ramp = settings.get("ramp_ms", 10) / 1000
    segments = []
    for segment in settings["segments"]:
        if len(segment) == 2:
            if segment[0] in kept_sections:
                segments.append(segment)
            continue
        a, b = max(segment[0], start) - start, min(segment[1], end) - start
        if b - a >= ramp:
            segments.append([round(a, 6), round(b, 6), segment[2]])
    # A Ride needs a segment; one at 0 dB keeps the step, and the check reports it as doing nothing in this cut.
    return dict(settings, segments=segments or [[0, ramp, 0.0]])


def cut_recipe(recipe, start, end):
    out = copy.deepcopy(recipe)
    # A cut is never reviewed or mastered.
    out.pop("reviews", None)
    if "sections" in recipe:
        out["sections"] = {}
        for name, section in recipe["sections"].items():
            a, b = max(section["start_s"], start) - start, min(section["end_s"], end) - start
            if b - a >= MIN_SECTION_S:
                out["sections"][name] = dict(section, start_s=round(a, 6), end_s=round(b, 6))
    steps = out.get("steps", []) + [s for r in out.get("moves", []) for s in (r.get("trial") or {}).get("steps", [])]
    for step in steps:
        if step.get("plugin") == "Ride":
            step["settings"] = clip_ride(step["settings"], start, end, out.get("sections", {}))
    return out


def cut_file(source, target, start, end):
    info = soundfile.info(source)
    first, last = round(start * info.samplerate), min(round(end * info.samplerate), info.frames)
    audio, rate = soundfile.read(source, start=min(first, info.frames), stop=last, always_2d=True)
    soundfile.write(target, audio, rate, subtype=info.subtype)


def cut(recipe_path, args):
    recipe_path = Path(recipe_path)
    song = recipe_path.parent
    recipe = json.loads(recipe_path.read_text())
    start, end, name = window(recipe, args)
    infos = {stem: soundfile.info(song / "stems" / stem) for stem in recipe["stems"]}
    length = max(info.frames / info.samplerate + recipe["stems"][stem].get("offset_samples", 0) / info.samplerate
                 for stem, info in infos.items())
    end = min(end, length)
    if not 0 <= start < end:
        sys.exit(f"the window must satisfy 0 <= start < end <= {length:.3f} s")
    out = song / "renders" / "cuts" / name
    for folder in ("stems", "references"):
        shutil.rmtree(out / folder, ignore_errors=True)
    (out / "stems").mkdir(parents=True)
    # Only recipe stems: files the recipe excludes (DIs, vocals) are never read.
    for stem in recipe["stems"]:
        cut_file(song / "stems" / stem, out / "stems" / stem, start, end)
    references = sorted(p for p in (song / "references").glob("*") if p.suffix.lower() in AUDIO) \
        if (song / "references").is_dir() else []
    if references:
        (out / "references").mkdir()
    for path in references:
        cut_file(path, out / "references" / path.name, start, end)
    cut = cut_recipe(recipe, start, end)
    (out / "recipe.json").write_text(json.dumps(cut, indent=2) + "\n")
    print(f"window {start:g}-{end:g} s, sections {sorted(cut.get('sections', {}))}")
    return out / "recipe.json"


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(USAGE)
    print(cut(sys.argv[1], sys.argv[2:]))
