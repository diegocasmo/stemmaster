import json
import math
import sys
from pathlib import Path

import numpy as np
import pedalboard
import pyloudnorm
import soundfile

import airwindows
import native

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".agents" / "skills" / "analyze"))
from analyze import true_peak_dbtp

STEM_FIELDS = {"gain_db", "pan", "invert", "offset_samples", "bus", "sends"}
BUS_FIELDS = {"bus", "sends"}
SEND_FIELDS = {"bus", "gain_db"}
SECTION_FIELDS = {"start_s", "end_s", "group", "loudness_lu"}
NATIVE = {"Ride": native.ride, "Duck": native.duck}
LIMITERS = {"ZL Limiter", "Limiter"}
DRAGONFLY_DRY_DEFAULT = 80
REVERB_DRY_DEFAULT = 0.4
MIX_DEFAULT = 0.5
PLUGINS = ROOT / "plugins"
MAX_TRUE_PEAK_DBTP = -1.0
LUFS_TOLERANCE = 0.005


def load(step):
    name, settings = step["plugin"], dict(step["settings"])
    bundle = PLUGINS / f"{name}.vst3"
    if not bundle.exists():
        return getattr(pedalboard, name)(**settings)
    # A fresh instance per step: reset() does not clear every plugin's state, so reuse changes the sound.
    plugin = pedalboard.load_plugin(str(bundle))
    if "effect" in settings:
        airwindows.select(plugin, settings.pop("effect"))
        # Switching effects keeps the previous effect's slot values, so an unset parameter is a leftover, not a default.
        if missing := set(plugin.parameters) - airwindows.WRAPPER - set(settings):
            sys.exit(f"step {step.get('id')}: set every parameter of {step['settings']['effect']}, missing {sorted(missing)}")
    for key, value in settings.items():
        # setattr would silently add an unknown name instead of failing.
        if key not in plugin.parameters:
            sys.exit(f"step {step.get('id')}: {name} has no parameter {key}")
        setattr(plugin, key, value)
    return plugin


def process(audio, rate, steps, target, context):
    for step in steps:
        if step["target"] != target:
            continue
        if step["plugin"] in NATIVE:
            audio = NATIVE[step["plugin"]](audio, rate, step["settings"], context)
            continue
        plugin = load(step)
        # Some VST3s accept only stereo, so a mono stem goes through them as two equal channels.
        mono = audio.shape[1] == 1 and isinstance(plugin, pedalboard.VST3Plugin)
        stereo = np.repeat(audio, 2, axis=1) if mono else audio
        # Plugins take (channels, samples); the rest of this file uses (samples, channels).
        out = plugin(stereo.T.astype(np.float32), rate).T.astype(np.float64)
        audio = out.mean(axis=1, keepdims=True) if mono else out
    return audio


def place(audio, offset, length):
    placed = np.zeros((length, audio.shape[1]))
    start, end = max(offset, 0), min(offset + len(audio), length)
    if end <= start:
        return placed
    placed[start:end] = audio[start - offset : end - offset]
    return placed


def pan(audio, position):
    if audio.shape[1] == 1:
        # Constant power: -3 dB per side at center, so loudness holds as it moves.
        angle = (position + 1) * math.pi / 4
        return audio * [math.cos(angle), math.sin(angle)]
    return audio * [min(1, 1 - position), min(1, 1 + position)]


def bus_order(recipe):
    order = [settings.get("bus", "mix") for settings in recipe["stems"].values()] + list(recipe.get("buses", {}))
    return [bus for bus in dict.fromkeys(order) if bus != "mix"]


def parent(recipe, bus):
    return recipe.get("buses", {}).get(bus, {}).get("bus", "mix")


def sources(recipe):
    found = {name: settings for name, settings in recipe["stems"].items()}
    found.update({bus: recipe.get("buses", {}).get(bus, {}) for bus in bus_order(recipe)})
    return found


def timeline(recipe, song):
    infos = {name: soundfile.info(song / "stems" / name) for name in recipe["stems"]}
    rates = {info.samplerate for info in infos.values()}
    if len(rates) != 1:
        sys.exit(f"stems must share one sample rate, got {sorted(rates)}")
    rate = rates.pop()
    length = max(info.frames + recipe["stems"][name].get("offset_samples", 0) for name, info in infos.items())
    return rate, length + round(recipe.get("tail_seconds", 0) * rate)


def check(recipe, rate, length):
    stems, buses, sections = recipe["stems"], recipe.get("buses", {}), recipe.get("sections", {})
    names = bus_order(recipe)
    for name, settings in stems.items():
        if unknown := set(settings) - STEM_FIELDS:
            sys.exit(f"stem {name}: unknown fields {sorted(unknown)}")
    for name, settings in buses.items():
        if unknown := set(settings) - BUS_FIELDS:
            sys.exit(f"bus {name}: unknown fields {sorted(unknown)}")
        if settings.get("bus", "mix") not in names + ["mix"]:
            sys.exit(f"bus {name}: unknown parent {settings['bus']} (declare it in buses)")
    if clash := (set(names) | {"mix"}) & set(stems):
        sys.exit(f"bus names clash with stem names: {sorted(clash)}")
    if "mix" in buses:
        sys.exit("mix is the final sum, not a bus")
    for name, settings in sources(recipe).items():
        for send in settings.get("sends", []):
            if unknown := set(send) - SEND_FIELDS:
                sys.exit(f"{name}: send has unknown fields {sorted(unknown)}")
            if send.get("bus") not in names:
                sys.exit(f"{name}: send to unknown bus {send.get('bus')} (declare it in buses)")
            if not native.number(send.get("gain_db", 0)):
                sys.exit(f"{name}: send gain_db must be a number")
    returns = {send["bus"] for settings in sources(recipe).values() for send in settings.get("sends", [])}
    fed = {settings.get("bus", "mix") for settings in sources(recipe).values()} | returns
    if unfed := set(buses) - fed:
        sys.exit(f"buses nothing feeds: {sorted(unfed)}")
    check_sections(sections, length / rate)
    steps = recipe.get("steps", [])
    for step in steps:
        label = f"step {step.get('id')}"
        if step.get("stage", "mix") not in ("mix", "master"):
            sys.exit(f"{label}: stage must be mix or master")
        if step.get("stage") == "master" and step["target"] != "mix":
            sys.exit(f"{label}: master steps must target mix")
        if step["target"] not in set(names) | set(stems) | {"mix"}:
            sys.exit(f"{label}: unknown target {step['target']}")
        plugin = step["plugin"]
        if plugin == "Ride":
            error = native.ride_error(step["settings"], sections, rate)
        elif plugin == "Duck":
            error = native.duck_error(step["settings"], step["target"], set(names) | set(stems), rate)
        elif not (PLUGINS / f"{plugin}.vst3").exists() and not hasattr(pedalboard, plugin):
            error = f"unknown plugin {plugin} (copy its .vst3 into plugins/)"
        elif step["target"] in returns:
            error = dry_error(step)
        else:
            error = None
        if error:
            sys.exit(f"{label}: {error}")
    master = [step for step in steps if step.get("stage") == "master"]
    if master and master[-1]["plugin"] not in LIMITERS:
        sys.exit(f"the last master step must be a limiter ({', '.join(sorted(LIMITERS))})")
    check_cycles(recipe)


def check_sections(sections, song_seconds):
    spans = []
    for name, section in sections.items():
        if unknown := set(section) - SECTION_FIELDS:
            sys.exit(f"section {name}: unknown fields {sorted(unknown)}")
        start, end = section.get("start_s"), section.get("end_s")
        if not (native.number(start) and native.number(end) and 0 <= start < end <= song_seconds):
            sys.exit(f"section {name}: needs 0 <= start_s < end_s <= {song_seconds:.2f}")
        if "loudness_lu" in section and not native.number(section["loudness_lu"]):
            sys.exit(f"section {name}: loudness_lu must be a number")
        spans.append((start, end, name))
    spans.sort()
    for (_, end, first), (start, _, second) in zip(spans, spans[1:]):
        if start < end:
            sys.exit(f"sections {first} and {second} overlap")
    marked = [name for name, section in sections.items() if "loudness_lu" in section]
    if marked and len(marked) != len(sections):
        sys.exit("give loudness_lu to every section or to none")


def dry_error(step):
    # Returns fed by sends must be wet only, or the dry sound is added twice.
    plugin, settings = step["plugin"], step["settings"]
    if plugin.startswith("Dragonfly") and settings.get("dry_level", DRAGONFLY_DRY_DEFAULT) != 0:
        return f"{plugin} on a send return needs dry_level 0"
    if plugin == "Reverb" and settings.get("dry_level", REVERB_DRY_DEFAULT) != 0:
        return "Reverb on a send return needs dry_level 0"
    if plugin in ("Delay", "Chorus") and settings.get("mix", MIX_DEFAULT) != 1:
        return f"{plugin} on a send return needs mix 1"
    return None


def check_cycles(recipe):
    edges = {}
    for name, settings in sources(recipe).items():
        edges.setdefault(name, set()).add(settings.get("bus", "mix"))
        edges[name] |= {send["bus"] for send in settings.get("sends", [])}
    for step in recipe.get("steps", []):
        if step["plugin"] == "Duck":
            edges.setdefault(step["settings"]["key"], set()).add(step["target"])
    state = {}

    def visit(node, path):
        if state.get(node) == "done":
            return
        if state.get(node) == "open":
            sys.exit(f"routing cycle: {' -> '.join(path[path.index(node) :] + [node])}")
        state[node] = "open"
        for following in sorted(edges.get(node, ())):
            visit(following, path + [node])
        state[node] = "done"

    for node in list(edges):
        visit(node, [])


class Mix:
    """Each output is added to the buses it feeds as soon as it is ready, so whole-song copies don't pile up.

    Stems run in recipe order and buses in first-seen order, as the render always summed them, so recipes
    without buses, sends or keys keep their exact samples. A keyed step pulls its key forward.
    """

    def __init__(self, recipe, song, rate, length, taps=()):
        self.recipe, self.song, self.rate, self.length = recipe, song, rate, length
        self.taps, self.tapped = set(taps), {}
        self.sections = recipe.get("sections", {})
        self.steps = [step for step in recipe.get("steps", []) if step.get("stage", "mix") == "mix"]
        self.keys = {step["settings"]["key"] for step in recipe.get("steps", []) if step["plugin"] == "Duck"}
        self.feeds = sources(recipe)
        self.sums, self.keyed, self.done = {}, {}, set()

    def add(self, bus, audio):
        self.sums[bus] = self.sums.get(bus, np.zeros((self.length, 2))) + audio

    def send(self, name, audio, settings):
        self.add(settings.get("bus", "mix"), audio)
        for send in settings.get("sends", []):
            self.add(send["bus"], audio * 10 ** (send.get("gain_db", 0) / 20))

    def key(self, name):
        self.pull(name)
        return self.keyed[name]

    def pull(self, name):
        if name in self.recipe["stems"]:
            self.stem(name)
        else:
            self.bus(name)

    def stem(self, name):
        if name in self.done:
            return
        self.done.add(name)
        settings = self.recipe["stems"][name]
        audio = soundfile.read(self.song / "stems" / name, always_2d=True)[0]
        audio = audio * 10 ** (settings.get("gain_db", 0) / 20)
        if settings.get("invert", False):
            audio = -audio
        audio = process(place(audio, settings.get("offset_samples", 0), self.length), self.rate, self.steps, name, self)
        if name in self.keys:
            self.keyed[name] = audio
        audio = pan(audio, settings.get("pan", 0))
        if name in self.taps:
            self.tapped[name] = audio
        self.send(name, audio, settings)

    def bus(self, name):
        if name in self.done:
            return
        self.done.add(name)
        for source, settings in self.feeds.items():
            if name == settings.get("bus", "mix") or name in {send["bus"] for send in settings.get("sends", [])}:
                self.pull(source)
        audio = process(self.sums.pop(name, np.zeros((self.length, 2))), self.rate, self.steps, name, self)
        if name in self.keys:
            self.keyed[name] = audio
        if name in self.taps:
            self.tapped[name] = audio
        self.send(name, audio, self.feeds[name])

    def render(self):
        for name in self.recipe["stems"]:
            self.stem(name)
        for name in bus_order(self.recipe):
            self.bus(name)
        return process(self.sums.pop("mix", np.zeros((self.length, 2))), self.rate, self.steps, "mix", self)


def master_target(recipe):
    path = ROOT / "profiles" / str(recipe.get("profile")) / "profile.json"
    target = json.loads(path.read_text()).get("master_integrated_lufs") if path.exists() else None
    if not isinstance(target, (int, float)) or not math.isfinite(target):
        sys.exit(f"--master needs a numeric master_integrated_lufs in {path}")
    return target


def measure(audio, rate):
    return pyloudnorm.Meter(rate).integrated_loudness(audio), true_peak_dbtp(audio)


def render(recipe_path, start_seconds=None, end_seconds=None, final=False, taps=()):
    song = Path(recipe_path).parent
    recipe = json.loads(Path(recipe_path).read_text())
    rate, length = timeline(recipe, song)
    check(recipe, rate, length)
    master_steps = [step for step in recipe.get("steps", []) if step.get("stage") == "master"]
    if unknown := set(taps) - set(sources(recipe)):
        sys.exit(f"--tap names no stem or bus: {sorted(unknown)}")
    target = master_target(recipe) if final else None
    mixer = Mix(recipe, song, rate, length, taps)
    mix = mixer.render()

    first = round((start_seconds or 0) * rate)
    last = round(end_seconds * rate) if end_seconds is not None else len(mix)
    if not 0 <= first < min(last, len(mix)):
        sys.exit(f"clip must satisfy 0 <= start < end and start < {len(mix) / rate:.2f} s")
    out = song / "renders" / "mix.wav"
    out.parent.mkdir(exist_ok=True)
    soundfile.write(out, mix[first:last], rate, subtype="FLOAT")
    written = [out]
    for name, audio in mixer.tapped.items():
        kind = "stem" if name in recipe["stems"] else "bus"
        written.append(song / "renders" / f"tap-{kind}-{name.removesuffix('.wav')}.wav")
        soundfile.write(written[-1], audio[first:last], rate, subtype="FLOAT")
    # Integrated loudness is song-wide, so a clip gets no master.
    if not master_steps or (first, last) != (0, len(mix)):
        if final:
            sys.exit("--master needs master steps and a whole-song render")
        return written
    master = song / "renders" / ("master.wav" if final else "trial.wav")
    # Written aside and checked first, so a failed render never replaces a good one.
    candidate = master.with_name(master.stem + ".tmp.wav")
    try:
        mastered = process(mix, rate, master_steps, "mix", mixer)
        soundfile.write(candidate, mastered, rate, subtype="PCM_24")
        lufs, peak = measure(*soundfile.read(candidate, always_2d=True))
        # The 24-bit write clamps at full scale, so the file's own peak hides overs.
        clipped = int(np.sum(np.abs(mastered) > 1))
        print(f"{master.name}: {lufs:.3f} LUFS, {peak:.3f} dBTP" + (f", {clipped} samples clipped" if clipped else ""))
        if final and not (abs(lufs - target) <= LUFS_TOLERANCE and peak <= MAX_TRUE_PEAK_DBTP):
            sys.exit(f"master misses {target} LUFS and {MAX_TRUE_PEAK_DBTP} dBTP; master.wav left unchanged")
        candidate.replace(master)
    finally:
        candidate.unlink(missing_ok=True)
    return written + [master]


if __name__ == "__main__":
    args, taps, final = sys.argv[1:], [], False
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--master":
            final = True
        elif flag == "--tap" and args:
            taps.append(args.pop(0))
        else:
            args = []
    if len(args) not in (1, 3):
        sys.exit("usage: uv run .agents/skills/render/render.py [--master] [--tap <name>]... <recipe.json> [<start_s> <end_s>]")
    for path in render(args[0], *[float(t) for t in args[1:]], final=final, taps=taps):
        print(path)
