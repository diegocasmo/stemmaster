"""A short copy of a song to tune moves on: every recipe stem cut from the same raw sample, so offsets keep their
alignment, with sections and Ride segments shifted into the window and clipped to it."""
import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
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


def clip_ride(label, settings, sections, first, last, rate):
    ramp_ms = settings.get("ramp_ms", native.DEFAULT_RAMP_MS)
    half, start = ramp_ms / 2000 * rate, first / rate
    segments = []
    for segment in settings["segments"]:
        # Named segments become seconds, so dropping a short section never drops the gain it carries.
        a, b, gain = native.segment_times(segment, sections)
        # The samples native.ride gives a nonzero weight: its ramps are centred on each edge.
        rises = a * rate - half < last - 1 if half else a * rate <= last - 1
        if not rises or b * rate + half <= first:
            continue
        if b <= start:
            print(f"{label}: segment {segment} ends at {b:g} s, before the cut starts at {start:g} s, so the cut drops "
                  f"it and its fall into the cut's first {b + ramp_ms / 2000 - start:g} s", file=sys.stderr)
            continue
        if a < start and (b - start) * 1000 < ramp_ms:
            print(f"{label}: segment {segment} starts before the cut, and the {(b - start) * 1000:g} ms left in it are "
                  f"under its ramp_ms {ramp_ms:g}, so the cut drops it and its first {b + ramp_ms / 2000 - start:g} s "
                  f"differ from the whole song", file=sys.stderr)
            continue
        # Only a start before the cut is clipped; a segment may run past the audio, so the cut's end ramps as the song's.
        # Exact shifted times, unrounded: rounding would move a ramp by a sample and part back-to-back segments.
        segments.append([max(a, start) - start, b - start, gain])
    # A Ride needs a segment; one at 0 dB keeps the step, and the check reports it as doing nothing in this cut.
    return dict(settings, segments=segments or [[0, (math.ceil(ramp_ms) + 1) / 1000, 0.0]])


def cut_recipe(recipe, first, last, rate):
    out = copy.deepcopy(recipe)
    # A cut is never reviewed or mastered.
    out.pop("reviews", None)
    sections = recipe.get("sections", {})
    steps = out.get("steps", []) + [s for r in out.get("moves", []) for s in (r.get("trial") or {}).get("steps", [])]
    for step in steps:
        if step.get("plugin") == "Ride":
            step["settings"] = clip_ride(f"step {step.get('id')}", step["settings"], sections, first, last, rate)
    if "sections" in recipe:
        start, seconds = first / rate, (last - first) / rate
        out["sections"] = {}
        for name, section in sections.items():
            a, b = max(section["start_s"], start) - start, min(section["end_s"] - start, seconds)
            if b - a >= MIN_SECTION_S:
                out["sections"][name] = dict(section, start_s=round(a, 6), end_s=min(round(b, 6), seconds))
    return out


def replace(target, write):
    """Writes a new file and moves it over the target, so a hard link or other entry already there is never written through."""
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    os.close(handle)
    try:
        write(temporary)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def cut_file(source, target, first, last):
    info = soundfile.info(source)
    audio, rate = soundfile.read(source, start=min(first, info.frames), stop=min(last, info.frames), always_2d=True)
    # The source's own format: a stem name without an extension says nothing about it.
    replace(target, lambda path: soundfile.write(path, audio, rate, subtype=info.subtype, format=info.format))


def cut(recipe_path, args):
    recipe_path = Path(recipe_path)
    song = recipe_path.parent
    recipe = json.loads(recipe_path.read_text())
    start, end, name = window(recipe, args)
    if not SAFE_NAME.fullmatch(name):
        sys.exit(f"{name}: a cut's name must use only letters, digits and . _ + -; give start and end seconds instead")
    out = song / "renders" / "cuts" / name
    home = (song / "renders" / "cuts").resolve() / name
    references = sorted(p for p in (song / "references").glob("*") if p.suffix.lower() in AUDIO) \
        if (song / "references").is_dir() else []
    writes = [(recipe_path, "", "recipe.json")] + [(song / "stems" / stem, "stems", stem) for stem in recipe["stems"]]
    writes += [(path, "references", path.name) for path in references]
    # Every file the cut deletes or writes must be its own: a stem key like "/x", "../x" or "a/b", or a symlink,
    # would otherwise point a write at the song's originals.
    for source, folder, file in writes:
        target, expected = out / folder / file, home / folder / file
        if Path(file).name != file or file == ".." or target.resolve() != expected:
            sys.exit(f"{folder}/{file} would be written outside {home}, so nothing was written")
        if target.resolve() == source.resolve():
            sys.exit(f"{target} is its own source {source}, so nothing was written")
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
    try:
        render.check(recipe, rate, length + round(recipe.get("tail_seconds", 0) * rate))
    except SystemExit as error:
        sys.exit(f"the song's recipe does not render, so it has no cut: {error}")
    part = cut_recipe(recipe, first, last, rate)
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
    if references:
        (out / "references").mkdir()
    for path in references:
        own = soundfile.info(path).samplerate
        cut_file(path, out / "references" / path.name, round(first / rate * own), round(last / rate * own))
    replace(out / "recipe.json", lambda path: Path(path).write_text(json.dumps(part, indent=2) + "\n"))
    print(f"window {first / rate:g}-{last / rate:g} s, sections {sorted(part.get('sections', {}))}")
    return out / "recipe.json"


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(USAGE)
    print(cut(sys.argv[1], sys.argv[2:]))
