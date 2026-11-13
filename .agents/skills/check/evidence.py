import copy
import functools
import hashlib
import json
import os
import pickle
import sys
import time
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
from memo import memoized

import contracts
import incremental
import outcomes as outcome_tools
import workers
from structure import CLAIMED_FIELDS, DRUM_CHAINS, Routing, apply_patch, fields_of, roles_of as structure_roles

ATTACK_HIT_MS = (0, 25)
SAME_HIT_MS = 30
OVERLAPS = [
    ("kick", "bass", [(0, 120), (60, 200)], "kick"),
    ("bass", "guitars", [(150, 500), (700, 3000)], None),
    ("synths", "guitars", [(200, 4000)], None),
]
MATCH_LU = 0.05
MATCH_ROUNDS = 12
MATCH_TRIAL_LU = 0.1
SECTION_INTENT_LU = 1.5
OWN_HIT_DB = 6
OWN_GROUP_MIN = 3
CODE = [ROOT / ".agents" / "skills" / path for path in (
    "render/render.py", "render/native.py", "render/airwindows.py", "render/prepared.py",
    "analyze/analyze.py", "analyze/hits.py", "analyze/measure.py", "analyze/memo.py", "analyze/notes.py", "align/align.py",
    "check/evidence.py", "check/structure.py", "check/chains.py", "check/contracts.py", "check/outcomes.py",
    "check/workers.py", "check/incremental.py",
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
    docs = sorted((ROOT / "docs" / "mixing-chains").glob("*.md"))
    profile = ROOT / "profiles" / str(recipe.get("profile")) / "profile.json"
    return [content(CODE + docs), content(sorted((song / "stems").iterdir())), content(plugins),
            content([profile]) if profile.exists() else None]


def fingerprint(*parts):
    def plain(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in dict.items(value)}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        return value
    # Dict order is kept: render sums stems, orders buses and applies settings in recipe order.
    with incremental.quiet():
        return hashlib.sha256(json.dumps(plain(list(parts))).encode()).hexdigest()


TEXT = {"note", "reason", "review", "tool_reason", "chain_reason", "split_reason", "role_reason"}


def untext(entry):
    return {key: value for key, value in entry.items() if key not in TEXT}


def numeric(recipe):
    """The recipe as renders and readings see it: no moves, notes, reviews or reasons."""
    out = {key: value for key, value in recipe.items() if key not in ("moves", "notes", "reviews")}
    if "steps" in out:
        out["steps"] = [untext(step) for step in out["steps"]]
    if "sources" in out:
        out["sources"] = {name: untext(source) for name, source in out["sources"].items()}
    return out


def record_part(record):
    """A record as its contract reads it. An override's presence and leaves decide the outcome, its reason does not."""
    out = untext(record)
    if isinstance(out.get("override"), dict):
        out["override"] = dict(out["override"], reason="reason" in out["override"])
    if isinstance(out.get("trial"), dict) and "steps" in out["trial"]:
        out["trial"] = dict(out["trial"], steps=[untext(step) for step in out["trial"]["steps"]])
    return out


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


def placed_stem(recipe, song, stem, length):
    audio, _ = soundfile.read(song / "stems" / stem, always_2d=True)
    return render.place(audio, recipe["stems"][stem].get("offset_samples", 0), length)


def own_tom_hits(recipe, song, sources, times, stem, onsets, rate, length):
    """A tom mic's onsets that are the tom, not the rest of the kit it also hears.

    At every hit of another drum the tom mic sits a near fixed level under each of that drum's mics, so an onset on
    such a hit is kept only when it is at least 6 dB louder than that bleed predicts. A tom's own hit is loudest in its
    own mic of all the tom mics, and close to the cymbal mics: kit bleed sits 6 dB or more under them.
    """
    tom = placed_stem(recipe, song, stem, length)
    keep = np.ones(len(onsets), dtype=bool)
    if not len(onsets):
        return onsets
    here = hit_tools.measure(tom, rate, onsets)["level_db"]
    for name, source in sources.items():
        if source.get("chain") not in DRUM_CHAINS or stem in source["stems"]:
            continue
        for mic in source["stems"]:
            other = placed_stem(recipe, song, mic, length)
            there = hit_tools.measure(other, rate, onsets)["level_db"]
            if source.get("chain") == "toms":
                keep &= ~(there > here)
                continue
            if source.get("chain") == "overheads":
                keep &= ~(there >= here + OWN_HIT_DB)
            hits = times.get(name, [])
            if not len(hits):
                continue
            ratio = hit_tools.measure(tom, rate, hits)["level_db"] - hit_tools.measure(other, rate, hits)["level_db"]
            bleed = float(np.median(ratio[np.isfinite(ratio)]))
            nearest = np.searchsorted(hits, onsets)
            for index, onset in enumerate(onsets):
                close = any(0 <= i < len(hits) and abs(int(hits[i]) - int(onset)) <= SAME_HIT_MS / 1000 * rate for i in (nearest[index] - 1, nearest[index]))
                if close and here[index] - there[index] < bleed + OWN_HIT_DB:
                    keep[index] = False
    # Bleed nothing else explains is left: own hits are the loud group above the highest level gap of 6 dB or more.
    kept = onsets[keep]
    if len(kept) > 1:
        levels = hit_tools.measure(tom, rate, kept)["level_db"]
        order = np.sort(levels)
        # One stray loud onset over a gap is not a group.
        wide = [i for i in np.flatnonzero(np.diff(order) >= OWN_HIT_DB) if len(order) - i - 1 >= OWN_GROUP_MIN]
        if wide:
            kept = kept[levels > order[wide[-1]]]
    return kept


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


@memoized
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


class Master:
    """A render's trial master, made on first read: matching it to the target runs the master chain several times."""

    def __init__(self, result, target):
        self.result, self.target, self.made = result, target, None

    def make(self):
        if self.made is None:
            with incremental.quiet():
                self._make()
        return self.made

    def _make(self):
        if self.made is None:
            if self.target is None:
                master_in, trial = self.result.master(self.result.mix)
                self.made = {"drive_change_db": 0.0, "matched": False}, master_in, trial
            else:
                self.made = matched_master(self.result, self.target)

    def get(self, name):
        if not self.result.master_steps:
            return None
        # An unmatched master is never read, so it is made only for the battery's numbers.
        if name == "matched" and self.target is None:
            return False
        numbers, master_in, trial = self.make()
        return {"matched": numbers["matched"], "trial": trial, "master-in": master_in}[name]


def battery(recipe, result, points, times, chain_nodes, master):
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
    numbers, master_in, trial = master.make()
    out["trial"] = dict(numbers)
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
    raw = dict(times)
    for name, source in sources.items():
        if source.get("chain") == "toms" and name in times:
            times[name] = own_tom_hits(recipe, song, sources, raw, timing_stems[name], raw[name], rate, length)
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
    return points, chain_times, chain_nodes, timing_stems, {name: len(found) for name, found in times.items()}, times


def plan_key(recipe, nodes, outside):
    """Everything plan() reads: the stems' samples and the code (in `outside`), and the recipe's routing, offsets,
    sources and step targets. No level, pan or setting decides where a hit is."""
    def routing(entries, extra=()):
        return {name: {"bus": entry.get("bus", "mix"), "sends": [send.get("bus") for send in entry.get("sends", [])]}
                | {key: entry[key] for key in extra if key in entry} for name, entry in entries.items()}
    sources = {name: untext(source) for name, source in recipe.get("sources", {}).items()}
    # Only a step on a bus that is no node adds a point, so adding or dropping a stem's or node's step keeps the key.
    points = [target for target in dict.fromkeys(step["target"] for step in recipe.get("steps", []))
              if target != "mix" and target not in nodes.values() and target not in recipe["stems"]]
    return fingerprint("plan", outside[:2], sources, routing(recipe["stems"], ("offset_samples",)),
                       routing(recipe.get("buses", {})), points, recipe.get("tail_seconds", 0), nodes)


def cached_plan(recipe, song, nodes, outside):
    """plan(), kept on disk by everything it reads, so a re-check with the same stems and routing skips hit detection."""
    path = song / "renders" / "check" / f"plan-{plan_key(recipe, nodes, outside)}.pkl"
    try:
        data = path.read_bytes()
        if hashlib.sha256(data[32:]).digest() == data[:32]:
            return pickle.loads(data[32:])
    except Exception:
        pass
    planned = plan(recipe, song, nodes)
    payload = pickle.dumps(planned, protocol=5)
    try:
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_bytes(hashlib.sha256(payload).digest() + payload)
        os.replace(temp, path)
    except OSError:
        pass
    # The same round trip a hit makes, so a cold check and a re-check build on equal objects.
    return pickle.loads(payload)


MAX_VIEWS = 3
STARTED = time.monotonic()
RENDERS = [0]


def progress(message):
    print(f"check: {message}, {time.monotonic() - STARTED:.0f} s", file=sys.stderr, flush=True)


def write_atomic(path, text):
    # Worker processes write the same cache files the check reads, and one killed mid-write must leave no torn file.
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(text)
    os.replace(temp, path)


def node_memo():
    megabytes = os.environ.get("STEMMASTER_NODE_MEMO_MB", str(render.MEMO_BYTES >> 20))
    try:
        megabytes = int(megabytes)
    except ValueError:
        sys.exit(f"STEMMASTER_NODE_MEMO_MB must be a whole number of MB (0 turns the memo off), not {megabytes!r}")
    if megabytes <= 0:
        return None
    folder = os.environ.get("STEMMASTER_NODE_MEMO_DIR")
    try:
        folder = folder or render.memo_folder()
        return render.Memo(folder, megabytes << 20)
    except Exception as error:
        print(f"warning: node memo off, can't use {folder}: {error}", file=sys.stderr)
        return None


class Variants:
    """A view renders its variant. A battery is measured only for a key the report publishes, and a matched master only
    when a reading or a battery needs one."""

    def __init__(self, recipe, song, cache, layout, boundary, outside, ctx, taps, published):
        self.recipe, self.song, self.cache, self.layout = recipe, song, cache, layout
        self.boundary, self.outside, self.ctx, self.taps = boundary, outside, ctx, taps
        self.views, self.batteries, self.probes = {}, {}, {}
        self.recipes = {"current": incremental.track(recipe, "current")}
        self.target = ctx.target
        self.memo = node_memo()
        self.published = set(published)
        self.contracts = None
        self.signal_keys = incremental.Keys(self)

    def add(self, key, variant, probes=None):
        if key not in self.recipes:
            self.recipes[key] = incremental.track(variant, key)
        if probes:
            self.probes.setdefault(key, {}).update(probes)
        return key

    def battery_path(self, key):
        points, chain_times, chain_nodes = self.layout
        return self.cache / f"{fingerprint(numeric(self.recipes[key]), self.target, self.boundary, self.outside, points, chain_times, chain_nodes)}.json"

    def battery(self, key):
        with incremental.quiet():
            return self._battery(key)

    def _battery(self, key):
        if key not in self.batteries:
            path = self.battery_path(key)
            if path.exists():
                self.batteries[key] = json.loads(path.read_text())
            else:
                view = self.view(key)
                numbers = battery(self.recipe, view.result, *self.layout, view.master)
                write_atomic(path, json.dumps(numbers))
                self.batteries[key] = numbers
        return self.batteries[key]

    def drop(self, key):
        # A published battery is measured before its render goes, so it never needs a second render.
        if key in self.published:
            self.battery(key)
        del self.views[key]

    def view(self, key):
        with incremental.quiet():
            return self._view(key)

    def _view(self, key):
        if key in self.views:
            return self.views[key]
        # The current view stays; the others are dropped oldest first, since each render holds every tap.
        others = [k for k in self.views if k != "current"]
        while len(others) >= MAX_VIEWS - 1:
            self.drop(others.pop(0))
        variant = self.recipes[key]
        plugins = {step.get("id"): step["plugin"] for step in variant.get("steps", [])}
        probe = {"Duck": outcome_tools.duck_depth, "Ride": outcome_tools.gain_change, "Gain": outcome_tools.gain_change,
                 "Delay": functools.partial(outcome_tools.engagement, align=False)}
        probes = {step_id: probe.get(plugins.get(step_id), outcome_tools.engagement) for step_id in self.probes.get(key, {})}
        taps = sorted(({tap for tap, _ in self.layout[0].values()} | self.taps) & set(render.sources(variant)))
        result = render.Result(variant, self.song, taps, strict=variant is self.recipe, boundary=self.boundary, probes=probes,
                               memo=self.memo)
        points, _, chain_nodes = self.layout
        # A battery reads every observation tap, so a variant that loses one fails here even when none is measured.
        for tap in [tap for tap, _ in points.values()] + [node for first, second, _, _ in OVERLAPS for chain in (first, second)
                                                          for node in chain_nodes.get(chain, [])]:
            result.tapped[tap]
        master = Master(result, self.target)
        if set(probes) & {step.get("id") for step in result.master_steps}:
            # Each master pass rewrites its steps' probes, so the matched master runs first, before any other pass.
            master.make()
        else:
            # A master no reading needs may never run, yet its bad settings must still fail here.
            for step in result.master_steps:
                if step["plugin"] not in render.NATIVE:
                    render.load(step)
        view = outcome_tools.View(self.ctx, result, master, variant)
        view.key = key
        # A copy: a later master render (a contract reading the actual master) writes its probes into the mixer, and
        # every contract must read the same matched-drive probes whatever order the contracts run in.
        view.probed = Probed(result.mixer.probed)
        view.probed.key = key
        view.twin = lambda other: self.view(self.add(recipe_key(other), other))
        self.views[key] = view
        RENDERS[0] += 1
        progress(f"{RENDERS[0]} variant renders")
        return view


def release(variants):
    """Lets a finished check's renders go: each view's twin points back at its Variants, a cycle that would keep them."""
    for view in variants.views.values():
        view.twin = None
    variants.views.clear()
    variants.batteries.clear()
    variants.ctx = variants.memo = None


class Probed(dict):
    """A view's step probes; a contract's read of one is recorded with the key of the step's input and output."""

    key = None

    def get(self, step_id, default=None):
        incremental.reading(self.key, f"probe:{step_id}")
        return dict.get(self, step_id, default)

    def __getitem__(self, step_id):
        incremental.reading(self.key, f"probe:{step_id}")
        return dict.__getitem__(self, step_id)

    def items(self):
        incremental.give_up("read every probe of a view")
        return dict.items(self)


def node_tap(ctx, source):
    # Without a drums bus there is no one place to try a drum-bus move: the first drum's node is not the kit.
    if source == "drum-bus":
        return outcome_tools.drums_bus(ctx)
    names = outcome_tools.point_names(ctx, "node", source)
    return names[0] if names else None


def trial_variant(recipe, record, ctx):
    variant = copy.deepcopy(recipe)
    move, source = record["move"], record["source"]
    tag = unique_tag(recipe, f"trial-{move}-{source}".replace(".", "-"))
    patch = record.get("trial")
    if patch is None:
        default = contracts.DEFAULT_TRIALS.get(move)
        target = node_tap(ctx, source)
        if default is None or target is None:
            return None, None, None
        core = dict(default["core"], id=f"{tag}-core", target=target, settings=dict(default["core"]["settings"]))
        steps = variant.get("steps", [])
        if "send_db" in default:
            bus = f"{tag}-bus"
            variant.setdefault("buses", {})[bus] = {}
            entries = variant["stems"] if target in variant["stems"] else variant["buses"]
            entries.setdefault(target, {}).setdefault("sends", []).append({"bus": bus, "gain_db": default["send_db"]})
            before = [dict(step, id=f"{tag}-pre{i}", target=bus, settings=dict(step["settings"])) for i, step in enumerate(default["before"])]
            variant["steps"] = steps + before + [dict(core, target=bus)]
            return variant, [core["id"]], None
        else:
            # Right after the node's own steps, with a Gain the check sets to equal loudness, as a hand trial would be.
            last = max([i for i, step in enumerate(steps) if step["target"] == target], default=len(steps) - 1)
            gain = {"id": f"{tag}-match", "target": target, "plugin": "Gain", "settings": {"gain_db": 0.0}}
            variant["steps"] = steps[: last + 1] + [core, gain] + steps[last + 1 :]
            return variant, [core["id"]], gain["id"]
    variant = apply_patch(recipe, patch)
    return variant, [step["id"] for step in patch.get("steps", []) if step.get("plugin") != "Gain"], None


def unique_tag(recipe, base):
    """A prefix no step, stem or bus name starts with, so a trial's steps and bus never share a name with the song's
    (render probes are stored by step id)."""
    taken = {step.get("id") for step in recipe.get("steps", [])} | set(recipe["stems"]) | set(render.bus_order(recipe))
    tag, n = base, 1
    while any(name == tag or str(name).startswith(f"{tag}-") for name in taken):
        n += 1
        tag = f"{base}-{n}"
    return tag


def match_db(recipes, key, match_id):
    step = next((s for s in recipes[key]["steps"] if s["id"] == match_id), None) if match_id else None
    return step["settings"]["gain_db"] if step else None


def set_match(recipes, key, match_id, gain_db):
    step = next((s for s in recipes[key]["steps"] if s["id"] == match_id), None) if match_id and gain_db is not None else None
    if step:
        step["settings"]["gain_db"] = gain_db


def match_trial(variants, key, target, match_id):
    variant = variants.recipes[key]
    gain = next((step for step in variant["steps"] if step["id"] == match_id), None)
    if gain is None:
        return
    a, b = variants.view(key).lufs(f"tap:{target}", None), variants.view("current").lufs(f"tap:{target}", None)
    if a is None or b is None or abs(a - b) <= MATCH_TRIAL_LU:
        return
    incremental.give_up("matched a trial's loudness")
    gain["settings"]["gain_db"] = round(b - a, 3)
    variants.views.pop(key, None)
    variants.batteries.pop(key, None)
    variants.view(key)


def alt_variant(recipe, record, depth):
    """kick.duck-bass's alternative: the same band on the same target, cut statically at the Duck's typical depth."""
    variant = copy.deepcopy(recipe)
    claimed = set(record.get("steps", []))
    seconds = 1e6
    variant["steps"] = [
        {"id": f"{step['id']}-static", "target": step["target"], "plugin": "Ride",
         "settings": {"segments": [[0, seconds, -depth.get(step["id"], 0.0)]], "band_hz": step["settings"].get("band_hz")}}
        if step.get("id") in claimed and step["plugin"] == "Duck" else step
        for step in variant["steps"]
    ]
    return variant


def tried_variants(recipe, boundary):
    """master.limit's settings tried: the current limiter with its release halved and doubled."""
    out = []
    for factor in (0.5, 2):
        variant = copy.deepcopy(recipe)
        step = next((s for s in variant["steps"] if s.get("id") == boundary), None)
        if step is None or "release_release" not in step["settings"]:
            return []
        step["settings"]["release_release"] = step["settings"]["release_release"] * factor
        out.append(variant)
    return out


def facts(recipe, routing, ctx, records):
    def reverb_on(bus):
        return any(step["target"] == bus and any(word in step["plugin"] for word in ("Reverb", "EarlyReflections"))
                   for step in recipe.get("steps", []))

    def returns_of(names):
        return {send["bus"] for name in names if name for send in ctx._settings(name, recipe).get("sends", [])}

    def own(source):
        return ctx.sources.get(source, {}).get("stems", []) + [ctx.nodes.get(source)]

    drums = [name for name, source in ctx.sources.items() if source.get("chain") in DRUM_CHAINS]
    kit_reverbs = {bus for bus in returns_of([n for d in drums for n in own(d)]) if reverb_on(bus)}
    all_roles = [role for roles in ctx.roles.values() for role in roles.values()]
    masters = [step for step in recipe.get("steps", []) if step.get("stage") == "master"]

    def fact(name, args, record):
        source = record["source"]
        value = {
            "always": lambda: True,
            "no_space": lambda: "room" not in all_roles and not any(reverb_on(bus) for bus in returns_of(own(source))),
            "kit_reverb_unfed": lambda: bool(kit_reverbs) and not (returns_of(own(source)) & kit_reverbs),
            "no_kit_reverb": lambda: not kit_reverbs,
            "has_role": lambda: any(role in args["roles"] for role in all_roles),
            "no_role": lambda: not any(role in args["roles"] for role in all_roles),
            "lacks_roles": lambda: not any(role in ctx.roles.get(source, {}).values() for role in args["roles"]),
            "has_roles": lambda: all(role in ctx.roles.get(source, {}).values() for role in args["roles"]),
            "move_active": lambda: any(r.get("move") == args["move"] and r.get("decision") in ("kept", "changed") for r in records),
            "has_synth_role": lambda: all(s.get("role") in ("bass", "lead", "pad") for s in ctx.sources.values() if s.get("chain") == "synths"),
            "synth_role": lambda: ctx.sources.get(source, {}).get("role") in args["roles"],
            "grouped": lambda: grouped(routing, ctx),
            "trial_limiter": lambda: bool(masters) and masters[-1]["plugin"] in render.LIMITERS,
        }[name]()
        return outcome_tools.TRUE if value else outcome_tools.FALSE
    return fact


def grouped(routing, ctx):
    bus = outcome_tools.drums_bus(ctx)
    if bus is None:
        return False
    stems = [stem for source in ctx.sources.values() if source.get("chain") in DRUM_CHAINS for stem in source["stems"]]
    # Each drum stem reaches the drums bus through a group of its own, not straight into it.
    return all(bus in routing.path(stem) and routing.path(stem).index(bus) > 0 for stem in stems)


def chain_stems(ctx, source):
    """The stems a record's part may share a bus with: its chain's, the whole kit for the drum-bus, everything for the
    mix-bus and master."""
    if source in ("mix-bus", "master"):
        return set(ctx.recipe["stems"])
    chains = DRUM_CHAINS if source == "drum-bus" else {ctx.sources.get(source, {}).get("chain")}
    return {stem for src in ctx.sources.values() if src.get("chain") in chains for stem in src.get("stems", [])}


def contract_taps(ctx, records):
    taps = set()
    for record in records:
        contract = contracts.CONTRACTS.get(record.get("move"))
        if contract is None:
            continue
        for tree in (contract["needed"], contract["worked"]):
            for leaf in walk(tree):
                for key in ("point", "other"):
                    names = outcome_tools.point_names(ctx, leaf.get("args", {}).get(key, "node"), record["source"])
                    taps |= set(names or [])
        if "lead" in ctx.roles.get(record["source"], {}).values() or ctx.sources.get(record["source"], {}).get("chain") == "guitars":
            # Lead and rhythm readings sum each role's own outputs, which no leaf names directly.
            for role in ("lead", "rhythm"):
                taps |= set(outcome_tools.point_names(ctx, role, record["source"]) or [])
        acting = outcome_tools.acting_point(ctx.recipe, ctx.routing, record.get("steps", []), ctx.nodes.get(record["source"]),
                                            ctx.sources.get(record["source"], {}).get("stems", []), chain_stems(ctx, record["source"]))
        taps |= set(acting) if isinstance(acting, tuple) else {acting}
        steps = {step.get("id"): step for step in ctx.recipe.get("steps", []) + record.get("trial", {}).get("steps", [])}
        taps |= {steps[i]["target"] for i in record.get("steps", []) + [t.get("id") for t in record.get("trial", {}).get("steps", [])] if i in steps}
    return taps - {None, "mix", "trial", "premaster", "master-in", outcome_tools.UNREAD}


def walk(tree):
    if tree["kind"] in ("any", "all"):
        for child in tree["items"]:
            yield from walk(child)
    else:
        yield tree


def outcome(record, needed, worked, has_pair):
    T, F, U, R, S = outcome_tools.TRUE, outcome_tools.FALSE, outcome_tools.UNRESOLVED, outcome_tools.REVIEW, outcome_tools.SKIPPED
    if needed in (F, S):
        return "dropped", "needed"
    if needed == U:
        return "unresolved", "needed"
    if needed == R and not has_pair:
        return "review", "needed"
    if not has_pair:
        return "needs a trial", "needed"
    if worked == T:
        return "kept" if needed == T else "review", "worked"
    if worked == F:
        return "dropped", "worked"
    if worked == U:
        return "unresolved", "worked"
    return "review", "worked"


def judge(entry, needed_leaves, worked_leaves, needed, worked):
    record = entry["record"]
    decision = record.get("decision")
    computed, part = outcome(record, needed, worked, entry["has_pair"])
    fails = []
    override = record.get("override") or {}
    deciding = needed_leaves if part == "needed" else worked_leaves
    state = needed if part == "needed" else worked
    required = {leaf["id"] for leaf in deciding if leaf["state"] == state}
    # A kept move answers for what it does too: an override of its Needed if must also name each failed Worked if leaf.
    if decision in ("kept", "changed"):
        required |= {leaf["id"] for leaf in worked_leaves if leaf["state"] == outcome_tools.FALSE}
    cited = set(override.get("leaves", []))
    if override and not (required and required <= cited):
        fails.append(f"override must name the leaves it contradicts: {sorted(required)}")
    unsupported = sorted(leaf["id"] for leaf in needed_leaves + worked_leaves if leaf["id"] in cited and leaf["state"] == outcome_tools.UNRESOLVED)
    if unsupported:
        fails.append(f"an override answers measurements, not missing ones: {unsupported} are unresolved")
    if decision in ("kept", "changed") and computed != "unresolved":
        states = {leaf["id"]: (outcome_tools.TRUE if leaf["id"] in cited and leaf["state"] == outcome_tools.FALSE else leaf["state"])
                  for leaf in worked_leaves}
        if worked is not None and outcome_tools.restate(entry["contract"]["worked"], states, "worked") == outcome_tools.UNRESOLVED:
            missing = [leaf["text"] for leaf in worked_leaves if leaf["state"] == outcome_tools.UNRESOLVED]
            fails.append(f"unresolved: a kept move needs supported evidence for {missing}")
    better = [leaf["text"] for leaf in worked_leaves if leaf.get("measure") == "blend_shortfall" and leaf["state"] == outcome_tools.FALSE]
    if better:
        fails.append(f"a better polarity or offset exists ({better}); use it")
        return computed, fails
    if computed == "unresolved":
        missing = [leaf["text"] for leaf in deciding if leaf["state"] == outcome_tools.UNRESOLVED]
        fails.append(f"unresolved: no supported evidence for {missing}")
    elif computed == "needs a trial":
        fails.append("its Needed if finds a fault: give a trial, or keep it")
    elif decision in ("kept", "changed") and (computed == "dropped" or worked == outcome_tools.FALSE) and not override:
        fails.append("the default outcome is dropped; drop it, or give an override with a measured reason")
    elif computed == "kept" and decision == "dropped" and not override:
        fails.append("the trial worked; keep it, or give an override with a measured reason")
    if computed == "dropped" and part == "worked" and decision == "dropped" and "trial" in record and not override:
        fails.append("a record trial that failed needs an override naming its failing leaves")
    for step_id, residual in entry.get("engaged", {}).items():
        if residual is None or residual < outcome_tools.INEFFECTIVE_DB:
            fails.append(f"{step_id} is ineffective (residual {residual} dB after matching)")
    return computed, fails


def off_key(recipe, record):
    return recipe_key(off_variant(recipe, record))


def recipe_key(variant):
    # Keyed by the recipe itself: a step id may equal another record's field path, and two roles (no-send and
    # no-delay) may need the very same render.
    return f"recipe:{fingerprint(numeric(variant))}"


def contract_entries(recipe, variants, ctx, master_mode):
    """Each record's contract and variant keys, made once per Variants: the taps they add are in every contract's key."""
    if variants.contracts is not None:
        return variants.contracts
    records = [r for r in recipe.get("moves", []) if r.get("move") != "balance"
               and (master_mode or not str(r.get("move")).startswith("master."))]
    entries = []
    for record in records:
        entry = {"record": record, "contract": contracts.CONTRACTS[record["move"]], "keys": {}}
        if record.get("decision") in ("kept", "changed"):
            key = variants.add(off_key(recipe, record), off_variant(recipe, record))
            entry["keys"] = {"base": key, "with": "current", "without": key}
            for step_id in record.get("steps", []):
                variants.probes.setdefault("current", {})[step_id] = "probe"
            sends = [field for field in record.get("fields", []) if ".sends." in field]
            if sends:
                nosend = off_variant(recipe, {"fields": sends})
                entry["keys"]["nosend"] = variants.add(recipe_key(nosend), nosend)
        elif entry["contract"].get("compare") == "options":
            # Align's polarity and offset options are the comparison; the current recipe is the option chosen.
            entry["keys"] = {"base": "current", "with": "current"}
        else:
            entry["keys"] = {"base": "current"}
            variant, probed, entry["match"] = trial_variant(recipe, record, ctx)
            if variant is not None:
                key = variants.add(f"trial:{record['move']}:{record['source']}", variant, {i: "engage" for i in probed})
                entry["keys"] |= {"with": key, "without": "current"}
                entry["probed"] = probed
                if without_new_sends(recipe, variant) != variant:
                    # The trial with every send it adds removed and its other steps kept, so dry gain can't stand in for a dead send.
                    nosend = without_new_sends(recipe, variant)
                    entry["keys"]["nosend"] = variants.add(recipe_key(nosend), nosend)
        entries.append(entry)
    for entry in entries:
        if "with" in entry["keys"]:
            ctx.join_recipe = variants.recipes[entry["keys"]["with"]]
            variants.taps.add(ctx.join(entry["record"]["source"]))
            variants.taps.update(ctx.own_returns(entry["record"]["source"]))
    ctx.join_recipe = None
    variants.taps.discard(None)
    variants.contracts = records, entries
    return variants.contracts


def contract_path(recipe, variants, ctx, master_mode, records, entry):
    active = sorted({json.dumps([r.get("move"), r.get("decision")]) for r in records})
    variant_parts = {role: numeric(variants.recipes[key]) for role, key in entry["keys"].items()}
    # Taps come from every record, and a reading of an untapped name is None, so they are part of every key.
    # The override is left out: only judge reads it, and judge runs on every hit, so editing one re-reads nothing.
    unoverridden = {key: value for key, value in entry["record"].items() if key != "override"}
    key = fingerprint(record_part(unoverridden), entry["contract"], variant_parts, numeric(recipe), active,
                      sorted(variants.taps), variants.outside, ctx.times, ctx.target, master_mode)
    return variants.cache / f"contract-{key}.json"


def evaluate_entry(recipe, variants, ctx, master_mode, records, entry, fact):
    record, keys = entry["record"], entry["keys"]
    ctx.notes_read = []
    compare = entry["contract"].get("compare")
    if compare == "alt" and "with" in keys:
        with_key = keys["with"]
        depth = {k: v.get("depth_db", 0.0) for k, v in variants.view(with_key).probed.items()}
        judged = record.get("steps", []) + entry.get("probed", [])
        keys["alt"] = variants.add(f"alt:{record['move']}:{record['source']}",
                                   alt_variant(variants.recipes[with_key], {"steps": judged}, depth))
    if compare == "tried" and master_mode:
        keys["tried"] = [variants.add(f"tried:{i}", v) for i, v in enumerate(tried_variants(recipe, variants.boundary))]
    if "with" in keys and any(leaf.get("measure") == "delay_gap_tail" for leaf in walk(entry["contract"]["worked"])):
        nodelay = outcome_tools.without_lead_delays(ctx, record["source"], variants.recipes[keys["with"]])
        if nodelay is not None:
            keys["nodelay"] = variants.add(recipe_key(nodelay), nodelay)
            nolead = outcome_tools.without_lead(ctx, record["source"], nodelay)
            keys["nolead"] = variants.add(recipe_key(nolead), nolead)
    if "trial" not in record and entry.get("probed"):
        match_trial(variants, keys["with"], node_tap(ctx, record["source"]), entry.get("match"))
    views = {role: [variants.view(k) for k in key] if isinstance(key, list) else variants.view(key) for role, key in keys.items()}
    if compare == "options" and "without" not in keys:
        # A dropped blend leaves the recipe's own options in place: only whether they are the best is read.
        views["options_only"] = True
    entry["views"] = views
    entry["has_pair"] = "with" in views
    if record.get("decision") in ("kept", "changed"):
        entry["engaged"] = engaged(views["with"], record.get("steps", []), recipe)
        if "nosend" in views:
            # Only the sends are off in this variant, so the move's own dry gains can't stand in for an inaudible send.
            entry["engaged"]["sends"] = delivered(views["with"], views["nosend"], ctx, record["source"])
    elif "with" in keys and keys["with"] != "current":
        entry["engaged"] = engaged(views["with"], entry.get("probed", []), variants.recipes[keys["with"]])
        if "nosend" in views:
            entry["engaged"]["sends"] = delivered(views["with"], views["nosend"], ctx, record["source"])
    judged_recipe = variants.recipes[keys["with"]] if "with" in keys else recipe
    trial_ids = [step["id"] for step in record.get("trial", {}).get("steps", [])] or entry.get("probed", [])
    ctx.acting = outcome_tools.acting_point(judged_recipe, Routing(judged_recipe), record.get("steps", []) + trial_ids,
                                            ctx.nodes.get(record["source"]), ctx.sources.get(record["source"], {}).get("stems", []),
                                            chain_stems(ctx, record["source"]))
    ctx.join_recipe = variants.recipes[keys["with"]] if "with" in keys else None
    ctx.record = record
    ctx.trial_ids = trial_ids + ([entry["match"]] if entry.get("match") else [])
    base = variants.recipes[keys["base"]]
    needed_leaves, worked_leaves = [], []
    needed = outcome_tools.evaluate(entry["contract"]["needed"], record, views, facts(base, Routing(base), ctx, records),
                                    "needed", needed_leaves)
    worked = outcome_tools.evaluate(entry["contract"]["worked"], record, views, fact, "worked", worked_leaves, on_with=True) \
        if "with" in views else None
    evaluated = {"needed": needed, "worked": worked, "needed_read_on": keys["base"], "with": keys.get("with"),
                 "without": keys.get("without"), "engaged": entry.get("engaged", {}),
                 "leaves": needed_leaves + worked_leaves, "read_at": ctx.acting, "has_pair": entry["has_pair"]}
    ctx.acting = ctx.join_recipe = ctx.record = None
    ctx.trial_ids = []
    return evaluated


def trace_static(recipe, variants, ctx, master_mode, records, entry):
    """contract_path's key with the recipe values a trace checks one by one left out."""
    active = sorted({json.dumps([r.get("move"), r.get("decision")]) for r in records})
    parts = {role: incremental.structure(variants.recipes[key]) for role, key in entry["keys"].items()}
    unoverridden = {key: value for key, value in entry["record"].items() if key != "override"}
    return fingerprint("trace", record_part(unoverridden), entry["contract"], parts, incremental.structure(recipe), active,
                       sorted(variants.taps), variants.outside, ctx.times, ctx.target, master_mode)


def trace_path(variants, master_mode, record):
    return variants.cache / f"trace-{fingerprint(record['move'], record['source'], master_mode)}.json"


def promote(recipe, variants, ctx, master_mode):
    """Writes the exact-key cache file of each contract whose last trace still holds, so the check reads it as cached."""
    records, entries = contract_entries(recipe, variants, ctx, master_mode)
    reused = 0
    for entry in entries:
        path = contract_path(recipe, variants, ctx, master_mode, records, entry)
        found = trace_path(variants, master_mode, entry["record"])
        if path.exists() or not found.exists():
            continue
        trace = incremental.unseal(found)
        if not incremental.valid(trace):
            continue
        keys = entry["keys"]
        try:
            held = incremental.holds(trace, variants.signal_keys, keys, variants, trace_static(recipe, variants, ctx, master_mode, records, entry))
        except (Exception, SystemExit):
            # A recipe whose keys can't be computed is a miss: the check's own render reports its error in its order.
            held = False
        if held:
            # A traced contract matched no trial's loudness, so its trial's gain is the variant's own, as a cold run writes it.
            payload = trace["payload"] | {"needed_read_on": keys["base"], "with": keys.get("with"), "without": keys.get("without"),
                                          "match_db": match_db(variants.recipes, keys.get("with"), entry.get("match"))}
            write_atomic(path, incremental.seal(payload))
            reused += 1
    if reused:
        progress(f"{reused} contracts reused: nothing they read changed")


def contract_report(recipe, song, nodes, variants, ctx, master_mode, only=None):
    """Every record's contract outcome; with `only`, just those (move, source) records, for a worker to cache."""
    records, entries = contract_entries(recipe, variants, ctx, master_mode)
    routing = Routing(recipe)
    fact = facts(recipe, routing, ctx, records)
    out = []
    # Records that share a variant run together, so each render is made once and dropped once.
    ordered = sorted(entries, key=lambda e: str(e["keys"].get("without", "")) + str(e["keys"].get("with", "")))
    for done, entry in enumerate(ordered, 1):
        record, keys = entry["record"], entry["keys"]
        if only is not None and (record["move"], record["source"]) not in only:
            continue
        progress(f"contract {done}/{len(ordered)}")
        path = contract_path(recipe, variants, ctx, master_mode, records, entry)
        cached = incremental.unseal(path)
        if cached is not None:
            for source, summary in cached.pop("notes").items():
                ctx.notes_summary.setdefault(source, summary)
            # --apply-defaults writes the trial as measured, so a cache hit must leave its matched gain in the variant too.
            set_match(variants.recipes, keys.get("with"), entry.get("match"), cached.pop("match_db", None))
            # The key leaves out text and the override, so both come from the current record, never the cache.
            out.append(verdict(entry, cached))
            continue
        static = trace_static(recipe, variants, ctx, master_mode, records, entry)
        pristine = dict(keys)
        with incremental.recording() as trace:
            evaluated = evaluate_entry(recipe, variants, ctx, master_mode, records, entry, fact)
        # The sources this record read, in first-read order, so a cache hit adds them to the report as a cold run would.
        read = {source: notes_tool_summary(ctx.notes[source]) for source in ctx.notes_read}
        for source, summary in read.items():
            ctx.notes_summary.setdefault(source, summary)
        payload = evaluated | {"notes": read, "match_db": match_db(variants.recipes, keys.get("with"), entry.get("match"))}
        write_atomic(path, incremental.seal(payload))
        found = incremental.finish(trace, variants.signal_keys, pristine, static, json.loads(json.dumps(payload)))
        if found is not None:
            write_atomic(trace_path(variants, master_mode, record), incremental.seal(found))
        out.append(verdict(entry, evaluated))
        entry.pop("views")
    order = {(r["move"], r["source"]): i for i, r in enumerate(records)}
    return sorted(out, key=lambda r: order[(r["move"], r["source"])])


def verdict(entry, evaluated):
    """A record's outcome and fails, judged from its evaluated leaves and its current decision and override."""
    record = entry["record"]
    leaves = evaluated["leaves"]
    parts = ([leaf for leaf in leaves if leaf["id"].split(".")[0] == part] for part in ("needed", "worked"))
    computed, fails = judge({"record": record, "contract": entry["contract"], "has_pair": evaluated["has_pair"],
                             "engaged": evaluated["engaged"]}, *parts, evaluated["needed"], evaluated["worked"])
    return {"move": record["move"], "source": record["source"], "decision": record.get("decision"),
            "needed": evaluated["needed"], "worked": evaluated["worked"], "outcome": computed, "fails": fails,
            "needed_read_on": evaluated["needed_read_on"], "with": evaluated["with"], "without": evaluated["without"],
            "engaged": evaluated["engaged"], "leaves": leaves, "override": record.get("override"), "read_at": evaluated["read_at"]}


def without_new_sends(recipe, variant):
    out = copy.deepcopy(variant)
    for kind in ("stems", "buses"):
        for name, settings in out.get(kind, {}).items():
            before = recipe.get(kind, {}).get(name, {}).get("sends", [])
            if settings.get("sends", []) != before:
                settings["sends"] = list(before)
    return out


def delivered(on, off, ctx, source):
    """What a move's sends add to the mix, against its source's dry node: a send too quiet to hear does nothing."""
    node = outcome_tools.point_names(ctx, "node", source)
    dry = on.signal(node[0]) if node else None
    if dry is None:
        return None
    added = float(((on.signal("mix") - off.signal("mix")) ** 2).sum())
    return round(10 * np.log10(max(added, 1e-30) / max(float((dry**2).sum()), 1e-30)), 2)


def engaged(view, step_ids, recipe):
    """Each processing step's residual; when a move claims only Gain steps, those gains must change something."""
    steps = [s for s in recipe.get("steps", []) if s.get("id") in set(step_ids)]
    judged = [s for s in steps if s["plugin"] != "Gain"] or steps
    return {step["id"]: view.probed.get(step["id"], {}).get("residual_db") for step in judged}


def prepare(recipe, song, nodes, planned, outside):
    """The check's variants, context and published records, built the same way in the check and in each worker."""
    points, chain_times, chain_nodes, timing_stems, counts, times = planned
    layout = (points, chain_times, chain_nodes)
    masters = [step for step in recipe.get("steps", []) if step.get("stage") == "master"]
    boundary = masters[-1].get("id") if masters else None
    ctx = outcome_tools.Context(recipe, song, nodes, Routing(recipe), times)
    records_all = [r for r in recipe.get("moves", []) if r.get("move") != "balance"]
    kinds = {field: kind for field, (_, kind) in fields_of(recipe).items()}
    records = []
    for record in records_all:
        if record.get("decision") not in ("kept", "changed"):
            continue
        entry = {key: record.get(key) for key in ("move", "source", "decision", "steps", "fields")}
        # A claimed gain_db or bus has no off state: the level or routing it replaced is not in the recipe.
        kept_on = [field for field in record.get("fields", []) if kinds.get(field, field.rsplit(".", 1)[-1]) in ("gain_db", "bus")]
        if kept_on:
            entry["kept_on_in_off"] = kept_on
        if not record.get("steps") and len(kept_on) == len(record.get("fields", [])):
            entry["off"] = {"skipped": "claims only gain_db or bus, which have no off state"}
        else:
            entry["off"] = off_key(recipe, record)
        records.append(entry)
    published = {"current"} | {entry["off"] for entry in records if isinstance(entry["off"], str)}
    cache = song / "renders" / "check"
    variants = Variants(recipe, song, cache, layout, boundary, outside, ctx, contract_taps(ctx, records_all), published)
    return variants, ctx, records


def report(recipe, song, nodes, master_mode=False):
    return measured(recipe, song, nodes, master_mode)[:2]


def measured(recipe, song, nodes, master_mode=False):
    """The report, and the variants it judged each record on (a kept default reads its trial from them)."""
    cache = song / "renders" / "check"
    cache.mkdir(parents=True, exist_ok=True)
    outside = inputs(song, recipe)
    planned = cached_plan(recipe, song, nodes, outside)
    variants, ctx, records = prepare(recipe, song, nodes, planned, outside)
    progress(f"report on {sum(r.get('move') != 'balance' for r in recipe.get('moves', []))} moves")
    promote(recipe, variants, ctx, master_mode)
    workers.prewarm(recipe, song, nodes, master_mode, planned, outside, variants, ctx)
    timing_stems, counts = planned[3], planned[4]
    contract = contract_report(recipe, song, nodes, variants, ctx, master_mode)
    current = variants.battery("current")
    for done, entry in enumerate(records, 1):
        progress(f"off {done}/{len(records)}")
        if isinstance(entry["off"], str):
            entry["off"] = variants.battery(entry["off"])
    numbers = {"timing_stems": timing_stems, "hits": counts, "windows": windows_summary(ctx), "questions": intent_questions(recipe, song, ctx),
               "notes": source_order(recipe, ctx.notes_summary), "current": current, "records": records, "contracts": contract}
    (cache / "report.json").write_text(json.dumps(numbers, indent=1))
    (cache / "report.md").write_text(markdown(numbers))
    return cache / "report.md", numbers, variants


def intent_questions(recipe, song, ctx):
    """Inputs the mixer writes that decide moves: synth roles, and section intents, checked against the original song."""
    found = {f"role:{name}": f"source {name} is labelled a {source.get('role')} synth, which decides the synth moves it gets"
             for name, source in ctx.sources.items() if source.get("chain") == "synths"}
    found |= {f"hits:{name}": f"tom {name} has {len(ctx.times.get(name, []))} own hits, so its hit-based checks are skipped; is it really unplayed?"
              for name in ctx.sources if ctx.too_few_hits(name)}
    sections = recipe.get("sections", {})
    if not sections:
        return found
    references = sorted(p for p in (song / "references").glob("*") if p.suffix.lower() in (".wav", ".flac", ".aif", ".aiff")) \
        if (song / "references").exists() else []
    original = None
    for path in references:
        audio, rate = soundfile.read(path, always_2d=True)
        if rate == ctx.rate:
            original = measure.sections(audio, rate, sections)["sections"]
            break
    for name, section in sections.items():
        intent = f"section {name}: group {section.get('group')}, loudness_lu {section.get('loudness_lu')}"
        if original is None:
            found[f"section:{name}"] = f"{intent}; no original song in references/ to check it against"
        elif "loudness_lu" in section and original[name].get("error_lu") is not None and abs(original[name]["error_lu"]) > SECTION_INTENT_LU:
            found[f"section:{name}"] = f"{intent}; the original song has it {original[name]['error_lu']:+.2f} LU off that"
    return found


def windows_summary(ctx):
    out = {}
    for source in list(ctx.sources) + ["drum-bus"]:
        fast = ctx.window("fast", source)
        uniform = ctx._windows.get(("fast", source), {}) or {}
        out[source] = None if fast is None else {name: round(float(ctx.window(name, source).sum()) / ctx.rate, 1) for name in ("fast", "sparse")} | {"uniform": bool(uniform.get("uniform"))}
    return out


def source_order(recipe, summary):
    # Contracts run in cache-key order, which says nothing about the song, so the report lists sources as the recipe does.
    order = {source: i for i, source in enumerate(recipe.get("sources", {}))}
    return dict(sorted(summary.items(), key=lambda item: order[item[0]]))


def notes_tool_summary(found):
    import notes as note_tools

    return {"performances": [note_tools.summary(p) for p in found["performances"]]}


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
    lines += ["## Move outcomes", "", "| move | source | decision | default outcome | needed | worked |", "|---|---|---|---|---|---|"]
    lines += [f"| {c['move']} | {c['source']} | {c['decision']} | {c['outcome']} | {c['needed']} | {c['worked']} |" for c in numbers["contracts"]]
    lines.append("")
    for c in numbers["contracts"]:
        lines += [f"### {c['move']} on {c['source']}: {c['decision']}, default {c['outcome']}", "",
                  f"Needed if read on: {c['needed_read_on']}; with: {c['with']}; without: {c['without']}.", ""]
        lines += [f"- fail: {fail}" for fail in c["fails"]]
        lines += [f"- {leaf['id']} {leaf['text']}: {leaf['state']}" + (f" ({leaf['value']} {leaf.get('test', '')})" if leaf.get("value") is not None else "")
                  for leaf in c["leaves"]]
        lines += [f"- engaged {step}: residual {value} dB" for step, value in c["engaged"].items()]
        if c.get("override"):
            lines.append(f"- override: {c['override'].get('reason')} (leaves {c['override'].get('leaves')})")
        lines.append("")
    lines += [f"Fast and sparse passages (seconds): {json.dumps(numbers['windows'])}", "",
              f"Notes: {json.dumps(numbers['notes'])}", ""]
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
