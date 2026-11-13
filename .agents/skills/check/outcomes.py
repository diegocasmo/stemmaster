import math
import sys
from pathlib import Path

import numpy as np
import pyloudnorm
from scipy import ndimage, signal

ROOT = Path(__file__).resolve().parents[3]
for skill in ("render", "analyze", "align"):
    sys.path.insert(0, str(ROOT / ".agents" / "skills" / skill))
import render
import hits as hit_tools
import measure
import notes as note_tools
from analyze import BAND_CENTERS_HZ
from memo import memoized

import contracts
from structure import DRUM_CHAINS, Routing, roles_of

TRUE, FALSE, UNRESOLVED, REVIEW, SKIPPED = "true", "false", "unresolved", "review", "skipped"
# A section the part only rings into or picks up out of: too short to read, too real to leave out.
EDGE = "edge"


class Review:
    """A measure's reading that only a reviewer can settle, with what it could not read."""

    def __init__(self, value, why, floor=False):
        # floor: what could not be read can only raise the value, so a value that already decides the leaf stands.
        self.value, self.why, self.floor = value, why, floor
ON_MS, OFF_MS = (0, 100), (-20, 200)
ONSET_MS = (0, 25)
FRAME_MS = 5
WINDOW_S, HOP_S = 4, 1
MIN_WINDOW_EVENTS, MIN_WINDOWS, FAST_OVER_SPARSE = 3, 3, 1.5
MIN_SPREAD_EVENTS = 8
MIN_FEW_HITS = 3
DAMAGE_DB = 1
DECAY_FLOOR_DB, DECAY_LOSS_DB, DECAY_LOST_SHARE = 6, 3, 0.05
INEFFECTIVE_DB = -40
SIDE_FLOOR_DB = -60
NATIVE_MIN_DB = 0.1
ENGAGE_MAX_LAG = 4096


def db(value):
    return 10 * math.log10(value) if value is not None and value > 0 else None


def mean_db(power, mask=None):
    selected = power if mask is None else power[mask]
    return db(float(selected.mean())) if len(selected) else None


def compare(value, op, target):
    return {">": value > target, ">=": value >= target, "<": value < target, "<=": value <= target, "==": abs(value - target) < 1e-9}[op]


def engagement(before, after, rate, align=True):
    if before.shape != after.shape:
        width = min(before.shape[1], after.shape[1])
        before, after = before[:, :width], after[:, :width]
    if not np.any(after) or not np.any(before):
        return {"residual_db": None if not np.any(after) else 0.0}
    size = 1 << int(np.ceil(np.log2(2 * len(before))))
    # Per channel, then summed: summing the channels first cancels anti-phase stereo to nothing.
    corr = sum(np.fft.irfft(np.fft.rfft(after[:, c], size) * np.conj(np.fft.rfft(before[:, c], size)), size)
               for c in range(before.shape[1]))
    corr = np.concatenate([corr[-ENGAGE_MAX_LAG:], corr[: ENGAGE_MAX_LAG + 1]])
    # A delay's timing change is its effect, not latency to compensate.
    lag = int(np.argmax(np.abs(corr))) - ENGAGE_MAX_LAG if align else 0
    shifted = np.zeros_like(before)
    if lag >= 0:
        shifted[lag:] = before[: len(before) - lag]
    else:
        shifted[:lag] = before[-lag:]
    # Over the whole song and every channel, so a ride on one quiet section or a side-only EQ still counts.
    a, b = shifted.ravel(), after.ravel()
    gain = float(a @ b / max(a @ a, 1e-30))
    residual = float(((b - gain * a) ** 2).sum() / max((b**2).sum(), 1e-30))
    return {"residual_db": round(10 * math.log10(max(residual, 1e-30)), 2), "lag": lag, "gain_db": db(gain * gain),
            "curve": gain_curve(shifted, after, rate)}


def gain_curve(before, after, rate):
    """The gain a step applies, in 5 ms frames of its own aligned input and output (NaN where the input is silent)."""
    frame = round(FRAME_MS / 1000 * rate)
    usable = len(before) // frame * frame
    pin = (before[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    pout = (after[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    curve = np.full(len(pin), np.nan, dtype=np.float32)
    live = pin > pin.max() * 1e-6
    curve[live] = 10 * np.log10(np.maximum(pout[live], 1e-30) / pin[live])
    return curve


def gain_change(before, after, rate):
    """A Ride or Duck changes level only: engaged when its gain moves at least 0.1 dB anywhere the input is not silent."""
    frame = round(FRAME_MS / 1000 * rate)
    usable = len(before) // frame * frame
    pin = (before[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    pout = (after[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    live = pin > pin.max() * 1e-6
    change = np.abs(10 * np.log10(np.maximum(pout[live], 1e-30) / np.maximum(pin[live], 1e-30)))
    largest = float(change.max()) if len(change) else 0.0
    return {"residual_db": 0.0 if largest >= NATIVE_MIN_DB else None, "max_change_db": round(largest, 2)}


def duck_depth(before, after, rate):
    """A Duck's typical cut while it acts: the median level drop over the frames where it cuts at least 0.5 dB."""
    frame = round(FRAME_MS / 1000 * rate)
    usable = len(before) // frame * frame
    pin = (before[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    pout = (after[:usable] ** 2).sum(axis=1).reshape(-1, frame).mean(axis=1)
    live = pin > pin.max() * 1e-6
    cut = 10 * np.log10(np.maximum(pin[live], 1e-30) / np.maximum(pout[live], 1e-30))
    acting = cut[cut >= 0.5]
    return gain_change(before, after, rate) | {"depth_db": round(float(np.median(acting)), 2) if len(acting) else 0.0}


class Context:

    def __init__(self, recipe, song, nodes, routing, times):
        self.recipe, self.song, self.nodes, self.routing = recipe, song, nodes, routing
        self.rate, self.length = render.timeline(recipe, song)
        self.sources = recipe.get("sources", {})
        self.times = times
        # A synth plays its source's role (bass, lead or pad) on every stem, so the role's own moves can read it.
        self.roles = {name: {stem: source["role"] for stem in source.get("stems", [])} if source.get("chain") == "synths"
                      and source.get("role") else roles_of(source) for name, source in self.sources.items()}
        self.notes = {}
        self.notes_read = []
        self.notes_summary = {}
        self._windows = {}
        # Set while one record is read: its shared bus, if its steps act past its own node, and the recipe
        # (with the move on) whose routing decides where its dry and wet paths sum.
        self.acting = None
        self.join_recipe = None
        self.record = None
        self.trial_ids = []
        self.frozen = {}
        profile = ROOT / "profiles" / str(recipe.get("profile")) / "profile.json"
        import json

        self.target = json.loads(profile.read_text()).get("master_integrated_lufs") if profile.exists() else None

    def too_few_hits(self, source):
        return self.sources.get(source, {}).get("chain") == "toms" and len(self.times.get(source, [])) < MIN_FEW_HITS

    def chain_sources(self, chain):
        # A bass synth is part of the bass wherever another move reads the bass.
        return [name for name, source in self.sources.items() if source.get("chain") == chain
                or (chain == "bass" and source.get("chain") == "synths" and source.get("role") == "bass")]

    def merged(self, lists):
        times = np.sort(np.concatenate([t for t in lists if t is not None and len(t)])) if any(
            t is not None and len(t) for t in lists) else np.array([], dtype=int)
        if not len(times):
            return times
        keep = np.concatenate([[True], np.diff(times) >= 0.03 * self.rate])
        return times[keep]

    def source_notes(self, source):
        """Notes of each played stem, found on that stem itself (DIs are never used)."""
        if source not in self.notes:
            import soundfile

            found = []
            for stem in self.sources[source]["stems"]:
                audio, _ = soundfile.read(self.song / "stems" / stem, always_2d=True)
                placed = render.place(audio, self.recipe["stems"][stem].get("offset_samples", 0), self.length)
                found.append(note_tools.measure(placed, self.rate, note_tools.onsets(placed, self.rate)))
            self.notes[source] = {"performances": found}
        # Listed on every read, not only the first: a cached contract must replay every source it reads. A failed
        # read raises before this, so it adds nothing to the summary, as a cold run would.
        if source not in self.notes_read:
            self.notes_read.append(source)
        return self.notes[source]

    def events(self, name, source):
        chain = self.sources.get(source, {}).get("chain")
        if name == "own":
            if source in self.times:
                return self.times[source]
            if chain in ("bass", "guitars", "synths"):
                return self.merged([np.array([n["start"] for n in p]) for p in self.source_notes(source)["performances"]])
            return self.events("drums", source)
        if name == "notes":
            return self.events("own", source)
        if name in ("kick", "snare"):
            return self.merged([self.times.get(s) for s in self.chain_sources(name)])
        if name == "drums":
            return self.merged([self.events("kick", source), self.events("snare", source)])
        if name == "shells":
            return self.merged([self.events("drums", source)] + [self.times.get(s) for s in self.chain_sources("toms")])
        raise ValueError(name)

    def window(self, name, source):
        if name in (None, "song"):
            return np.ones(self.length, dtype=bool)
        key = (name, source)
        if key not in self._windows:
            self._windows[key] = self._fast_sparse(source)
        masks = self._windows[key]
        return None if masks is None else masks[name]

    def _fast_sparse(self, source):
        chain = self.sources.get(source, {}).get("chain")
        times = self.events("own" if chain in DRUM_CHAINS else "drums", source)
        window, hop = WINDOW_S * self.rate, HOP_S * self.rate
        starts = np.arange(0, max(1, self.length - window + 1), hop)
        if not len(times):
            return None
        counts = np.searchsorted(times, starts + window) - np.searchsorted(times, starts)
        busy = counts >= MIN_WINDOW_EVENTS
        if busy.sum() < 2 * MIN_WINDOWS:
            return None
        rates = counts[busy]
        fast = busy & (counts >= np.percentile(rates, 90))
        sparse = busy & (counts <= np.percentile(rates, 25))
        if fast.sum() < MIN_WINDOWS or sparse.sum() < MIN_WINDOWS:
            return None
        if counts[fast].mean() < FAST_OVER_SPARSE * counts[sparse].mean():
            # No passage is clearly faster than another, so the busy part of the song is the one steady passage.
            every = np.zeros(self.length, dtype=bool)
            for start in starts[busy]:
                every[start : start + window] = True
            return {"fast": every, "sparse": every, "uniform": True}
        masks = {}
        for label, chosen in (("fast", fast), ("sparse", sparse)):
            mask = np.zeros(self.length, dtype=bool)
            for start in starts[chosen]:
                mask[start : start + window] = True
            masks[label] = mask
        return masks

    def join(self, source):
        """The first point that both the source's node and every bus its sends feed reach: where dry and wet sum."""
        recipe = self.join_recipe or self.recipe
        if source == "drum-bus":
            nodes = [self.nodes[name] for name, src in self.sources.items() if src.get("chain") in DRUM_CHAINS and name in self.nodes]
            stems = [stem for name, src in self.sources.items() if src.get("chain") in DRUM_CHAINS for stem in src["stems"]]
        else:
            nodes = [self.nodes[source]] if source in self.nodes else []
            stems = self.sources.get(source, {}).get("stems", [])
        returns = {send["bus"] for name in stems + nodes for send in self._settings(name, recipe).get("sends", [])}
        if not returns or not nodes:
            return None
        chains = [[node] + self._ancestors(node, recipe) for node in nodes] + [[r] + self._ancestors(r, recipe) for r in returns]
        return next((bus for bus in chains[0] if all(bus in chain for chain in chains[1:])), "mix")

    def own_returns(self, source):
        recipe = self.join_recipe or self.recipe
        stems = self.sources.get(source, {}).get("stems", [])
        node = self.nodes.get(source)
        returns = {send["bus"] for name in stems + [node] if name for send in self._settings(name, recipe).get("sends", [])}
        routing = Routing(recipe)
        return sorted(bus for bus in returns if routing.stems_into(bus) <= set(stems))

    def _settings(self, name, recipe=None):
        recipe = recipe or self.recipe
        return recipe["stems"].get(name) or recipe.get("buses", {}).get(name, {})

    def _ancestors(self, name, recipe=None):
        recipe = recipe or self.recipe
        out, current = [], self._settings(name, recipe).get("bus", "mix")
        while current != "mix":
            out.append(current)
            current = recipe.get("buses", {}).get(current, {}).get("bus", "mix")
        return out + ["mix"]


# A record whose claimed steps sit on paths no single point carries together: its node reading would miss some.
UNREAD = "(no single point)"


def acting_point(recipe, routing, step_ids, node, own_stems, chain_stems=None):
    """The one bus a move's processing acts on when that is not the source's node: a shared bus past it (the tom-bus
    compressor) or a group inside it (a rhythm-guitar bus). Gain steps only change level, so they don't choose it."""
    if node is None:
        return None
    steps = {step.get("id"): step for step in recipe.get("steps", [])}
    targets = {steps[i]["target"] for i in step_ids if i in steps and steps[i]["plugin"] != "Gain"}
    if not targets:
        return None
    # Steps in series (a tom's own compressor, then the tom bus's) are read after the last: the one every other
    # target routes into.
    chains = {t: ([] if t == "mix" else [t] + (routing.path(t) if t in recipe["stems"] else ancestors(recipe, t)))
              for t in targets}
    last = [t for t in targets if all(t in chains[other] for other in targets)]
    if len(last) != 1:
        # Parallel paths: read where they all meet, or at the node if it already sums them; else nothing reads them all.
        first = next(iter(targets))
        meet = next((bus for bus in chains[first] if all(bus in chains[other] for other in targets)), None)
        if meet is None and all(node in chains[t] or t == node for t in targets):
            return None
        if meet is None:
            return UNREAD
        chain_stems = set(chain_stems or own_stems)
        if not routing.stems_into(meet) <= chain_stems:
            # The meeting bus carries other parts (a drums bus): read the sum of its inputs on the claimed paths,
            # when those carry only this chain; else nothing reads the paths alone.
            inputs = sorted({chains[t][chains[t].index(meet) - 1] for t in targets})
            fed = [{name} if name in recipe["stems"] else routing.stems_into(name) for name in inputs]
            return tuple(inputs) if all(reach <= chain_stems for reach in fed) else UNREAD
        last = [meet]
    target = last[0]
    if target in recipe["stems"] or target in (node, "mix"):
        return None
    reach = routing.stems_into(target)
    inside = reach and reach <= set(own_stems)
    past = routing.stems_into(node) <= reach
    return target if inside or past else None


def ancestors(recipe, bus):
    found = []
    while bus != "mix":
        bus = recipe.get("buses", {}).get(bus, {}).get("bus", "mix")
        if bus != "mix":
            found.append(bus)
    return found


def point_names(ctx, point, source, recipe=None):
    if point == "node" and ctx.acting == UNREAD:
        return None
    if point == "node" and isinstance(ctx.acting, tuple):
        return list(ctx.acting) if recipe is None or set(ctx.acting) <= set(render.sources(recipe)) else None
    if point == "node" and ctx.acting and (recipe is None or ctx.acting in render.sources(recipe)):
        return [ctx.acting]
    if point == "node":
        if source == "drum-bus":
            # Without a drums bus the kit is the sum of the drum nodes, which is what a drums bus would carry.
            return [drums_bus(ctx)] if drums_bus(ctx) else point_names(ctx, "chain:kick+snare+toms+overheads", source)
        if source in ("mix-bus", "master"):
            return ["mix"]
        return [ctx.nodes[source]] if source in ctx.nodes else None
    if point == "stems":
        return list(ctx.sources[source]["stems"]) if source in ctx.sources else None
    if point in ("rhythm", "lead"):
        return rhythm_path(ctx, source, point, recipe)
    if point.startswith("stem:"):
        found = [stem for stem, role in ctx.roles.get(source, {}).items() if role == point[5:]]
        return found or None
    if point in ("mix", "trial", "premaster", "master-in"):
        return [point]
    if point.startswith("tap:"):
        return [point[4:]]
    if point.startswith("chain:"):
        names = [ctx.nodes[s] for c in point[6:].split("+") for s in ctx.chain_sources(c) if s in ctx.nodes]
        return names or None
    if point == "join":
        bus = ctx.join(source)
        return [bus] if bus else None
    if point == "delivered":
        # The source's node plus the returns only it feeds: its own dry and wet sound, before anything else joins.
        returns = ctx.own_returns(source)
        return [ctx.nodes[source]] + returns if source in ctx.nodes and returns else None
    if point == "rest":
        own = top_level(ctx, point_names(ctx, "node", source) or [])
        inputs = [name for name, settings in render.sources(ctx.recipe).items()
                  if settings.get("bus", "mix") == "mix" and name not in own]
        return inputs or None
    raise ValueError(point)


def rhythm_path(ctx, source, wanted="rhythm", recipe=None):
    """A role's guitars as heard (the rhythms, or the lead): every stem or bus carrying only that role whose output
    leaves into a bus that also carries something else, or into the mix, summed. Parallel buses each count. Read on
    the graph of the render it taps, so a trial's own buses never stand in for the recipe's."""
    recipe = recipe or ctx.join_recipe or ctx.recipe
    rhythm = role_stems(ctx, source, wanted)
    if not rhythm:
        return None
    if wanted == "lead":
        # The lead's own level is its dry path, read on the graph without its sends: a return it feeds (a Delay or
        # reverb) is judged by its own leaf. Where such a return joins the dry path, View reads a render without them.
        recipe = without_sends(recipe, rhythm)
    routing = Routing(recipe)
    nodes = render.sources(recipe)
    exits = []
    for name, settings in nodes.items():
        reach = {name} if name in recipe["stems"] else routing.stems_into(name)
        if not reach or not reach <= rhythm:
            continue
        parent = settings.get("bus", "mix")
        if parent == "mix" or parent not in nodes or not routing.stems_into(parent) <= rhythm:
            exits.append(name)
        # A send carries a scaled copy into a bus with other parts: no single tap holds what the rhythms add there.
        # Leaving it out only lowers the lead, which makes "lead above the rhythms" harder, so the lead reads without it.
        if wanted == "rhythm" and any(not routing.stems_into(send["bus"]) <= rhythm for send in settings.get("sends", [])):
            return None
    return exits or None


def without_sends(recipe, stems):
    """The recipe with every send from a stem or bus carrying only these stems removed."""
    import copy

    routing = Routing(recipe)
    sending = [name for name, settings in render.sources(recipe).items() if settings.get("sends")
               and ({name} if name in recipe["stems"] else routing.stems_into(name) or {None}) <= stems]
    if not sending:
        return recipe
    variant = copy.deepcopy(recipe)
    for name in sending:
        (variant["stems"].get(name) or variant["buses"][name])["sends"] = []
    return variant


def lead_wet_joins(ctx, source, recipe):
    """The recipe without the lead's sends, and the lead-only buses on its dry path that a return it feeds reaches,
    through the return's parents or its sends."""
    lead = role_stems(ctx, source, "lead")
    dry = without_sends(recipe, lead)
    if dry is recipe:
        return None, set()
    dry_routing, routing = Routing(dry), Routing(recipe)
    path = {bus for stem in lead for bus in dry_routing.path(stem) if dry_routing.stems_into(bus) <= lead}
    nodes = render.sources(recipe)
    joined = set()
    for name in nodes:
        if name in recipe["stems"] or name in path or dry_routing.stems_into(name) \
                or not routing.stems_into(name) or not routing.stems_into(name) <= lead:
            continue
        seen, queue = set(), [name]
        while queue:
            node = queue.pop()
            if node in seen or node == "mix":
                continue
            seen.add(node)
            if node in path:
                joined.add(node)
            settings = nodes.get(node, {})
            queue += [settings.get("bus", "mix")] + [send["bus"] for send in settings.get("sends", [])]
    return dry, joined


def lead_dry_render(ctx, source, recipe):
    """The recipe without the lead's sends, when a return the lead feeds joins its dry path, so no tap of this render
    holds the dry lead alone; else None."""
    dry, joined = lead_wet_joins(ctx, source, recipe)
    return dry if joined else None


# Steps whose output scales with their input, so a return mixed in before them can't change what they do to the lead.
LINEAR_PLUGINS = {"Gain", "Ride", "HighpassFilter", "LowpassFilter", "PeakFilter", "LowShelfFilter", "HighShelfFilter"}


def lead_wet_dynamics(ctx, source, recipe):
    """Level-dependent steps a lead return feeds along with the dry lead: there the dry render can't show what the
    return does to the lead's level."""
    _, joined = lead_wet_joins(ctx, source, recipe)
    if not joined:
        return []
    lead = role_stems(ctx, source, "lead")
    routing = Routing(recipe)
    after = set()
    for bus in joined:
        while bus != "mix" and routing.stems_into(bus) <= lead:
            after.add(bus)
            bus = recipe.get("buses", {}).get(bus, {}).get("bus", "mix")
    return sorted(step["id"] for step in recipe.get("steps", []) if step.get("target") in after
                  and step.get("plugin") not in LINEAR_PLUGINS)


def role_stems(ctx, source, wanted):
    """A role's stems: the source's lead, or the rhythms of every guitar source, which the mix hears as one."""
    if wanted == "rhythm":
        return {stem for name in rhythm_sources(ctx, source) for stem, role in ctx.roles[name].items() if role == "rhythm"}
    return {stem for stem, role in ctx.roles.get(source, {}).items() if role == wanted}


def mixed_sends(ctx, source, wanted, recipe=None):
    """The buses carrying other parts that a role's own stems or buses send into."""
    recipe = recipe or ctx.join_recipe or ctx.recipe
    routing = Routing(recipe)
    own = {stem for stem, role in ctx.roles.get(source, {}).items() if role == wanted}
    found = set()
    for name, settings in render.sources(recipe).items():
        reach = {name} if name in recipe["stems"] else routing.stems_into(name)
        if own and reach and reach <= own:
            found |= {send["bus"] for send in settings.get("sends", []) if not routing.stems_into(send["bus"]) <= own}
    return sorted(found)


def top_level(ctx, names):
    """The mix inputs these taps reach, so a ratio against the rest never counts the source twice."""
    out = set()
    for name in names:
        chain = [name] + ctx._ancestors(name)
        out.add(chain[-2] if len(chain) > 1 else name)
    return out


def drums_bus(ctx):
    """The bus carrying every drum stem and nothing else: a bus that also carries the band is not the drums bus."""
    drum_stems = {s for name, src in ctx.sources.items() if src.get("chain") in DRUM_CHAINS for s in src["stems"]}
    if not drum_stems:
        return None
    # On every drum stem's own route, not a parallel return the kit only sends to.
    shared = [bus for bus in ctx.routing.path(sorted(drum_stems)[0]) if all(bus in ctx.routing.path(stem) for stem in drum_stems)]
    return next((bus for bus in shared if ctx.routing.stems_into(bus) == drum_stems), None)


@memoized
def signal_hit_stats(audio, rate, times):
    return hit_tools.measure(audio, rate, times, include_off_hit=False)


@memoized
def signal_lufs(audio, rate):
    value = pyloudnorm.Meter(rate).integrated_loudness(audio)
    return value if math.isfinite(value) else None


class View:

    def __init__(self, ctx, result, master=None, recipe=None):
        self.ctx, self.result, self.rate, self.recipe = ctx, result, result.rate, recipe
        # Renders another recipe's view, for a reading no tap of this render holds.
        self.twin = None
        self.cache = {}
        self.master = master or {}

    def signal(self, name):
        if name in ("mix", "premaster"):
            return self.result.mix
        if name in ("trial", "master-in"):
            # Dip, crest and limiter work mean something only at release loudness: no profile target, no reading.
            return self.master.get(name) if self.master.get("matched") else None
        return self.result.tapped.get(name)

    def audio(self, point, source):
        if point == "lead" and self.twin is not None:
            dry = lead_dry_render(self.ctx, source, self.recipe or self.ctx.recipe)
            if dry is not None:
                return self.twin(dry).audio(point, source)
        names = point_names(self.ctx, point, source, self.recipe)
        if not names:
            return None
        parts = [self.signal(name) for name in names]
        if point == "delivered" and parts[0] is not None:
            # A return the without render lacks (a trial's own bus) adds nothing there.
            parts = [part for part in parts if part is not None]
        if any(part is None for part in parts):
            return None
        return sum(parts) if len(parts) > 1 else parts[0]

    def memo(self, key, compute):
        # "node" means another bus while a record's steps act on a shared bus, so the cache key carries that bus.
        key = (self.ctx.acting, id(self.ctx.join_recipe), id(self.ctx.record)) + key
        if key not in self.cache:
            self.cache[key] = compute()
        return self.cache[key]

    def power(self, point, source, band=None):
        def compute():
            audio = self.audio(point, source)
            if audio is None:
                return None
            filtered = measure.band(audio, self.rate, *(band or (None, None)))
            return (filtered**2).sum(axis=1)
        return self.memo(("power", point, source, band), compute)

    def frames(self, point, source, band=None):
        def compute():
            power = self.power(point, source, band)
            if power is None:
                return None
            frame = round(FRAME_MS / 1000 * self.rate)
            usable = len(power) // frame * frame
            return power[:usable].reshape(-1, frame).mean(axis=1)
        return self.memo(("frames", point, source, band), compute)

    def hit_stats(self, point, source, events, window=None):
        def compute():
            audio = self.audio(point, source)
            times = self.events(events, source, window)
            if audio is None or times is None or not len(times):
                return None
            return signal_hit_stats(audio, self.rate, times)
        return self.memo(("hits", point, source, events, window), compute)

    def events(self, events, source, window=None):
        times = self.ctx.events(events or "own", source)
        if window in (None, "song"):
            return times
        mask = self.ctx.window(window, source)
        if mask is None:
            return None
        return times[mask[np.minimum(times, len(mask) - 1)]]

    def lufs(self, point, source):
        def compute():
            audio = self.audio(point, source)
            if audio is None:
                return None
            return signal_lufs(audio, self.rate)
        return self.memo(("lufs", point, source), compute)


def onset_mask(ctx, times, length, span=ONSET_MS):
    return measure.mask_from(length, times, span, ctx.rate)


# Measures read on one variant. Each returns a number, None (unresolved) or SKIPPED.

def m_stem_count(view, source, args):
    wanted = args.get("roles")
    if wanted == ("rhythm",):
        # The song's rhythms, which split guitar sources share.
        return len(role_stems(view.ctx, source, "rhythm"))
    roles = view.ctx.roles.get(source, {})
    return sum(1 for role in roles.values() if not wanted or role in wanted)


def m_bleed(view, source, args):
    values = []
    times = view.ctx.events("own", source)
    if not len(times):
        return None
    for name in point_names(view.ctx, args.get("point", "node"), source) or []:
        audio = view.signal(name)
        if audio is None:
            return None
        power = (measure.band(audio, view.rate, *(args.get("band") or (None, None))) ** 2).sum(axis=1)
        on = onset_mask(view.ctx, times, len(power), ON_MS)
        off = ~onset_mask(view.ctx, times, len(power), OFF_MS)
        values.append(None if not off.any() else (mean_db(power, off) or -200) - (mean_db(power, on) or -200))
    return max(values) if values and None not in values else None


def m_decay_ms(view, source, args):
    power = view.power(args.get("point", "node"), source)
    times = view.events("own", source, args.get("window"))
    if power is None or times is None or len(times) < MIN_SPREAD_EVENTS:
        return None
    level = hit_tools.envelope_db(power, view.rate)
    found = []
    for start, end in zip(times, list(times[1:]) + [len(level)]):
        segment = level[start:end]
        if not len(segment):
            continue
        peak = int(np.argmax(segment[: round(0.05 * view.rate)]))
        below = np.flatnonzero(segment[peak:] < segment[peak] - 30)
        found.append((below[0] if len(below) else len(segment) - peak) / view.rate * 1000)
    return float(np.median(found)) if found else None


def m_band(view, source, args):
    power = view.power(args.get("point", "node"), source, tuple(args["band"]))
    lufs = view.lufs(args.get("point", "node"), source)
    mask = view.ctx.window(args.get("window"), source)
    if power is None or mask is None or lufs is None:
        return None
    # Against the point's own loudness, so a with/without difference is the band change at equal loudness.
    return (mean_db(power, mask[: len(power)]) or -200) - lufs


def m_band_diff(view, source, args):
    first = m_band(view, source, args)
    second = m_band(view, source, args | {"band": args["other_band"]})
    return None if None in (first, second) else first - second


def hit_value(view, source, args, field, reduce):
    stats = view.hit_stats(args.get("point", "node"), source, args.get("events", "own"), args.get("window"))
    if stats is None:
        return None
    values = stats[field][np.isfinite(stats[field])]
    return reduce(values) if len(values) else None


def m_attack_p50(view, source, args):
    return hit_value(view, source, args, "attack_db", lambda v: float(np.median(v)))


def m_attack_p10(view, source, args):
    return hit_value(view, source, args, "attack_db", lambda v: float(np.percentile(v, 10)))


def m_tail_p50(view, source, args):
    return hit_value(view, source, args, "tail_db", lambda v: float(np.median(v)))


def m_level_p50(view, source, args):
    lufs = view.lufs("mix", source)
    value = hit_value(view, source, args, "level_db", lambda v: float(np.median(v)))
    return None if None in (value, lufs) else value - lufs


def m_level_top(view, source, args):
    return hit_value(view, source, args, "level_db", lambda v: float(v.max() - np.median(v)))


def few_hit_spread(values):
    """p90-p10 with enough hits; with a short fill (a short song may have only a few tom hits) the full range, which is
    stricter; under three hits there is no spread to have."""
    if len(values) >= MIN_SPREAD_EVENTS:
        return float(np.percentile(values, 90) - np.percentile(values, 10))
    return float(values.max() - values.min()) if len(values) >= MIN_FEW_HITS else SKIPPED


def m_spread(view, source, args):
    stats = view.hit_stats(args.get("point", "node"), source, args.get("events", "own"), args.get("window"))
    if stats is None:
        return SKIPPED if not len(view.ctx.events(args.get("events", "own"), source)) else None
    values = stats["level_db"][np.isfinite(stats["level_db"])]
    return few_hit_spread(values)


def m_body_p50(view, source, args):
    power = view.power(args.get("point", "node"), source)
    times = view.events(args.get("events", "own"), source, args.get("window"))
    if power is None or times is None or not len(times):
        return None
    body = onset_mask(view.ctx, times, len(power), (30, 100))
    return (mean_db(power, body) or -200) - (mean_db(power) or -200)


def m_loud_rel(view, source, args):
    if args.get("window") in (None, "song"):
        point, mix = view.lufs(args.get("point", "node"), source), view.lufs("mix", source)
        return None if None in (point, mix) else point - mix
    mask = view.ctx.window(args["window"], source)
    audio, mix = view.audio(args.get("point", "node"), source), view.result.mix
    if mask is None or audio is None or not mask.any():
        return None
    meter = pyloudnorm.Meter(view.rate)
    values = [meter.integrated_loudness(a[: len(mask)][mask[: len(a)]]) for a in (audio, mix)]
    return None if not all(math.isfinite(v) for v in values) else values[0] - values[1]


def onset_energy(view, point, source, band, events):
    power = view.power(point, source, tuple(band) if band else None)
    times = view.events(events, source)
    if power is None or times is None or not len(times):
        return None
    return mean_db(power, onset_mask(view.ctx, times, len(power)))


def m_onset_gap(view, source, args):
    first = onset_energy(view, args.get("point", "node"), source, args["band"], args.get("events", "own"))
    second = onset_energy(view, args["other"], source, args["band"], args.get("events", "own"))
    return None if None in (first, second) else first - second


def m_onset_band(view, source, args):
    value = onset_energy(view, args.get("point", "node"), source, args["band"], args.get("events", "own"))
    total = view.lufs(args.get("point", "node"), source)
    return None if None in (value, total) else value - total


def m_cross_onset(view, source, args):
    first = onset_energy(view, args.get("point", "node"), source, args["band"], args["events"])
    second = onset_energy(view, args.get("point", "node"), source, args["other_band"], args["other_events"])
    return None if None in (first, second) else first - second


def m_band_gap(view, source, args):
    first = view.power(args.get("point", "node"), source, tuple(args["band"]))
    second = view.power(args["other"], source, tuple(args["band"]))
    return None if first is None or second is None else (mean_db(first) or -200) - (mean_db(second) or -200)


def m_overlap(view, source, args):
    first, second = view.audio(args.get("point", "node"), source), view.audio(args["other"], source)
    if first is None or second is None:
        return None
    inside = None
    if args.get("events"):
        times = view.events(args["events"], source)
        if times is None or not len(times):
            return None
        inside = onset_mask(view.ctx, times, len(first))
    low, high = args["band"]
    result = measure.overlap(first, second, view.rate, [(low or 0, high)], inside)
    value = next(iter(result["bands"].values()))["overlap_db"]
    return -200.0 if value is None and result["selected_seconds"] > 0 else value


def m_off_hit_level(view, source, args):
    power = view.power(args.get("point", "node"), source)
    times = view.events(args.get("events", "own"), source)
    mix = view.lufs("mix", source)
    if power is None or times is None or mix is None:
        return None
    off = ~onset_mask(view.ctx, times, len(power), OFF_MS)
    value = mean_db(power, off)
    return None if value is None else value - mix


def m_hit_over_off(view, source, args):
    stats = view.hit_stats(args.get("point", "node"), source, args["events"])
    power = view.power(args.get("point", "node"), source)
    if stats is None or power is None:
        return None
    off = ~onset_mask(view.ctx, view.events(args["events"], source), len(power), OFF_MS)
    level = hit_tools.envelope_db(power, view.rate)
    return float(np.median(stats["level_db"]) - np.median(level[off])) if off.any() else None


def m_st_spread(view, source, args):
    audio = view.audio(args.get("point", "node"), source)
    mask = view.ctx.window(args.get("window"), source)
    if audio is None or mask is None:
        return None
    short, hop = short_term(view, args.get("point", "node"), source)
    starts = np.arange(len(short)) * hop
    inside = short[mask[np.minimum(starts + int(1.5 * view.rate), len(mask) - 1)] & np.isfinite(short)]
    return float(np.percentile(inside, 90) - np.percentile(inside, 10)) if len(inside) >= MIN_SPREAD_EVENTS else None


def short_term(view, point, source, band=None):
    def compute():
        from analyze import short_term_lufs

        audio = view.audio(point, source)
        filtered = measure.band(audio, view.rate, *(band or (None, None)))
        return short_term_lufs(pyloudnorm.Meter(view.rate), filtered, view.rate)
    return view.memo(("short", point, source, band), compute)


def m_valley(view, source, args):
    times = view.events(args.get("events", "kick"), source, args.get("window"))
    audio = view.audio(args.get("point", "mix"), source)
    if times is None or audio is None or len(times) < 2:
        return None
    found = measure.valley(audio, view.rate, times)["valley_db"]
    return found["p50"] if found else None


def m_gap_valley(view, source, args):
    power = view.power(args.get("point", "node"), source)
    times = view.events(args.get("events", "own"), source, args.get("window"))
    if power is None or times is None or len(times) < 2:
        return None
    level = ndimage.uniform_filter1d(power, max(1, round(0.01 * view.rate)))
    values = []
    for start, following in zip(times, times[1:]):
        if following - start < 0.06 * view.rate:
            continue
        peak = level[start : start + round(0.05 * view.rate)].max()
        if peak > 0:
            values.append(10 * math.log10(max(level[start:following - round(0.005 * view.rate)].min(), 1e-20) / peak))
    return float(np.median(values)) if values else None


def sum_gain_parts(view, source, args):
    """Two points' band on the hits, each folded to mono after its pan, as the chain measures sum gain: panning
    apart can't hide a cancel."""
    first, second = view.audio(args.get("point", "node"), source), view.audio(args["other"], source)
    times = view.events(args.get("events", "own"), source)
    if first is None or second is None or times is None or not len(times):
        return None
    inside = onset_mask(view.ctx, times, len(first), ON_MS)
    return [measure.band(x.sum(axis=1, keepdims=True), view.rate, *args["band"])[inside] for x in (first, second)]


def spectrum(view, point, source):
    def compute():
        audio = view.audio(point, source)
        if audio is None:
            return None
        freqs, power = signal.welch(audio.mean(axis=1), view.rate, nperseg=65536)
        return freqs, power
    return view.memo(("spectrum", point, source), compute)


def prominence(freqs, power, low, high):
    """Largest rise of a 1/24-octave spectrum over its own 1-octave average in a band, in dB."""
    edges = 20 * 2 ** (np.arange(0, 24 * 10.5) / 24)
    centers = np.sqrt(edges[:-1] * edges[1:])
    fine = np.array([power[(freqs >= lo) & (freqs < hi)].mean() if ((freqs >= lo) & (freqs < hi)).any() else np.nan
                     for lo, hi in zip(edges[:-1], edges[1:])])
    valid = np.isfinite(fine) & (fine > 0)
    smooth = np.array([np.nanmean(fine[max(0, i - 12) : i + 13]) for i in range(len(fine))])
    excess = 10 * np.log10(np.where(valid, fine, np.nan) / smooth)
    inside = valid & (centers >= low) & (centers < (high or np.inf))
    return float(np.nanmax(excess[inside])) if inside.any() else None


def m_narrow_peak(view, source, args):
    found = spectrum(view, args.get("point", "node"), source)
    return None if found is None else prominence(*found, *args["band"])


def m_octave_peak(view, source, args):
    power = view.power(args.get("point", "node"), source)
    times = view.events("own", source)
    if power is None or not len(times):
        return None
    mask = onset_mask(view.ctx, times, len(power), ON_MS)
    levels = []
    for center in BAND_CENTERS_HZ[1:-1]:
        banded = view.power(args.get("point", "node"), source, (center / math.sqrt(2), center * math.sqrt(2)))
        levels.append(mean_db(banded, mask) or -200)
    return max(levels[i] - max(levels[i - 1], levels[i + 1]) for i in range(1, len(levels) - 1))


def m_band_vs_level_spread(view, source, args):
    stats = view.hit_stats(args.get("point", "node"), source, "own")
    power = view.power(args.get("point", "node"), source, tuple(args["band"]))
    times = view.events("own", source)
    if len(times) < MIN_FEW_HITS:
        return SKIPPED
    if stats is None or power is None:
        return None
    cumulative = np.concatenate([[0.0], np.cumsum(power)])
    reach = round(0.1 * view.rate)
    banded = 10 * np.log10(np.maximum(cumulative[np.minimum(times + reach, len(power))] - cumulative[times], 1e-20))
    level = stats["level_db"]
    # The band's spread among hits of equal level: what remains after the level explains what it can.
    slope, intercept = np.polyfit(level, banded, 1)
    residual = banded - (slope * level + intercept)
    return few_hit_spread(residual)


def m_fast_minus_sparse(view, source, args):
    fast, sparse = view.ctx.window("fast", source), view.ctx.window("sparse", source)
    if fast is None:
        return None
    short, hop = short_term(view, args.get("point", "node"), source, tuple(args["band"]))
    starts = np.arange(len(short)) * hop + int(1.5 * view.rate)
    pick = lambda mask: short[mask[np.minimum(starts, len(mask) - 1)] & np.isfinite(short)]
    a, b = pick(fast), pick(sparse)
    return float(np.median(a) - np.median(b)) if len(a) and len(b) else None


def m_dip(view, source, args):
    master_in, trial = view.signal("master-in"), view.signal("trial")
    times = view.events(args.get("events", "own"), source)
    if master_in is None or trial is None or times is None or not len(times):
        return None
    found = measure.dip(master_in, trial, view.rate, times)["dip_db"]
    return found["p50"] if found else None


def m_dip_both(view, source, args=None):
    values = [m_dip(view, source, {"events": chain}) for chain in ("kick", "snare")]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def m_crest_loss(view, source, args):
    times = view.events(args.get("events", "own"), source)
    before, after = view.signal("premaster"), view.signal("trial")
    if before is None or after is None or not len(times):
        return None
    levels = []
    for audio in (before, after):
        stats = hit_tools.measure(audio, view.rate, times)
        levels.append(float(np.median(stats["level_db"])) - pyloudnorm.Meter(view.rate).integrated_loudness(audio))
    return levels[0] - levels[1]


def profile_values(view):
    def compute():
        trial = view.signal("trial")
        if trial is None or view.ctx.recipe.get("profile") is None:
            return None
        return measure.profile_range(trial, view.rate, view.ctx.recipe["profile"])
    return view.memo(("range",), compute)


def m_range_outside(view, source, args):
    values = profile_values(view)
    if values is None:
        return SKIPPED if view.ctx.recipe.get("profile") is None else None
    return values.get(args["field"], {}).get("outside")


def m_range_outside_abs(view, source, args):
    value = m_range_outside(view, source, args)
    return value if value in (None, SKIPPED) else abs(value)


def m_edge_bands_outside(view, source, args):
    values = [m_range_outside_abs(view, source, {"field": f"bands_db.{b}"}) for b in ("31.5", "63", "16000")]
    if SKIPPED in values:
        return SKIPPED
    values = [v for v in values if v is not None]
    return max(values) if values else None


def m_any_outside(view, source, args):
    values = profile_values(view)
    if values is None:
        return SKIPPED if view.ctx.recipe.get("profile") is None else None
    fields = [f for f in values if f.startswith("bands_db.") or f == "side_mid_db"]
    return max(abs(values[f]["outside"]) for f in fields) if fields else None


def m_side(view, source, args):
    audio = view.audio(args.get("point", "mix"), source)
    if audio is None or audio.shape[1] != 2:
        return None
    side = measure.band((audio[:, 0] - audio[:, 1])[:, None] / 2, view.rate, *args["band"])
    total = view.power(args.get("point", "mix"), source)
    return (db(float((side**2).mean())) or -200) - (mean_db(total) or -200)


def m_side_share(view, source, args):
    """Side energy in a band over the signal's own energy in that band, so a quieter return can't hide a wider one."""
    audio = view.audio(args.get("point", "mix"), source)
    if audio is None or audio.shape[1] != 2:
        return None
    side = measure.band((audio[:, 0] - audio[:, 1])[:, None] / 2, view.rate, *args["band"])
    whole = measure.band(audio, view.rate, *args["band"])
    # Floored, since side energy 60 dB under the band is inaudible and a mono source's would read minus infinity.
    return max(SIDE_FLOOR_DB, db(float((side**2).sum()) / max(float((whole**2).sum()), 1e-30)) or SIDE_FLOOR_DB)


def m_mono_band(view, source, args):
    audio = view.audio(args.get("point", "mix"), source)
    if audio is None:
        return None
    mono = measure.band(audio.mean(axis=1, keepdims=True), view.rate, *args["band"])
    whole = measure.band(audio, view.rate, *args["band"])
    return db(float((mono**2).sum() * audio.shape[1]) / max(float((whole**2).sum()), 1e-30))


def m_mono_lufs(view, source, args):
    audio = view.audio(args.get("point", "mix"), source)
    mix = view.lufs("mix", source)
    if audio is None or mix is None:
        return None
    mono = audio.mean(axis=1, keepdims=True)
    return pyloudnorm.Meter(view.rate).integrated_loudness(np.repeat(mono, 2, axis=1)) - mix


def shared_after(view, point, source, linear_ok=False):
    """Steps and pans after a role's own outputs, on buses it shares with other parts: they can change its left-right
    balance where the role can't be read alone."""
    if point != "rhythm":
        return []
    recipe = view.recipe or view.ctx.recipe
    found = set()
    for name in point_names(view.ctx, point, source, recipe) or []:
        bus = render.sources(recipe).get(name, {}).get("bus", "mix")
        while bus != "mix":
            settings = recipe.get("buses", {}).get(bus, {})
            found |= {step["id"] for step in recipe.get("steps", []) if step.get("target") == bus
                      and not (linear_ok and step.get("plugin") in LINEAR_PLUGINS)}
            if settings.get("pan"):
                found.add(f"{bus} pan")
            bus = settings.get("bus", "mix")
    return sorted(found)


def lr_reading(view, source, args, value):
    # The same linear filter on both sides moves their balance only as far as their tones differ, so it counts only
    # when every octave's share matches within 1 dB.
    tones = lr_tone_diff(view, args.get("point", "node"), source)
    shared = shared_after(view, args.get("point", "node"), source, linear_ok=tones is not None and tones <= 1)
    if value is not None and shared:
        return Review(value, f"the rhythms then pass {shared} on buses shared with other parts, which can move their "
                             f"left-right balance: judge it in the mix")
    return value


def m_lr_loudness(view, source, args):
    audio = view.audio(args.get("point", "node"), source)
    if audio is None or audio.shape[1] != 2:
        return None
    meter = pyloudnorm.Meter(view.rate)
    left, right = (meter.integrated_loudness(np.repeat(audio[:, [c]], 2, axis=1)) for c in (0, 1))
    return lr_reading(view, source, args, abs(left - right))


def m_lr_band_diff(view, source, args):
    found = lr_tone_diff(view, args.get("point", "node"), source)
    return None if found is None else lr_reading(view, source, args, found)


def lr_tone_diff(view, point, source):
    audio = view.audio(point, source)
    if audio is None or audio.shape[1] != 2:
        return None
    diffs = []
    totals = [(audio[:, c] ** 2).mean() for c in (0, 1)]
    for center in BAND_CENTERS_HZ:
        if center * math.sqrt(2) >= view.rate / 2:
            continue
        banded = measure.band(audio, view.rate, center / math.sqrt(2), center * math.sqrt(2))
        sides = [db(float((banded[:, c] ** 2).mean()) / totals[c]) for c in (0, 1)]
        if None not in sides:
            diffs.append(abs(sides[0] - sides[1]))
    return max(diffs) if diffs else None


def section_readings(view, point, source):
    """Per section: short-term loudness over the 3 s windows, anchored at the section's start, centred where the part
    plays. A section the part rests through is left out (None); one it only rings into or picks up out of is EDGE; one
    where it plays with no such window is unread (NaN)."""
    sections = view.ctx.recipe.get("sections")
    audio = view.audio(point, source)
    if not sections or audio is None:
        return None
    cumulative = view.memo(("kcum", point, source), lambda: measure.k_weighted_cumulative(audio, view.rate))
    window, hop = int(measure.SHORT_TERM_SECONDS * view.rate), int(measure.SHORT_TERM_HOP_SECONDS * view.rate)
    found = {}
    for name, section in sections.items():
        first, last = round(section["start_s"] * view.rate), min(round(section["end_s"] * view.rate), len(audio))
        playing = played_mask(view, point, source, first)
        starts = np.arange(first, last - window + 1, hop)
        with np.errstate(divide="ignore"):
            short = -0.691 + 10 * np.log10((cumulative[starts + window] - cumulative[starts]) / window)
        heard = np.isfinite(short) & playing[np.minimum(starts + window // 2, len(playing) - 1)]
        if heard.any():
            found[name] = (section.get("group"), short[heard])
        else:
            found[name] = (section.get("group"), plays_in(playing, first, last, window // 2))
    return found


def plays_in(playing, first, last, edge):
    """How a section with no supported window is read: a rest (None), only a ring-out from the section before or a
    pickup into the next, a played run shorter than half a window that continues across the boundary (EDGE), or
    playing that can't be read (NaN)."""
    inside = playing[first:last].copy()
    if not inside.any():
        return None
    if first > 0 and playing[first - 1]:
        run = np.argmin(inside) if not inside.all() else len(inside)
        if run < edge:
            inside[:run] = False
    if last < len(playing) and playing[last]:
        run = np.argmin(inside[::-1]) if not inside.all() else len(inside)
        if run < edge:
            inside[len(inside) - run:] = False
    return np.array([np.nan]) if inside.any() else EDGE


def played_mask(view, point, source, section_start=None):
    """Where the source plays: within 40 dB of its loudest 100 ms in its own unprocessed stems. The rest of the band
    plays where any other source plays. From a section's start, a note ringing on from before (a run that carries
    over and decays steadily into a rest) is not playing in that section."""
    ctx = view.ctx
    own = own_sources(ctx, source)
    if point == "rest" or len(own) != 1:
        parts = [played_mask(view, "node", other, section_start) for other in ctx.sources if (other in own) != (point == "rest")]
        return np.logical_or.reduce(parts) if parts else np.zeros(ctx.length, dtype=bool)
    key = ("played", source)
    if key not in ctx.frozen:
        import soundfile

        power = np.zeros(ctx.length)
        for name in ctx.sources.get(source, {}).get("stems", []):
            audio, _ = soundfile.read(ctx.song / "stems" / name, always_2d=True)
            power += (render.place(audio, ctx.recipe["stems"][name].get("offset_samples", 0), ctx.length) ** 2).sum(axis=1)
        frame = round(0.1 * ctx.rate)
        usable = len(power) // frame * frame
        levels = 10 * np.log10(np.maximum(power[:usable].reshape(-1, frame).mean(axis=1), 1e-20))
        mask = np.ones(ctx.length, dtype=bool)
        if usable:
            mask[:usable] = np.repeat(levels >= levels.max() - 40, frame)
        ctx.frozen[key] = (mask, levels, frame)
    mask, levels, frame = ctx.frozen[key]
    start = None if section_start is None else section_start // frame
    if not start or start >= len(levels) or not mask[start * frame - 1]:
        return mask
    floor, k = levels[start - 1], start
    while k < len(levels) and mask[k * frame] and levels[k] <= floor + 3:
        floor = min(floor, levels[k])
        k += 1
    # Only a run that dies away into a rest is a ring-out: it reaches the rest before any rise, every 0.5 s of it is
    # at least 1 dB under the one before, and its last 0.5 s is 10 dB under its first. A part that plays on, then
    # stops or lets a last chord ring, is still playing.
    blocks = [levels[i : i + 5].mean() for i in range(start, k - 4, 5)]
    if k < len(levels) and not mask[k * frame] and len(blocks) >= 2 and blocks[-1] <= blocks[0] - 10 \
            and all(later <= earlier - 1 for earlier, later in zip(blocks, blocks[1:])):
        mask = mask.copy()
        mask[start * frame : k * frame] = False
    return mask


def own_sources(ctx, source):
    """The sources a record's part is made of: the drum-bus is the whole kit."""
    if source == "drum-bus":
        return {name for name, src in ctx.sources.items() if src.get("chain") in DRUM_CHAINS}
    return {source}


def section_levels(view, point, source):
    found = section_readings(view, point, source)
    if found is None:
        return None if view.ctx.recipe.get("sections") else {}
    return {name: (group, values if values is None or values is EDGE
                   else float("nan") if np.isnan(values).all() else float(np.median(values)))
            for name, (group, values) in found.items()}


def group_range(levels, what):
    """The largest level change within a section group, over sections the part plays in. A group where the part only
    rings into or picks up out of a section beside another played one is a question for the reviewers."""
    out, edges = {}, {}
    for name, (group, level) in (levels or {}).items():
        # A section the part rests through has no level to compare; one too short for a reading leaves it unsupported.
        if group is None or level is None:
            continue
        if level is EDGE:
            edges.setdefault(group, []).append(name)
        else:
            out.setdefault(group, []).append(level)
    if any(np.isnan(values).any() for values in out.values()):
        return None
    # No group with two played sections: there is no change between equal sections to find.
    value = max((max(v) - min(v) for v in out.values() if len(v) > 1), default=0.0)
    asked = sorted(name for group, names in edges.items() if len(names) + len(out.get(group, [])) > 1 for name in names)
    if asked:
        return Review(value, f"{what} only rings into or picks up out of {asked} (under 1.5 s), too short for a 3 s "
                             f"reading: judge whether its level there needs a ride", floor=True)
    return value


def m_group_ratio_range(view, source, args):
    own = section_levels(view, args.get("point", "node"), source)
    rest = section_levels(view, "rest", source)
    if own is None or rest is None:
        return None
    ratios = {}
    for name, (group, level) in own.items():
        other = rest[name][1]
        if level is None or other is None:
            ratios[name] = (group, None)
        elif level is EDGE or other is EDGE:
            ratios[name] = (group, EDGE)
        else:
            ratios[name] = (group, level - other)
    return group_range(ratios, f"{source} or the rest of the band")


def m_group_swing(view, source, args):
    return group_range(section_levels(view, args.get("point", "node"), source), source)


def m_section_spread(view, source, args):
    found = section_readings(view, args.get("point", "node"), source)
    if not found:
        return None
    spreads = []
    for group, values in found.values():
        if values is None or values is EDGE:
            continue
        if np.isnan(values).all():
            return None
        spreads.append(float(np.percentile(values, 90) - np.percentile(values, 10)))
    return max(spreads) if spreads else None


def m_section_error(view, source, args):
    sections = view.ctx.recipe.get("sections")
    if not sections or not all("loudness_lu" in s for s in sections.values()):
        return None
    errors = [e.get("error_lu") for e in measure.sections(view.result.mix, view.rate, sections)["sections"].values()]
    # A section too short for a 3 s window has no reading, which leaves every-section checks unsupported.
    return None if None in errors or not errors else max(abs(e) for e in errors)


def m_peak_to_loudness(view, source, args):
    audio = view.audio(args.get("point", "node"), source)
    lufs = view.lufs(args.get("point", "node"), source)
    return None if audio is None or lufs is None else render.true_peak_dbtp(audio) - lufs


def note_list(view, source, window=None):
    notes = [n for p in view.ctx.source_notes(source)["performances"] for n in p]
    if window not in (None, "song"):
        mask = view.ctx.window(window, source)
        if mask is None:
            return None
        notes = [n for n in notes if mask[min(n["start"], len(mask) - 1)]]
    return notes


def note_levels(view, source, args, notes):
    power = view.power(args.get("point", "node"), source, tuple(args["band"]) if args.get("band") else None)
    if power is None:
        return None
    cumulative = np.concatenate([[0.0], np.cumsum(power)])
    out = []
    for n in notes:
        if "level_db" not in n:
            continue
        end = min(n["end"], n["start"] + round(0.1 * view.rate))
        out.append(10 * math.log10(max((cumulative[end] - cumulative[n["start"]]) / max(end - n["start"], 1), 1e-20)))
    return np.array(out)


def m_note_spread(view, source, args):
    notes = note_list(view, source, args.get("window"))
    if notes is None:
        return None
    levels = note_levels(view, source, args, notes)
    if levels is None or len(levels) < MIN_SPREAD_EVENTS:
        return None
    return float(np.percentile(levels, 90) - np.percentile(levels, 10))


def m_note_top(view, source, args):
    levels = note_levels(view, source, args, note_list(view, source))
    return None if levels is None or len(levels) < MIN_SPREAD_EVENTS else float(levels.max() - np.median(levels))


def m_note_attack(view, source, args):
    power = view.power(args.get("point", "node"), source)
    if power is None:
        return None
    cumulative = np.concatenate([[0.0], np.cumsum(power)])
    values = []
    for n in note_list(view, source):
        if "attack_db" not in n:
            continue
        head = cumulative[n["start"] + round(0.025 * view.rate)] - cumulative[n["start"]]
        body_end = min(n["end"], n["start"] + round(0.1 * view.rate))
        body = cumulative[body_end] - cumulative[n["start"] + round(0.03 * view.rate)]
        if body > 0 and head > 0:
            values.append(10 * math.log10(head / body))
    return float(np.median(values)) if len(values) >= MIN_SPREAD_EVENTS else None


def lowest_note(view, source):
    f0s = sorted(n["f0_hz"] for n in note_list(view, source) if "f0_hz" in n)
    # The lowest pitch seen in at least three notes, so one octave error can't set it.
    for value in f0s:
        if sum(1 for other in f0s if abs(12 * math.log2(other / value)) < 0.5) >= 3:
            return value
    return None


def m_below_lowest_note(view, source, args):
    f0 = lowest_note(view, source)
    if f0 is None:
        return None
    below = m_band(view, source, {"band": (0, f0 / 1.12)})
    at = m_band(view, source, {"band": (f0 / 1.12, f0 * 1.12)})
    return None if None in (below, at) else below - at


def tom_f0(view, source):
    """The tom's fundamental, found once on its unprocessed stem, so a filter that removes it can't move it."""
    ctx = view.ctx
    if ("tom_f0", source) not in ctx.frozen:
        import soundfile

        stem = ctx.sources[source]["stems"][0]
        audio, _ = soundfile.read(ctx.song / "stems" / stem, always_2d=True)
        audio = render.place(audio, ctx.recipe["stems"][stem].get("offset_samples", 0), ctx.length)
        times = ctx.events("own", source)
        value = None
        if len(times):
            mask = onset_mask(ctx, times, len(audio), (0, 150))
            freqs, power = signal.welch(audio.mean(axis=1)[mask], ctx.rate, nperseg=16384)
            inside = (freqs >= 50) & (freqs <= 400)
            value = float(freqs[inside][np.argmax(power[inside])])
        ctx.frozen[("tom_f0", source)] = value
    return ctx.frozen[("tom_f0", source)]


def m_below_f0(view, source, args):
    f0 = tom_f0(view, source)
    if f0 is None:
        return None
    below = onset_energy(view, "node", source, (0, f0 / 1.2), "kick")
    at = onset_energy(view, "node", source, (f0 / 1.2, f0 * 1.2), "own")
    return None if None in (below, at) else below - at


def m_f0_band(view, source, args):
    f0 = tom_f0(view, source)
    if f0 is None:
        return None
    return m_onset_band(view, source, {"band": (f0 / 1.2, f0 * 1.2), "events": "own"})


def m_off_hit_band(view, source, args):
    point = args.get("point", "node")
    power = view.power(point, source, tuple(args["band"]))
    total = view.power(point, source)
    times = view.events(args.get("events", "drums"), source)
    if power is None or times is None:
        return None
    off = ~onset_mask(view.ctx, times, len(power), OFF_MS)
    if any(role == "lead" for role in view.ctx.roles.get(source, {}).values()):
        lead = active_mask(view, "stem:lead", source)
        if lead is None:
            return None
        off &= ~lead[: len(off)]
    # Against the mix over the same passage, so a broad cut on the part can't cancel itself out of the guard.
    mix = view.power("mix", source)
    return (mean_db(power, off) or -200) - (mean_db(mix, off[: len(mix)]) or -200)


def active_mask(view, point, source):
    """Where a part plays, found once on its unprocessed stems so processing can't move it: 100 ms frames within
    20 dB of the part's loudest frames, each marking only its own time."""
    ctx = view.ctx
    names = point_names(ctx, point, source)
    if not names or any(name not in ctx.recipe["stems"] for name in names):
        return None
    key = ("active", tuple(names))
    if key not in ctx.frozen:
        import soundfile

        power = np.zeros(ctx.length)
        for name in names:
            audio, _ = soundfile.read(ctx.song / "stems" / name, always_2d=True)
            power += (render.place(audio, ctx.recipe["stems"][name].get("offset_samples", 0), ctx.length) ** 2).sum(axis=1)
        frame = round(0.1 * ctx.rate)
        usable = len(power) // frame * frame
        levels = 10 * np.log10(np.maximum(power[:usable].reshape(-1, frame).mean(axis=1), 1e-20))
        mask = np.zeros(ctx.length, dtype=bool)
        mask[:usable] = np.repeat(levels >= np.percentile(levels, 99.9) - 20, frame)
        ctx.frozen[key] = mask if mask.any() else None
    return ctx.frozen[key]


def rhythm_sources(ctx, source):
    """Whose rhythms a lead sits above: every guitar source's, its own included."""
    return [name for name, src in ctx.sources.items() if src.get("chain") == "guitars" and "rhythm" in ctx.roles.get(name, {}).values()]


def m_lead_gap(view, source, args):
    ctx = view.ctx
    if "lead" not in ctx.roles.get(source, {}).values() or not role_stems(ctx, source, "rhythm"):
        return None
    mask = active_mask(view, "stem:lead", source)
    a, b = view.power("lead", source, tuple(args["band"])), view.power("rhythm", source, tuple(args["band"]))
    if mask is None or a is None or b is None:
        return None
    value = (mean_db(a, mask) or -200) - (mean_db(b, mask) or -200)
    dynamics = lead_wet_dynamics(ctx, source, view.recipe or ctx.recipe)
    if dynamics:
        return Review(value, f"the lead's returns feed {dynamics} along with the dry lead, so the dry render can't show "
                             f"how they move the lead's level: judge the lead's level in the mix")
    shared = mixed_sends(ctx, source, "lead", view.recipe)
    if shared:
        return Review(value, f"the lead also sends to {shared}, which carry other parts, so its level there is not "
                             f"counted (a delayed copy can add or cancel): judge the lead's level in the mix")
    return value


def main_octave(ctx, source):
    """The source's strongest octave, found once on its unprocessed stems, so a filter that removes it can't move it."""
    if ("main", source) not in ctx.frozen:
        import soundfile

        power = 0.0
        for stem in ctx.sources[source]["stems"]:
            audio, _ = soundfile.read(ctx.song / "stems" / stem, always_2d=True)
            placed = render.place(audio, ctx.recipe["stems"][stem].get("offset_samples", 0), ctx.length)
            # Powers add, not signals: summing first would let stereo or polarity cancellation hide the main band.
            for channel in placed.T:
                freqs, spectrum = signal.welch(channel, ctx.rate, nperseg=16384)
                power = power + spectrum
        energy = [power[(freqs >= c / math.sqrt(2)) & (freqs < c * math.sqrt(2))].sum() for c in BAND_CENTERS_HZ]
        ctx.frozen[("main", source)] = BAND_CENTERS_HZ[int(np.argmax(energy))]
    center = ctx.frozen[("main", source)]
    return center / math.sqrt(2), center * math.sqrt(2)


def m_main_band(view, source, args):
    return m_band(view, source, {"band": main_octave(view.ctx, source)})


def m_main_band_gap(view, source, args):
    return m_band_gap(view, source, {"other": args["other"], "band": main_octave(view.ctx, source)})


def m_tail_vs_drums(view, source, args):
    own = m_tail_p50(view, source, {"events": "notes"})
    drums = m_tail_p50(view, source, {"point": "chain:kick+snare+toms+overheads", "events": "drums"})
    return None if None in (own, drums) else own - drums


def m_pad_attack_gap(view, source, args):
    if view.ctx.sources.get(source, {}).get("role") != "pad":
        return -1.0
    first = onset_energy(view, "node", source, (None, None), "notes")
    second = onset_energy(view, args["other"], source, (None, None), "notes")
    return None if None in (first, second) else first - second


def m_limiter_work(view, source, args):
    master_in, trial = view.signal("master-in"), view.signal("trial")
    if master_in is None or trial is None:
        return None
    meter = pyloudnorm.Meter(view.rate)
    crest = lambda a: render.true_peak_dbtp(a) - meter.integrated_loudness(a)
    return crest(master_in) - crest(trial)


def actual_master(view):
    """The master the recipe itself exports, with no drive added to reach the target."""
    return view.memo(("actual",), lambda: view.result.master(view.result.mix)[1] if view.result.master_steps else None)


def m_true_peak(view, source, args):
    master = actual_master(view)
    return None if master is None else render.true_peak_dbtp(master)


def m_loudness_error(view, source, args):
    master = actual_master(view)
    if master is None or view.ctx.target is None:
        return None
    return abs(pyloudnorm.Meter(view.rate).integrated_loudness(master) - view.ctx.target)


def blend_pairs(ctx, source):
    roles = ctx.roles.get(source, {})
    chain = ctx.sources.get(source, {}).get("chain")
    stems = ctx.sources[source]["stems"]
    if chain == "guitars":
        # Which tracks are one take isn't in the audio: the record names them, the reference first.
        take = (ctx.record or {}).get("take", [])
        return [(take[0], stem) for stem in take[1:]]
    reference = ctx.sources[source].get("timing") or stems[0]
    return [(reference, stem) for stem in stems if stem != reference]


def m_take_sum(view, source, args):
    """How alike each pair the record names as one take is at its best offset (normalized correlation, so level
    doesn't count): one performance reads well above 0, separate takes near it. The lowest pair counts."""
    import align

    pairs = blend_pairs(view.ctx, source)
    notes = view.ctx.events("own", source)
    if not pairs or len(notes) < MIN_SPREAD_EVENTS:
        return SKIPPED if (view.ctx.record or {}).get("decision") == "dropped" else None
    found = [align.pair(view.ctx.recipe, view.ctx.song, a, b, *args["band"], align.LIMIT_MS, None, notes) for a, b in pairs]
    return min(max(f["normal"]["best_correlation"], f["inverted"]["best_correlation"]) for f in found)


def m_note_sum_gain_spread(view, source, args):
    """How much the tracks' sum gain in a band moves from note to note (p90 minus p10), at their recipe offsets,
    polarities and fixed levels: a blend that sums on some notes and cancels on others."""
    import align

    stems = view.ctx.sources.get(source, {}).get("stems", [])
    notes = view.ctx.events("own", source)
    if len(stems) < 2 or notes is None or len(notes) < MIN_SPREAD_EVENTS:
        return None
    width = round(align.WINDOW_MS / 1000 * view.rate)
    # On the mono sum, after each track's pan, as the chain measures sum gain: panning apart can't hide a cancel.
    recipe = with_gain_steps(view.recipe or view.ctx.recipe, set(stems))
    banded = [align.windows(align.band(align.placed(recipe, view.ctx.song, stem, stereo=True)[0]
                                       .sum(axis=1), view.rate, *args["band"]), notes, width) for stem in stems]
    each = sum((w**2).sum(axis=1) for w in banded)
    total = (sum(banded) ** 2).sum(axis=1)
    gains = 10 * np.log10(np.maximum(total, 1e-30) / np.maximum(each, 1e-30))[each > 0]
    return float(np.percentile(gains, 90) - np.percentile(gains, 10)) if len(gains) else None


def with_gain_steps(recipe, stems):
    """The recipe with each stem's Gain steps, and those on buses on its own route carrying only that stem, folded into
    its gain_db: the level each mic reaches the blend at, so moving level into a Gain step can't change the verdict.
    A return the stem only sends to is another path, not its level."""
    import copy

    routing = Routing(recipe)
    variant = copy.deepcopy(recipe)
    for step in recipe.get("steps", []):
        target = step.get("target")
        if step.get("plugin") != "Gain" or target == "mix":
            continue
        reach = {target} if target in recipe["stems"] else routing.stems_into(target)
        if len(reach) == 1 and reach <= stems and (target in recipe["stems"] or target in routing.path(next(iter(reach)))):
            stem = next(iter(reach))
            variant["stems"][stem]["gain_db"] = variant["stems"][stem].get("gain_db", 0) + step["settings"].get("gain_db", 0)
    return variant


def m_blend_shortfall(view, source, args):
    import align

    pairs = blend_pairs(view.ctx, source)
    chain = view.ctx.sources.get(source, {}).get("chain")
    if not pairs:
        # A dropped guitars.align names no take, so there is nothing to check; a kept one must name its take.
        return SKIPPED if chain == "guitars" and (view.ctx.record or {}).get("decision") == "dropped" else None
    timing = view.ctx.sources.get(source, {}).get("timing")
    notes = view.ctx.events("own", source) if chain in ("bass", "guitars") else None
    if notes is not None and len(notes) < MIN_SPREAD_EVENTS:
        return None
    recipe, worst, options = with_gain_steps(view.ctx.recipe, {name for pair in pairs for name in pair}), None, []
    for reference, other in pairs:
        found = align.pair(recipe, view.ctx.song, reference, other, *args["band"], align.LIMIT_MS, timing, notes)
        if found["recipe_sum_gain_db"] is None or not found["hits"]:
            return None
        best = max(found["normal"]["best_sum_gain_db"], found["inverted"]["best_sum_gain_db"])
        shortfall = best - found["recipe_sum_gain_db"]
        worst = shortfall if worst is None else max(worst, shortfall)
        now = recipe["stems"][other]
        options += [(other, now.get("offset_samples", 0), not now.get("invert", False))]
        options += [(other, found[k]["best_offset_samples"], k == "inverted") for k in ("normal", "inverted")]
    # The whole blend too: each mic's other polarity and its best pairwise options, the rest kept, at fixed levels.
    # Mics that cancel each other while each sums with the reference only show here.
    names = list(dict.fromkeys(name for pair in pairs for name in pair))
    if len(names) > 2:
        import copy

        whole = lambda r: align.blend(r, view.ctx.song, names, *args["band"], timing, notes)["sum_gain_db"]
        current = whole(recipe)
        for stem, offset, invert in options:
            variant = copy.deepcopy(recipe)
            variant["stems"][stem].update(offset_samples=offset, invert=invert)
            worst = max(worst, whole(variant) - current)
    return worst


MEASURES = {name[2:]: value for name, value in dict(globals()).items() if name.startswith("m_")}


# Measures that need the with and the without variant at once.

def frames_of(view, point, source, band=None):
    return view.frames(point, source, band)


def p_matched_sum_gain(on, off, source, args):
    """The change in sum gain with the move, at the levels the mix ends with: the points without the move are scaled to
    their energy with it, so a level change reads 0 and a band the move removes stops counting; only timing,
    polarity and phase change it."""
    now, before = sum_gain_parts(on, source, args), sum_gain_parts(off, source, args)
    if now is None or before is None:
        return None
    gain = lambda a, b: db(float(((a + b) ** 2).sum()) / max(float((a**2).sum() + (b**2).sum()), 1e-30))
    matched = [y * math.sqrt(float((x**2).sum()) / float((y**2).sum())) if (y**2).sum() > 0 else y for x, y in zip(now, before)]
    a, b = gain(*now), gain(*matched)
    return None if None in (a, b) else a - b


def p_damaged_hits(on, off, source, args):
    times = on.events(args.get("events", "own"), source)
    a = on.hit_stats(args.get("point", "node"), source, args.get("events", "own"))
    b = off.hit_stats(args.get("point", "node"), source, args.get("events", "own"))
    if a is None or b is None or not len(times):
        return None
    level, attack = a["level_db"] - b["level_db"], a["attack_db"] - b["attack_db"]
    return int(np.sum((level < -DAMAGE_DB) | (attack < -DAMAGE_DB)))


def p_decay_lost(on, off, source, args):
    """Hits whose own decay, above the bleed floor and up to the next hit, loses more than 3 dB in over 5% of its frames."""
    a, b = frames_of(on, "node", source), frames_of(off, "node", source)
    times = on.events("own", source)
    if a is None or b is None or not len(times):
        return None
    frame = round(FRAME_MS / 1000 * on.rate)
    starts = times // frame
    hit = np.zeros(len(b), dtype=bool)
    for start in starts:
        hit[max(0, start - 4) : start + 40] = True
    floor = 10 * np.log10(max(np.median(b[~hit]) if (~hit).any() else 1e-20, 1e-20)) + DECAY_FLOOR_DB
    lost = 0
    for start, end in zip(starts, list(starts[1:]) + [len(b)]):
        span = slice(start + 1, end)
        level = 10 * np.log10(np.maximum(b[span], 1e-20))
        own = level > floor
        if own.sum() < 2:
            continue
        loss = level[own] - 10 * np.log10(np.maximum(a[span][own], 1e-20))
        if (loss > DECAY_LOSS_DB).mean() > DECAY_LOST_SHARE:
            lost += 1
    return lost


def p_recovery(on, off, source, args):
    """Gain each compressor still takes 25-5 ms before each next hit in the fast passage, against its idle gain.

    Read on each step's own input and output in the render, so gain before or after it on the path can't hide it;
    the worst step counts.
    """
    times = on.events("own", source, "fast")
    steps = [step for step in judged_steps(on.ctx) if step["plugin"] not in render.NATIVE and step["plugin"] != "Gain"]
    if not steps or times is None or len(times) < 2:
        return None
    frame = round(FRAME_MS / 1000 * on.rate)
    worst = None
    for step in steps:
        curve = getattr(on, "probed", {}).get(step["id"], {}).get("curve")
        idle = idle_gain(off, source, f"tap:{step['target']}", [step])
        if curve is None or idle is None:
            return None
        left = [idle - np.nanmedian(curve[max(0, f - 5) : max(0, f - 1)]) for f in times[1:] // frame
                if np.isfinite(curve[max(0, f - 5) : max(0, f - 1)]).any()]
        if not left:
            return None
        worst = max(worst if worst is not None else -np.inf, float(np.median(left)))
    return worst


def judged_steps(ctx):
    """The steps a record's Worked if judges: its claimed steps, or its trial's, from the recipe they live in."""
    recipe = ctx.join_recipe or ctx.recipe
    ids = set(ctx.record.get("steps", [])) | set(ctx.trial_ids) if ctx.record else set()
    return [step for step in recipe.get("steps", []) if step.get("id") in ids]


def idle_gain(off, source, point, steps):
    """The gain the record's steps give a signal too quiet to trigger them, makeup included.

    A compressor that never rests has no idle stretch in the song to read, so its steps are run alone on a passage
    turned down 60 and 70 dB; when both agree within 0.1 dB the steps are linear there and that gain is idle.
    """
    audio = off.audio(point, source)
    mask = off.ctx.window("fast", source)
    if audio is None or mask is None:
        return None
    if not steps or any(s["plugin"] in render.NATIVE for s in steps):
        return None
    start = int(np.argmax(mask))
    passage = audio[start : start + WINDOW_S * off.rate]
    gains = []
    for drop_db in (60, 70):
        quiet = passage * 10 ** (-drop_db / 20)
        out = render.process(quiet, off.rate, [dict(step, target="idle") for step in steps], "idle", None)
        gains.append(10 * math.log10(max(float((out**2).sum()), 1e-30) / max(float((quiet**2).sum()), 1e-30)))
    return gains[0] if abs(gains[0] - gains[1]) <= 0.1 else None


def p_contribution_tail(on, off, source, args):
    """Tail (80-200 ms over 0-30 ms) at the point where the source's dry and wet paths sum, with against without."""
    times = on.events(args.get("events", "own"), source)
    return None if not len(times) else tail_change(on, off, source, args.get("point", "join"), times)


def p_tail_sparse_minus_fast(on, off, source, args):
    values = []
    for window in ("sparse", "fast"):
        times = on.events(args.get("events", "own"), source, window)
        if times is None or not len(times):
            return None
        values.append(tail_change(on, off, source, args.get("point", "join"), times))
    return None if None in values else values[0] - values[1]


def tail_change(on, off, source, point, times):
    a, b = frames_of(on, point, source), frames_of(off, point, source)
    if a is None or b is None:
        return None
    frame = round(FRAME_MS / 1000 * on.rate)
    idx = lambda lo, hi: np.concatenate([np.arange(t // frame + lo, t // frame + hi) for t in times]).clip(0, len(a) - 1)
    ratio = lambda f: (mean_db(f[idx(16, 40)]) or -200) - (mean_db(f[idx(0, 6)]) or -200)
    return ratio(a) - ratio(b)


def p_hit_swing(on, off, source, args):
    a, b = on.hit_stats("node", source, args["events"]), off.hit_stats("node", source, args["events"])
    on_off = [m_off_hit_level(v, source, {"events": args["events"]}) for v in (on, off)]
    if a is None or b is None or None in on_off:
        return None
    return float(np.median(a["level_db"] - b["level_db"])) - (on_off[0] - on_off[1])


def p_lowest_notes_level(on, off, source, args):
    notes = [n for n in note_list(on, source) if "f0_hz" in n and "level_db" in n]
    if len(notes) < MIN_SPREAD_EVENTS:
        return None
    cut = np.percentile([n["f0_hz"] for n in notes], 10)
    lowest = [n for n in notes if n["f0_hz"] <= cut]
    a, b = note_levels(on, source, {}, lowest), note_levels(off, source, {}, lowest)
    mix = [v.lufs("mix", source) for v in (on, off)]
    if a is None or b is None or None in mix:
        return None
    return float(np.median(a - b)) - (mix[0] - mix[1])


def p_faults_fall(on, off, source, args):
    """guitars.filters: every fault the without signal shows must fall."""
    checks = []
    if (m_overlap(off, source, {"other": args["other"], "band": (0, 120)}) or -200) > contracts.OVERLAP_PRESENT_DB:
        checks.append((m_overlap, {"other": args["other"], "band": (0, 120)}))
    checks.append((m_band, {"band": (10000, None)}))
    for function, params in checks:
        a, b = function(on, source, params), function(off, source, params)
        if None in (a, b):
            return None
        if a >= b - contracts.HOLDS_DB:
            return 0
    return 1


def p_overlap_falls(on, off, source, args):
    before = m_overlap(off, source, args)
    if before is None:
        return None
    if before <= contracts.OVERLAP_PRESENT_DB:
        return 1
    after = m_overlap(on, source, args)
    return None if after is None else int(after < before - contracts.HOLDS_DB)


def p_gaps_close(on, off, source, args):
    found = []
    lead_before = m_lead_gap(off, source, {"band": (2000, 4000)})
    roles = set(off.ctx.roles.get(source, {}).values())
    if lead_before is None and {"lead", "rhythm"} <= roles:
        return None
    lead_after = m_lead_gap(on, source, {"band": (2000, 4000)}) if lead_before is not None else None
    asked = [v.why for v in (lead_before, lead_after) if isinstance(v, Review)]
    lead_before, lead_after = (v.value if isinstance(v, Review) else v for v in (lead_before, lead_after))
    lead_found = []
    if lead_before is not None and lead_before < 0:
        lead_found.append(lead_after is not None and lead_after > lead_before + contracts.HOLDS_DB)
    for params in ({"point": "chain:kick", "other": "rhythm", "band": (2000, 5000), "events": "kick"},
                   {"point": "chain:snare", "other": "rhythm", "band": (2000, 5000), "events": "snare"}):
        before = m_onset_gap(off, source, params)
        if before is None:
            return None
        if before < 0:
            after = m_onset_gap(on, source, params)
            found.append(after is not None and after > before + contracts.HOLDS_DB)
    # A drum gap that stays shut is a known failure; only the lead's part can be a question.
    if not all(found):
        return 0
    result = int(all(found + lead_found))
    return Review(result, asked[0]) if asked else result


def p_bands_in_range(on, off, source, args):
    for field in args["fields"]:
        before = m_range_outside(off, source, {"field": field})
        if before == SKIPPED:
            return SKIPPED
        if before is None:
            return None
        if before > 0 and m_range_outside(on, source, {"field": field}) != 0:
            return 0
    return 1


def p_section_error_change(on, off, source, args):
    sections = on.ctx.recipe.get("sections")
    if not sections or not all("loudness_lu" in s for s in sections.values()):
        return None
    a, b = (measure.sections(v.result.mix, v.rate, sections)["sections"] for v in (on, off))
    if any(a[n].get("error_lu") is None or b[n].get("error_lu") is None for n in a):
        return None
    changes = [abs(a[n]["error_lu"] - b[n]["error_lu"]) for n in a]
    return max(changes) if changes else None


def lead_delays(ctx, source, recipe):
    """The buses where a Delay plays what the lead feeds it."""
    routing = Routing(recipe)
    lead = {stem for stem, role in ctx.roles.get(source, {}).items() if role == "lead"}
    return sorted({step["target"] for step in recipe.get("steps", []) if step.get("plugin") == "Delay"
                   and step["target"] not in recipe["stems"] and routing.stems_into(step["target"]) & lead})


def without_lead_delays(ctx, source, recipe):
    """The recipe with every send from the lead's own stems and buses toward a lead-fed Delay removed, so the
    difference from the recipe is what that Delay delivers, after everything downstream of it."""
    import copy

    delays = set(lead_delays(ctx, source, recipe))
    if not delays:
        return None
    variant = copy.deepcopy(recipe)
    routing = Routing(variant)
    lead = role_stems(ctx, source, "lead")

    def feeds_delay(bus):
        chain = [bus]
        while chain[-1] != "mix" and chain[-1] not in delays:
            chain.append(variant.get("buses", {}).get(chain[-1], {}).get("bus", "mix"))
        return chain[-1] in delays

    for name in list(variant["stems"]) + list(variant.get("buses", {})):
        settings = variant["stems"].get(name) or variant.get("buses", {}).get(name)
        reach = {name} if name in variant["stems"] else routing.stems_into(name)
        if settings and reach and reach <= lead and settings.get("sends"):
            settings["sends"] = [send for send in settings["sends"] if not feeds_delay(send["bus"])]
    return variant


def without_lead(ctx, source, recipe):
    """The recipe with the lead's stems silent, so the difference from it is the lead as the mix delivers it."""
    import copy

    variant = copy.deepcopy(recipe)
    for stem in role_stems(ctx, source, "lead"):
        variant["stems"][stem]["gain_db"] = -300.0
    return variant


def lead_gaps(mask, rate):
    """The lead's own gaps: where it rests within 2 s after it played, not the sections it sits out."""
    frame = round(FRAME_MS / 1000 * rate)
    active = mask[::frame]
    index = np.arange(len(active))
    last = np.maximum.accumulate(np.where(active, index, -10**9))
    gaps = ~active & (index - last <= round(2000 / FRAME_MS))
    return np.repeat(gaps, frame)[: len(mask)]


def echo_keys(ctx, source, recipe):
    """The Ducks keyed from somewhere a lead-fed Delay's output reaches, through parents and sends."""
    nodes = render.sources(recipe)
    seen, queue = set(), list(lead_delays(ctx, source, recipe))
    while queue:
        node = queue.pop()
        if node in seen or node == "mix":
            continue
        seen.add(node)
        settings = nodes.get(node, {})
        queue += [settings.get("bus", "mix")] + [send["bus"] for send in settings.get("sends", [])]
    return sorted(step["id"] for step in recipe.get("steps", []) if step.get("plugin") == "Duck"
                  and step.get("settings", {}).get("key") in seen)


def delay_gap_tail(on, nodelay, nolead, source):
    """A lead-fed Delay's tail in the lead's gaps over the dry lead while it plays, both as the mix delivers them, so
    every gain and process after them counts for both."""
    mask = active_mask(on, "stem:lead", source)
    if mask is None:
        return None
    if not lead_delays(on.ctx, source, on.recipe or on.ctx.recipe):
        return -200.0
    if nodelay is None or nolead is None:
        return None
    mix, dry, silent = (view.audio("mix", source) for view in (on, nodelay, nolead))
    tail, lead = ((part ** 2).sum(axis=1) for part in (mix - dry, dry - silent))
    size = min(len(tail), len(mask))
    level, gaps = mean_db(lead[:size], mask[:size]), lead_gaps(mask, on.rate)[:size]
    if level is None:
        return None
    if not gaps.any():
        return Review(None, "the lead never rests, so its Delay's tail can't be heard apart from it: judge the delay in the mix")
    keyed = echo_keys(on.ctx, source, on.recipe or on.ctx.recipe)
    if keyed:
        return Review(None, f"the lead's echo also drives {keyed}, so removing it moves more than the tail: judge the "
                            f"delay in the mix")
    return (mean_db(tail[:size], gaps) or -200.0) - level


def a_alt_overlap(on, alt, source, args):
    a, b = m_overlap(on, source, args), m_overlap(alt, source, args)
    return None if None in (a, b) else a - b


def a_alt_off_hit(on, alt, source, args):
    a, b = m_off_hit_level(on, source, args), m_off_hit_level(alt, source, args)
    return None if None in (a, b) else a - b


ALTS = {name[2:]: value for name, value in dict(globals()).items() if name.startswith("a_")}

PAIRS = {name[2:]: value for name, value in dict(globals()).items() if name.startswith("p_")}


def evaluate_leaf(item, record, views, facts):
    source = record["source"]
    kind = item["kind"]
    if kind == "fact":
        value = facts(item["name"], item["args"], record)
        return {"text": item["text"], "state": value, "value": None}
    args = dict(item.get("args", {}))
    if kind == "judged":
        value, function = None, MEASURES.get(item.get("measure"))
        # In Worked the question is about the change, so it shows with minus without; in Needed, the reading itself.
        reading = args.pop("reading", False)
        if function and reading and views.get("with" if item.get("on_with") else "base") is not None:
            value = safe(function, views["with" if item.get("on_with") else "base"], source, args)
            value = round(float(value), 3) if isinstance(value, (int, float)) else None
        elif function and item.get("on_with") and views.get("with") is not None and views.get("without") is not None:
            a, b = safe(function, views["with"], source, args), safe(function, views["without"], source, args)
            value = None if not all(isinstance(v, (int, float)) for v in (a, b)) else round(a - b, 3)
        elif function and views.get("base") is not None:
            value = safe(function, views["base"], source, args)
            value = value if isinstance(value, (int, float)) else None
        return {"text": item["text"] + (" (change with the move)" if item.get("on_with") and not reading and value is not None else ""),
                "state": REVIEW, "value": value}
    name, is_delta = item["measure"], args.pop("delta", False)
    if args.get("events", "own") == "own" and name in HIT_MEASURES and views.get("base") is not None \
            and views["base"].ctx.too_few_hits(source):
        return {"text": item["text"], "state": SKIPPED, "value": None, "measure": name, "why": "under 3 own hits"}
    if name == "delay_gap_tail":
        value = None if views.get("with") is None else safe(delay_gap_tail, views["with"], views.get("nodelay"), views.get("nolead"), source)
        if isinstance(value, Review):
            return {"text": item["text"], "state": REVIEW, "value": None, "measure": name, "why": value.why}
        if value is None:
            return {"text": item["text"], "state": UNRESOLVED, "value": None, "why": "no support", "measure": name}
        return {"text": item["text"], "state": TRUE if compare(value, item["op"], item["value"]) else FALSE,
                "value": round(float(value), 3), "test": f"{item['op']} {item['value']}", "measure": name}
    if name in ALTS or name == "dip_vs_tried":
        others = views.get("alt" if name in ALTS else "tried")
        if not others or views.get("with") is None:
            return {"text": item["text"], "state": UNRESOLVED, "value": None, "why": "no alternative rendered"}
        if name in ALTS:
            value = safe(ALTS[name], views["with"], others, source, args)
        else:
            own = safe(m_dip_both, views["with"], source)
            tried = [safe(m_dip_both, other, source) for other in others]
            value = None if own is None or None in tried else own - min(tried)
    elif (is_delta or name in PAIRS) and views.get("options_only"):
        return {"text": item["text"], "state": SKIPPED, "value": None, "measure": name}
    elif is_delta or name in PAIRS:
        on, off = views.get("with"), views.get("without")
        if on is None or off is None:
            return {"text": item["text"], "state": UNRESOLVED, "value": None, "why": "no with/without pair"}
        if name in PAIRS:
            value = safe(PAIRS[name], on, off, source, args)
        else:
            a, b = safe(MEASURES[name], on, source, args), safe(MEASURES[name], off, source, args)
            if SKIPPED in (a, b):
                value = SKIPPED
            elif isinstance(a, Review) or isinstance(b, Review):
                why = " ".join(v.why for v in (a, b) if isinstance(v, Review))
                a, b = (v.value if isinstance(v, Review) else v for v in (a, b))
                value = Review(None if None in (a, b) else round(a - b, 3), why)
            else:
                value = None if None in (a, b) else round(a - b, 3)
    else:
        view = views.get("with" if item.get("on_with") else "base")
        value = None if view is None else safe(MEASURES[name], view, source, args)
    if isinstance(value, Review) and value.floor and value.value is not None:
        rises = item["op"] in (">", ">=")
        if compare(value.value, item["op"], item["value"]) == rises:
            value = value.value
    if isinstance(value, Review):
        return {"text": item["text"], "state": REVIEW, "value": None if value.value is None else round(float(value.value), 3),
                "measure": name, "why": value.why}
    if value == SKIPPED:
        return {"text": item["text"], "state": SKIPPED, "value": None, "measure": name}
    if value is None:
        return {"text": item["text"], "state": UNRESOLVED, "value": None, "why": "no support", "measure": name}
    return {"text": item["text"], "state": TRUE if compare(value, item["op"], item["value"]) else FALSE,
            "value": round(float(value), 3), "test": f"{item['op']} {item['value']}", "measure": name}


# Measures read on a drum's own hits: a tom played fewer than three times has nothing for them to read.
HIT_MEASURES = {"bleed", "decay_ms", "attack_p50", "attack_p10", "tail_p50", "level_p50", "level_top", "spread", "body_p50",
                "onset_band", "octave_peak", "band_vs_level_spread", "gap_valley", "below_f0", "f0_band", "dip",
                "damaged_hits", "decay_lost", "recovery", "contribution_tail", "hit_over_off", "off_hit_level"}


def safe(function, *args):
    try:
        return function(*args)
    except (ValueError, ZeroDivisionError, IndexError, FloatingPointError):
        return None


def combine(kind, states):
    """Three-valued logic, plus REVIEW (a question no number answers) and SKIPPED (no profile)."""
    states = [s for s in states if s != SKIPPED] or [SKIPPED]
    if kind == "any":
        if TRUE in states:
            return TRUE
        if UNRESOLVED in states:
            return UNRESOLVED
        if REVIEW in states:
            return REVIEW
        return SKIPPED if states == [SKIPPED] else FALSE
    if FALSE in states:
        return FALSE
    if UNRESOLVED in states:
        return UNRESOLVED
    if REVIEW in states:
        return REVIEW
    return SKIPPED if states == [SKIPPED] else TRUE


def restate(tree, states, path):
    """A tree's state again from its leaves' states, after an override waives the leaves it names."""
    if tree["kind"] in ("any", "all"):
        return combine(tree["kind"], [restate(child, states, f"{path}.{i}") for i, child in enumerate(tree["items"])])
    return states.get(path, UNRESOLVED)


def evaluate(tree, record, views, facts, path, leaves, on_with=False):
    if tree["kind"] in ("any", "all"):
        states = [evaluate(child, record, views, facts, f"{path}.{i}", leaves, on_with) for i, child in enumerate(tree["items"])]
        return combine(tree["kind"], states)
    item = dict(tree, on_with=on_with)
    found = evaluate_leaf(item, record, views, facts) | {"id": path}
    leaves.append(found)
    return found["state"]
