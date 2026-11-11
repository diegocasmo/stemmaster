import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile

ROOT = Path(__file__).resolve().parents[3]
for skill in ("render", "analyze"):
    sys.path.insert(0, str(ROOT / ".agents" / "skills" / skill))
import render
from analyze import bands_db, measure as analyze_file, stereo
import hits as hit_tools
import measure

from structure import CLAIMED_FIELDS, DRUM_CHAINS, Routing, fields_of

ATTACK_HIT_MS = (0, 25)
SAME_HIT_MS = 30
OVERLAPS = [
    ("kick", "bass", [(0, 120), (60, 200)], "kick"),
    ("bass", "guitars", [(150, 500), (700, 3000)], None),
    ("synths", "guitars", [(200, 4000)], None),
]
MATCH_LU = 0.05
MATCH_ROUNDS = 12
CODE = [ROOT / ".agents" / "skills" / path for path in (
    "render/render.py", "render/native.py", "render/airwindows.py",
    "analyze/analyze.py", "analyze/hits.py", "analyze/measure.py", "check/evidence.py", "check/structure.py",
)]


def content(paths):
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.name).encode())
        with open(path, "rb") as file:
            while chunk := file.read(1 << 24):
                digest.update(chunk)
    return digest.hexdigest()


def inputs(song, recipe):
    """Everything outside the recipe that the numbers depend on."""
    plugins = sorted(f for f in (ROOT / "plugins").rglob("*") if f.is_file())
    profile = ROOT / "profiles" / str(recipe.get("profile")) / "profile.json"
    return [content(CODE), content(sorted((song / "stems").iterdir())), content(plugins),
            content([profile]) if profile.exists() else None]


def fingerprint(*parts):
    def plain(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        return value
    return hashlib.sha256(json.dumps(plain(list(parts)), sort_keys=True).encode()).hexdigest()


def off_variant(recipe, record):
    variant = copy.deepcopy(recipe)
    claimed = set(record.get("steps", []))
    variant["steps"] = [step for step in variant.get("steps", []) if step.get("id") not in claimed]
    for field in record.get("fields", []):
        kind, rest = field.split(".", 1)
        entries = variant["stems"] if kind == "stems" else variant.get("buses", {})
        if ".sends." in rest:
            name, bus = rest.split(".sends.")
            entries[name]["sends"] = [send for send in entries[name].get("sends", []) if send["bus"] != bus]
        else:
            name, key = rest.rsplit(".", 1)
            if key in CLAIMED_FIELDS:
                entries[name].pop(key, None)
    return variant


def timing(recipe, song, source, rate, length):
    """Hits found on a clean stem placed on the song's timeline, so no processing can hide one."""
    stems = source["stems"]
    stem = source.get("timing") or next((s for s in stems if "sample" in s.lower()), stems[0])
    audio, _ = soundfile.read(song / "stems" / stem, always_2d=True)
    placed = render.place(audio, recipe["stems"][stem].get("offset_samples", 0), length)
    return stem, hit_tools.detect((placed**2).sum(axis=1), rate)


def merged(time_lists, rate):
    times = np.sort(np.concatenate(time_lists)) if time_lists else np.array([], dtype=int)
    keep = np.concatenate([[True], np.diff(times) >= SAME_HIT_MS / 1000 * rate]) if len(times) else times.astype(bool)
    return times[keep]


def loudness(audio, rate):
    try:
        return pyloudnorm.Meter(rate).integrated_loudness(audio)
    except ValueError:
        return float("-inf")


def matched_master(result, target):
    """Finds the mix gain that brings the trial to `target` LUFS, bracketing it so a compressor can't stall the search."""
    def trial(gain_db):
        master_in, out = result.master(result.mix * 10 ** (gain_db / 20))
        return master_in, out, loudness(out, result.rate) - target

    tried = {0.0: trial(0.0)}
    low, high, gain = -np.inf, np.inf, 0.0
    for _ in range(MATCH_ROUNDS):
        error = tried[gain][2]
        if not np.isfinite(error) or abs(error) <= MATCH_LU:
            break
        low, high = (low, min(high, gain)) if error > 0 else (max(low, gain), high)
        gain = gain - error
        if not low < gain < high:
            gain = (low + high) / 2
        tried[gain] = trial(gain)
    best = min(tried, key=lambda g: abs(tried[g][2]) if np.isfinite(tried[g][2]) else np.inf)
    master_in, out, error = tried[best]
    return {"drive_change_db": round(best, 2), "residual_lu": measure.rounded(error),
            "matched": bool(np.isfinite(error) and abs(error) <= MATCH_LU)}, master_in, out


def observe(audio, rate, hits, sections):
    level = loudness(audio, rate)
    entry = {"integrated_lufs": measure.rounded(level), "bands_db": bands_db([audio], rate)["bands_db"],
             "side_mid_db": stereo([audio], rate)["side_mid_db"]}
    if hits is not None and len(hits):
        stats = hit_tools.measure(audio, rate, hits)
        entry["hits"] = hit_tools.summary(stats) | {
            "level_spread_db": measure.rounded(float(np.percentile(stats["level_db"], 90) - np.percentile(stats["level_db"], 10))),
            # Hit level against the source's own loudness compares peaks at equal loudness.
            "level_over_loudness_db": measure.spread(stats["level_db"] - level) if np.isfinite(level) else None,
        }
    if sections:
        entry["sections"] = measure.sections(audio, rate, sections)["sections"]
    return entry


def battery(recipe, result, points, times, chain_nodes, target):
    rate, out = result.rate, {}
    for label, (tap, hits) in points.items():
        out[label] = observe(result.tapped[tap], rate, hits, recipe.get("sections"))
    mix = analyze_file(result.mix, rate, loud=True)
    out["mix"] = {key: mix[key] for key in ("integrated_lufs", "bands_db", "side_mid_db", "side_mid_below_120hz_db", "correlation")}
    if recipe.get("sections"):
        out["mix"]["sections"] = measure.sections(result.mix, rate, recipe["sections"])["sections"]
    if len(times.get("kick", [])):
        out["mix"]["valley"] = measure.valley(result.mix, rate, times["kick"])
    for first, second, bands, on_hits in OVERLAPS:
        pair = [sum(result.tapped[node] for node in chain_nodes.get(chain, [])) for chain in (first, second)]
        if any(isinstance(audio, int) for audio in pair):
            continue
        out[f"overlap.{first}.{second}"] = measure.overlap(*pair, rate, bands)
        if on_hits and len(times.get(on_hits, [])):
            inside = measure.mask_from(len(pair[0]), times[on_hits], ATTACK_HIT_MS, rate)
            out[f"overlap.{first}.{second}.on_{on_hits}_hits"] = measure.overlap(*pair, rate, bands, inside)
    if not result.master_steps:
        out["trial"] = {"skipped": "no master steps"}
        return out
    if target is None:
        master_in, trial = result.master(result.mix)
        out["trial"] = {"drive_change_db": 0.0}
    else:
        out["trial"], master_in, trial = matched_master(result, target)
    out["trial"] |= {"integrated_lufs": measure.rounded(loudness(trial, rate)),
                     "true_peak_dbtp": measure.rounded(render.true_peak_dbtp(trial))}
    for chain in ("kick", "snare"):
        if len(times.get(chain, [])):
            out["trial"][f"dip_{chain}"] = measure.dip(master_in, trial, rate, times[chain])
    out["trial"]["range"] = measure.profile_range(trial, rate, recipe.get("profile"))
    return out


def plan(recipe, song, nodes):
    """Observation points (each source's node and every other bus a step acts on) and their frozen hits."""
    rate, length = render.timeline(recipe, song)
    sources = recipe.get("sources", {})
    times, timing_stems = {}, {}
    for name, source in sources.items():
        if source.get("chain") in DRUM_CHAINS and name in nodes:
            timing_stems[name], times[name] = timing(recipe, song, source, rate, length)
    routing = Routing(recipe)
    points = {f"source.{name}": (node, times.get(name)) for name, node in nodes.items()}
    for step in recipe.get("steps", []):
        target = step["target"]
        if target == "mix" or target in nodes.values() or f"bus.{target}" in points or target in recipe["stems"]:
            continue
        feeding = [name for name, source in sources.items() if routing.stems_into(target) & set(source["stems"])]
        points[f"bus.{target}"] = (target, merged([times[n] for n in feeding if n in times], rate))
    chain_times = {chain: merged([times[n] for n, s in sources.items() if s.get("chain") == chain and n in times], rate)
                   for chain in ("kick", "snare")}
    chain_nodes = {}
    for name, node in nodes.items():
        chain_nodes.setdefault(sources[name].get("chain"), []).append(node)
    return points, chain_times, chain_nodes, timing_stems, {name: len(found) for name, found in times.items()}


def evaluate(recipe, song, variant, cache, layout, target, boundary, outside):
    points, chain_times, chain_nodes = layout
    path = cache / f"{fingerprint(variant, target, boundary, outside, points, chain_times, chain_nodes)}.json"
    if path.exists():
        return json.loads(path.read_text())
    taps = sorted({tap for tap, _ in points.values()})
    # Only off variants may break render's rules, as removing a lone send leaves its return unfed.
    result = render.Result(variant, song, taps, strict=variant is recipe, boundary=boundary)
    numbers = battery(recipe, result, points, chain_times, chain_nodes, target)
    path.write_text(json.dumps(numbers))
    return numbers


def report(recipe, song, nodes):
    cache = song / "renders" / "check"
    cache.mkdir(parents=True, exist_ok=True)
    points, chain_times, chain_nodes, timing_stems, counts = plan(recipe, song, nodes)
    layout = (points, chain_times, chain_nodes)
    masters = [step for step in recipe.get("steps", []) if step.get("stage") == "master"]
    boundary = masters[-1].get("id") if masters else None
    outside = inputs(song, recipe)
    current = evaluate(recipe, song, recipe, cache, layout, None, boundary, outside)
    target = current["trial"].get("integrated_lufs")
    records, variants = [], {}
    kinds = {field: kind for field, (_, kind) in fields_of(recipe).items()}
    for record in recipe.get("moves", []):
        if record.get("move") == "balance" or record.get("decision") not in ("kept", "changed"):
            continue
        entry = {key: record.get(key) for key in ("move", "source", "decision", "steps", "fields")}
        # A claimed gain_db or bus has no off state: the level or routing it replaced is not in the recipe.
        kept_on = [field for field in record.get("fields", []) if kinds.get(field, field.rsplit(".", 1)[-1]) in ("gain_db", "bus")]
        if kept_on:
            entry["kept_on_in_off"] = kept_on
        if not record.get("steps") and len(kept_on) == len(record.get("fields", [])):
            entry["off"] = {"skipped": "claims only gain_db or bus, which have no off state"}
        else:
            claims = tuple(sorted(record.get("steps", []) + record.get("fields", [])))
            if claims not in variants:
                variants[claims] = evaluate(recipe, song, off_variant(recipe, record), cache, layout, target, boundary, outside)
            entry["off"] = variants[claims]
        records.append(entry)
    numbers = {"timing_stems": timing_stems, "hits": counts, "current": current, "records": records}
    (cache / "report.json").write_text(json.dumps(numbers, indent=1))
    (cache / "report.md").write_text(markdown(numbers))
    return cache / "report.md"


def flatten(value, prefix=""):
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            out |= flatten(inner, f"{prefix}.{key}" if prefix else str(key))
        return out
    return {prefix: value}


def moved(before, after):
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (before, after))
    return abs(after - before) >= 0.05 if numeric else before != after


def markdown(numbers):
    found = ", ".join(f"{name} ({stem}, {numbers['hits'][name]} hits)" for name, stem in numbers["timing_stems"].items())
    lines = ["# Check report", "", f"Hits found on: {found or 'no drum sources'}", ""]
    current = flatten(numbers["current"])
    for record in numbers["records"]:
        lines += [f"## {record['move']} on {record['source']} ({record['decision']})", ""]
        if record.get("kept_on_in_off"):
            lines += [f"Still on in the without column: {', '.join(record['kept_on_in_off'])}.", ""]
        off = flatten(record["off"])
        if "skipped" in record["off"]:
            lines += [f"Not measured: {record['off']['skipped']}.", ""]
            continue
        lines += ["| measure | without | with |", "|---|---|---|"]
        lines += [f"| {key} | {off.get(key)} | {current.get(key)} |"
                  for key in sorted(set(current) | set(off)) if moved(off.get(key), current.get(key))]
        lines.append("")
    lines += ["## Current mix", "", "| measure | value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(current.items())]
    return "\n".join(lines) + "\n"
