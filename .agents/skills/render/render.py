import ctypes
import contextlib
import hashlib
import io
import json
import marshal
import math
import os
import platform
import re
import shutil
import sys
import time
from importlib import metadata
from pathlib import Path
from stat import S_ISREG

import numpy as np
import pedalboard
import pyloudnorm
import soundfile

import airwindows
import native
import prepared

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
MASTER_IN = "master-in"
RESERVED = {MASTER_IN, "premaster", "trial"}
LIBC = ctypes.CDLL(None)
RAND_SEED = 1


def _load_plugin(bundle, **kwargs):
    # Dragonfly and Airwindows draw on the process-wide C rand(), so without a fixed seed their output depends on
    # how many plugins loaded before them in this process.
    LIBC.srand(RAND_SEED)
    return pedalboard.load_plugin(str(bundle), **kwargs)


def load(step, rate=None, channels=None):
    name, settings = step["plugin"], dict(step["settings"])
    bundle = PLUGINS / f"{name}.vst3"
    if not bundle.exists():
        return getattr(pedalboard, name)(**settings)
    prepared_key = prepared.key(bundle, settings, rate, channels)
    if (plugin := prepared.load(bundle, prepared_key, _load_plugin)) is not None:
        return plugin
    # A fresh instance per step: reset() does not clear every plugin's state, so reuse changes the sound.
    plugin = _load_plugin(bundle)
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
    prepared.remember(plugin, prepared_key)
    return plugin


def apply(audio, rate, step, context):
    if step["plugin"] in NATIVE:
        return NATIVE[step["plugin"]](audio, rate, step["settings"], context)
    plugin = load(step, rate=rate, channels=max(2, audio.shape[1]))
    # Some VST3s accept only stereo, so a mono stem goes through them as two equal channels.
    mono = audio.shape[1] == 1 and isinstance(plugin, pedalboard.VST3Plugin)
    stereo = np.repeat(audio, 2, axis=1) if mono else audio
    # Plugins take (channels, samples); the rest of this file uses (samples, channels).
    out = plugin(stereo.T.astype(np.float32), rate).T.astype(np.float64)
    return out.mean(axis=1, keepdims=True) if mono else out


def process(audio, rate, steps, target, context):
    for step in steps:
        if step["target"] != target:
            continue
        before = audio
        audio = apply(audio, rate, step, context)
        if step.get("id") in getattr(context, "probes", {}):
            context.probed[step["id"]] = context.probes[step["id"]](before, audio, rate)
    return audio


def digest(*parts):
    # Settings keep their order: parameters are set in that order, and some plugins depend on it.
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


MEMO_BYTES = 20 << 30
# Left free on the volume, so a full cache folder never starves another process's writes.
MEMO_SPARE_BYTES = 2 << 30
MEMO_STALE_NS = 24 * 3600 * 10**9
MEMO_CHUNK_BYTES = 8 << 20


def code_digest(paths):
    # Per file, so moving code from one file to the next changes it.
    return digest([[path.name, hashlib.sha256(path.read_bytes()).hexdigest()] for path in paths])


def runs_source(module):
    """False when the bytecode beside the module holds other code than its source: import may have run that instead."""
    try:
        cached = getattr(module, "__cached__", None)
        if not cached or not os.path.exists(cached):
            return True
        with open(cached, "rb") as file:
            code = marshal.loads(file.read()[16:])
        # Code objects compare by what they run, not by the file name they were compiled under.
        return code == compile(Path(module.__file__).read_bytes(), code.co_filename, "exec", dont_inherit=True)
    except Exception:
        return False


# Hashed at import, so the tag names the code this process runs even if the files change on disk later. None when a
# stale .pyc may be what runs, which turns the memo off.
CODE = code_digest(Path(__file__).with_name(name) for name in ("render.py", "native.py", "airwindows.py", "prepared.py")) \
    if all(runs_source(module) for module in (sys.modules[__name__], native, airwindows, prepared)) else None
# Only names the memo writes are ever evicted, so a folder shared with other files keeps them.
MEMO_NAME = re.compile(r"([0-9a-f]{16})-[0-9a-f]{64}(\.npy|\.seen|\.\d+\.tmp)")
# What an empty file takes on disk, so seen markers count toward the cap.
MEMO_BLOCK_BYTES = 4096
MEMO_MARKER_BUDGET_BYTES = 64 << 20


def memo_folder():
    return Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "stemmaster" / "nodes"


def layout(audio):
    # sha256 reads a C-contiguous buffer, and a Fortran-order array's transpose is one.
    return audio if audio.flags.c_contiguous else audio.T


def bundle_files(bundle):
    """Its files, and its links with their targets: a link can move to another folder with no file changing."""
    # os.walk follows linked folders (Path.rglob doesn't); a folder already seen by its real path is a cycle or a repeat.
    seen, files, links = set(), [], []
    def fail(error):
        raise error

    # An unreadable folder raises, so the step is uncached instead of keyed without it.
    for top, folders, names in os.walk(bundle, followlinks=True, onerror=fail):
        links += [[str(Path(top, name).relative_to(bundle)), "link", os.readlink(Path(top, name))]
                  for name in folders + names if os.path.islink(Path(top, name))]
        if os.path.realpath(top) in seen:
            folders.clear()
            continue
        seen.add(os.path.realpath(top))
        files += [Path(top, name) for name in names]
    return sorted(files), sorted(links)


class Memo:
    """Step outputs kept on disk across renders, runs and songs, keyed by everything upstream that decides their samples.

    Each output is an .npy file followed by the sha256 of everything before it, so a damaged file is a miss. A file's
    mtime is its last use. Past the byte cap, files of other code unused for a day go first, then the least recently used.
    """

    def __init__(self, folder, cap_bytes=MEMO_BYTES, sightings=2):
        if CODE is None:
            raise RuntimeError("a .pyc of render.py, native.py, airwindows.py or prepared.py holds other code than its source")
        # Most outputs are computed once and never asked for again, so with sightings=2 one is stored only on its
        # second computation; an empty .seen file remembers the first, across runs too. sightings=1 stores on the first.
        if sightings not in (1, 2):
            raise ValueError(f"sightings must be 1 or 2, not {sightings!r}")
        self.folder, self.cap, self.sightings = Path(folder), cap_bytes, sightings
        # Bytes of markers made since the last eviction: past a small budget they trigger one, so the cap holds even
        # when nothing is stored.
        self.marked = 0
        self.folder.mkdir(parents=True, exist_ok=True)
        self.bundles = {}
        self.ran = self.reused = 0
        # Cached files outlive this process, so the code and libraries that computed them are part of every key.
        # The OS build too: numpy's math and the plugins call system libraries an OS update can change.
        self.code = [CODE, sys.version, platform.platform(), os.uname().version,
                     *(metadata.version(name) for name in ("numpy", "scipy", "pedalboard", "numba", "llvmlite"))]
        self.tag = digest(self.code)[:16]
        self.evict()

    def path(self, key):
        return self.folder / f"{self.tag}-{key}.npy"

    def used(self, path):
        # The marker is dated with its file, so least recently used eviction takes the file before its marker.
        with contextlib.suppress(OSError):
            os.utime(path.with_suffix(".seen"), follow_symlinks=False)

    def get(self, key):
        path = self.path(key)
        # Any error is a miss: numpy's header parser can raise almost anything on damaged bytes.
        try:
            # Nothing the memo opens or creates follows a link: one planted in the folder could point anywhere. Non
            # blocking, so a FIFO there can't hang the render; only a regular file is read.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not S_ISREG(os.fstat(descriptor).st_mode):
                    return None
                with open(descriptor, "rb", closefd=False) as file:
                    size = os.fstat(file.fileno()).st_size
                    header = file.read(10)
                    header += file.read(int.from_bytes(header[8:10], "little"))
                    parsed = io.BytesIO(header)
                    np.lib.format.read_magic(parsed)
                    shape, fortran, dtype = np.lib.format.read_array_header_1_0(parsed)
                    # Checked before the samples are read, so a damaged file can't ask for more memory than it should.
                    if (size > self.cap or dtype != np.float64 or not all(type(n) is int and n >= 0 for n in shape)
                            or len(header) + 8 * math.prod(shape) + 32 != size):
                        return None
                    # A header can agree with a damaged file's size, so the hash is checked in bounded reads before
                    # the samples get memory, then again on the bytes kept.
                    content, chunk = hashlib.sha256(header), bytearray(MEMO_CHUNK_BYTES)
                    for start in range(len(header), size - 32, len(chunk)):
                        read = file.readinto(memoryview(chunk)[: min(len(chunk), size - 32 - start)])
                        content.update(memoryview(chunk)[:read])
                    if content.digest() != file.read(32):
                        return None
                    file.seek(len(header))
                    data = bytearray(size - len(header))
                    if file.readinto(data) != len(data):
                        return None
                    content = hashlib.sha256(header)
                    content.update(memoryview(data)[:-32])
                    if content.digest() != data[-32:]:
                        return None
                    os.utime(descriptor)
            finally:
                os.close(descriptor)
            audio = np.frombuffer(data, np.float64, math.prod(shape))
            self.used(path)
        except Exception:
            return None
        return audio.reshape(shape, order="F" if fortran else "C")

    def put(self, key, audio):
        # np.save's format keeps C or Fortran order but not other strides, and later sums can depend on the order.
        if not (audio.flags.c_contiguous or audio.flags.f_contiguous):
            return
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, np.lib.format.header_data_from_array_1_0(audio))
        header = header.getvalue()
        size = len(header) + audio.nbytes + 32
        path = self.path(key)
        temp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
        try:
            if size > self.cap or shutil.disk_usage(self.folder).free < size + MEMO_SPARE_BYTES:
                return
            if self.sightings > 1:
                seen = path.with_suffix(".seen")
                try:
                    os.close(os.open(seen, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644))
                    self.marked += MEMO_BLOCK_BYTES
                    if self.marked > min(self.cap // 100, MEMO_MARKER_BUDGET_BYTES):
                        self.evict()
                    return
                except FileExistsError:
                    if not S_ISREG(seen.lstat().st_mode):
                        return
            content = hashlib.sha256(header)
            content.update(layout(audio))
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
            try:
                descriptor = os.open(temp, flags, 0o644)
            except FileExistsError:
                # Left by a killed process whose pid this one reuses; unlink removes a link itself, never its target.
                temp.unlink()
                descriptor = os.open(temp, flags, 0o644)
            try:
                with open(descriptor, "wb") as file:
                    file.write(header)
                    file.write(layout(audio))
                    file.write(content.digest())
                os.replace(temp, path)
            except OSError:
                with contextlib.suppress(OSError):
                    temp.unlink()
                return
        except OSError:
            return
        self.used(path)
        self.evict()

    def evict(self):
        # A future mtime (clock set back, files copied in) would outrank every new file, so it counts as oldest.
        now, files, newest, self.marked = time.time_ns(), [], {}, 0
        try:
            entries = list(os.scandir(self.folder))
        except OSError:
            return
        for entry in entries:
            try:
                if (name := MEMO_NAME.fullmatch(entry.name)) and entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    used = stat.st_mtime_ns if stat.st_mtime_ns <= now else 0
                    tag = name[1]
                    newest[tag] = max(newest.get(tag, 0), used)
                    files.append((tag, used, max(stat.st_size, MEMO_BLOCK_BYTES) if name[2] == ".seen" else stat.st_size, entry.path))
            except OSError:
                pass
        # Another worktree's check may be running now, so its files are stale only once it has written nothing for a day.
        stale = {tag for tag, used in newest.items() if tag != self.tag and used < now - MEMO_STALE_NS}
        files = [(tag not in stale, used, size, path) for tag, used, size, path in files]
        total = sum(size for _, _, size, _ in files)
        for _, _, size, path in sorted(files):
            if total <= self.cap:
                break
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass

    def plugin(self, name):
        # The same check load() makes, so a bundle added or removed between renders changes the key.
        bundle = PLUGINS / f"{name}.vst3"
        if not bundle.exists():
            return ["built-in"]
        files, links = bundle_files(bundle)
        # A rewrite changes ctime or inode, and neither can be set back the way size and mtime can.
        stats = [[str(f.relative_to(bundle)), *((s := f.stat()).st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino)]
                 for f in files] + links
        found = self.bundles.get(name)
        if found is None or found[0] != stats:
            content = []
            for f in files:
                with open(f, "rb") as file:
                    content.append([str(f.relative_to(bundle)), "file", os.fstat(file.fileno()).st_size,
                                    hashlib.file_digest(file, "sha256").hexdigest()])
            found = self.bundles[name] = stats, digest(content + links)
        return ["bundle", found[1]]

    def loader(self, step):
        """The step's part of its key, and the bundle's file stats, which any change to the bundle alters; None if unknown."""
        try:
            if step["plugin"] in NATIVE:
                return ["native"], None
            identity = self.plugin(step["plugin"])
            return identity, self.bundles[step["plugin"]][0] if identity[0] == "bundle" else None
        except Exception:
            return None


def names_file(step):
    return step["plugin"] == "Convolution" or any(
        isinstance(value, str) and ("file" in name.lower() or "path" in name.lower() or os.path.isfile(value))
        for name, value in step["settings"].items())


def step_key(key, step, context, upstream):
    """None when the step's output can't be keyed: its input can't, it reads a file, or its key node can't."""
    if key is None or names_file(step):
        return None
    plugin = step["plugin"]
    if plugin == "Ride":
        upstream = context.sections
    return None if upstream is None else digest(key, context.memo.code, plugin, step["settings"], upstream)


def chain(source, rate, steps, target, context, key):
    """process() for a node whose input `source()` is identified by `key`, resuming after the longest prefix in context.memo.

    Steps are visited in order, so Duck keys are pulled and probes filled in the same order as process(). From the
    first step that can't be keyed, every step runs and nothing is kept. Returns the output and its key, or None.
    """
    memo, audio, pending, broken = context.memo, None, [], False

    def settle():
        nonlocal audio, pending, key, broken
        found, index = None, -1
        for index in range(len(pending) - 1, -1, -1):
            if pending[index][1] is not None and (found := memo.get(pending[index][1])) is not None:
                break
        # A bundle changed since its loader was taken must load, and fail, as without the memo, so the hit is dropped.
        if found is not None and any(memo.loader(step) != loader for step, _, loader in pending[: index + 1]):
            found = None
        if found is not None:
            memo.reused += index + 1
            audio, pending = found, pending[index + 1 :]
        elif audio is None:
            audio = source()
        for step, after, loader in pending:
            audio = apply(audio, rate, step, context)
            memo.ran += 1
            # A bundle changed between this step's key and its load leaves this output, and all built on it, unkeyed.
            if after is not None and not broken and loader != memo.loader(step):
                broken, key = True, None
            if after is not None and not broken:
                memo.put(after, audio)
        pending = []
        return audio

    for step in steps:
        if step["target"] != target:
            continue
        if key is not None and step["plugin"] == "Duck" and step["settings"]["key"] not in context.done:
            # Pulling a key renders another node, so the steps before it run first, as in process(), and fail in the same order.
            settle()
        # Taken once: the same loader builds the key and is checked again after the step runs.
        loader = memo.loader(step) if key is not None else None
        upstream = loader and loader[0]
        if key is not None and step["plugin"] == "Duck":
            upstream = context.signature(step["settings"]["key"])
        try:
            key = step_key(key, step, context, upstream)
        except Exception:
            # A step whose key can't be built runs uncached, so it fails or not exactly as in process(), after the steps before it.
            key = None
        if step.get("id") in context.probes:
            before = settle()
            pending = [(step, key, loader)]
            context.probed[step["id"]] = context.probes[step["id"]](before, settle(), rate)
        else:
            pending.append((step, key, loader))
    return settle(), key


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


def check(recipe, rate, length, strict=True):
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
    if reserved := set(names) & RESERVED:
        sys.exit(f"{sorted(reserved)} name the mix and master signals the check reads, not buses")
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
    # Off variants that remove a send leave its return unfed; it then renders silence.
    if strict and (unfed := set(buses) - fed):
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
    if strict and master and master[-1]["plugin"] not in LIMITERS:
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

    def __init__(self, recipe, song, rate, length, taps=(), probes=None, memo=None, tapped=None):
        self.recipe, self.song, self.rate, self.length = recipe, song, rate, length
        self.taps, self.tapped = set(taps), {} if tapped is None else tapped
        self.probes, self.probed = probes or {}, {}
        self.sections = recipe.get("sections", {})
        self.steps = [step for step in recipe.get("steps", []) if step.get("stage", "mix") == "mix"]
        self.keys = {step["settings"]["key"] for step in recipe.get("steps", []) if step["plugin"] == "Duck"}
        self.feeds = sources(recipe)
        self.sums, self.keyed, self.done = {}, {}, set()
        # With a memo, each node's output key and each bus's inputs in the order they were summed.
        self.memo, self.signatures, self.parts = memo, {}, {}

    def add(self, bus, audio, part=None):
        self.sums[bus] = self.sums.get(bus, np.zeros((self.length, 2))) + audio
        self.parts.setdefault(bus, []).append(part)

    def send(self, name, audio, settings, key=None):
        self.add(settings.get("bus", "mix"), audio, key)
        for send in settings.get("sends", []):
            self.add(send["bus"], audio * 10 ** (send.get("gain_db", 0) / 20), key and [key, send.get("gain_db", 0)])

    def key(self, name):
        self.pull(name)
        return self.keyed[name]

    def signature(self, name):
        self.pull(name)
        return self.signatures[name]

    def node(self, name, source, key):
        if self.memo is None:
            return process(source(), self.rate, self.steps, name, self), None
        audio, self.signatures[name] = chain(source, self.rate, self.steps, name, self, key)
        return audio, self.signatures[name]

    def summed(self, name):
        audio, parts = self.sums.pop(name, np.zeros((self.length, 2))), self.parts.pop(name, [])
        return (lambda: audio), self.memo and None not in parts and digest("bus", self.rate, self.length, parts) or None

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
        read = soundfile.read(self.song / "stems" / name, always_2d=True)[0]

        def source():
            audio = read * 10 ** (settings.get("gain_db", 0) / 20)
            if settings.get("invert", False):
                audio = -audio
            return place(audio, settings.get("offset_samples", 0), self.length)

        # The samples read, not the file's stat: a rewrite can keep its size and mtime.
        start = self.memo and digest("stem", read.shape, hashlib.sha256(read).hexdigest(), settings.get("gain_db", 0),
                                     settings.get("invert", False), settings.get("offset_samples", 0), self.rate, self.length)
        audio, key = self.node(name, source, start)
        if name in self.keys:
            self.keyed[name] = audio
        audio = pan(audio, settings.get("pan", 0))
        if name in self.taps:
            self.tapped[name] = audio
        self.send(name, audio, settings, key and digest(key, "pan", settings.get("pan", 0)))

    def bus(self, name):
        if name in self.done:
            return
        self.done.add(name)
        for source, settings in self.feeds.items():
            if name == settings.get("bus", "mix") or name in {send["bus"] for send in settings.get("sends", [])}:
                self.pull(source)
        audio, key = self.node(name, *self.summed(name))
        if name in self.keys:
            self.keyed[name] = audio
        if name in self.taps:
            self.tapped[name] = audio
        self.send(name, audio, self.feeds[name], key)

    def render(self):
        for name in self.recipe["stems"]:
            self.stem(name)
        for name in bus_order(self.recipe):
            self.bus(name)
        return self.node("mix", *self.summed("mix"))[0]


def master_target(recipe):
    path = ROOT / "profiles" / str(recipe.get("profile")) / "profile.json"
    target = json.loads(path.read_text()).get("master_integrated_lufs") if path.exists() else None
    if not isinstance(target, (int, float)) or not math.isfinite(target):
        sys.exit(f"--master needs a numeric master_integrated_lufs in {path}")
    return target


def measure(audio, rate):
    return pyloudnorm.Meter(rate).integrated_loudness(audio), true_peak_dbtp(audio)


class Result:
    def __init__(self, recipe, song, taps=(), strict=True, boundary=None, probes=None, memo=None, tapped=None):
        self.rate, length = timeline(recipe, song)
        check(recipe, self.rate, length, strict)
        if unknown := set(taps) - set(sources(recipe)) - {MASTER_IN}:
            sys.exit(f"--tap names no stem or bus: {sorted(unknown)}")
        self.mixer = Mix(recipe, song, self.rate, length, taps, probes, memo, tapped)
        self.mix = self.mixer.render()
        self.tapped = self.mixer.tapped
        self.master_steps = [step for step in recipe.get("steps", []) if step.get("stage") == "master"]
        if not self.master_steps and MASTER_IN in taps:
            sys.exit(f"--tap {MASTER_IN} needs master steps")
        self.boundary = boundary or (self.master_steps[-1].get("id") if self.master_steps else None)

    def master(self, mix):
        """Returns the input of the boundary step (the last master step by default) and the master.

        A variant without the boundary step has nothing between the two, so a check that removes the
        limiter measures no limiting instead of whatever step now comes last.
        """
        ids = [step.get("id") for step in self.master_steps]
        split = ids.index(self.boundary) if self.boundary in ids else len(ids)
        master_in = process(mix, self.rate, self.master_steps[:split], "mix", self.mixer)
        return master_in, process(master_in, self.rate, self.master_steps[split:], "mix", self.mixer)


def render(recipe_path, start_seconds=None, end_seconds=None, final=False, taps=()):
    song = Path(recipe_path).parent
    recipe = json.loads(Path(recipe_path).read_text())
    target = master_target(recipe) if final else None
    result = Result(recipe, song, taps)
    rate, mix, tapped = result.rate, result.mix, result.tapped

    first = round((start_seconds or 0) * rate)
    last = round(end_seconds * rate) if end_seconds is not None else len(mix)
    if not 0 <= first < min(last, len(mix)):
        sys.exit(f"clip must satisfy 0 <= start < end and start < {len(mix) / rate:.2f} s")
    out = song / "renders" / "mix.wav"
    out.parent.mkdir(exist_ok=True)
    soundfile.write(out, mix[first:last], rate, subtype="FLOAT")
    written = [out]
    for name, audio in tapped.items():
        kind = "stem" if name in recipe["stems"] else "bus"
        written.append(song / "renders" / f"tap-{kind}-{name.removesuffix('.wav')}.wav")
        soundfile.write(written[-1], audio[first:last], rate, subtype="FLOAT")
    # Integrated loudness is song-wide, so a clip gets no master.
    if not result.master_steps or (first, last) != (0, len(mix)):
        if final:
            sys.exit("--master needs master steps and a whole-song render")
        return written
    master_in, mastered = result.master(mix)
    if MASTER_IN in taps:
        written.append(song / "renders" / f"tap-{MASTER_IN}.wav")
        soundfile.write(written[-1], master_in, rate, subtype="FLOAT")
    master = song / "renders" / ("master.wav" if final else "trial.wav")
    # Written aside and checked first, so a failed render never replaces a good one.
    candidate = master.with_name(master.stem + ".tmp.wav")
    try:
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
