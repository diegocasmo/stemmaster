"""Reuse a contract's outcome after a recipe edit that can't change it.

A contract's evaluation reads three kinds of input: the recipe's structure (routing, step ids, targets and plugins,
sources, sections, offsets), the values of other recipe fields (step settings, stem and bus levels, pans, polarity,
send levels), and signals (taps, the mix, the trial master, step probes) of the variants it renders. While a contract
is evaluated, each value it reads outside the renderer is recorded with what it read, and each signal with a key of
everything upstream that decides its samples (the node memo's own idea of a key, computed here without rendering).
After an edit, a contract whose structure key, values and signal keys all match reuses its outcome: its evaluation
is a function of exactly these, so it would compute the same outcome again.

A contract that reads anything this can't name (a render made during its evaluation, a step that reads a file) keeps
only the exact cache key, as before.
"""
import contextlib
import hashlib
import json

import render

STRUCTURAL = {"stems": {"bus", "sends", "offset_samples"}, "buses": {"bus", "sends"}, "send": {"bus"}, "settings": set()}
_STATE = {"trace": None, "quiet": 0}
MISSING = ["__missing__"]


def digest(*parts):
    with quiet():
        return render.digest(*parts)


@contextlib.contextmanager
def quiet():
    """Reads made here (renders, cache keys) are not the contract's own: their effect reaches it through signals."""
    _STATE["quiet"] += 1
    try:
        yield
    finally:
        _STATE["quiet"] -= 1


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in dict.items(value)}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class Tracked(dict):
    """A recipe entry whose non-structural reads, made while a contract is evaluated, are recorded with their values.

    A key the evaluation wrote itself, on a copy, is not recorded again: what it holds comes from reads already
    recorded. A write to a variant itself would outlive the contract, so that contract keeps only its exact key.
    """

    __slots__ = ("owner", "path", "written", "copied")

    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.owner, self.path, self.written, self.copied = None, None, set(), False

    def _note(self, op, key=None):
        trace = _STATE["trace"]
        if trace is None or _STATE["quiet"] or self.owner is None:
            return
        structural = STRUCTURAL[self.path[0]]
        if op == "all":
            value = {k: plain(v) for k, v in dict.items(self) if k not in structural and k not in self.written}
        elif key in structural or key in self.written:
            return
        elif op == "has":
            value = dict.__contains__(self, key)
        else:
            value = plain(dict.__getitem__(self, key)) if dict.__contains__(self, key) else MISSING
        trace.value(self.owner, self.path, op, key, value)

    def _write(self, keys):
        if not self.copied and _STATE["trace"] is not None and not _STATE["quiet"]:
            _STATE["trace"].give_up(f"wrote {sorted(map(str, keys))} into a variant")
        self.written |= set(keys)

    def __getitem__(self, key):
        self._note("get", key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._note("get", key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._note("has", key)
        return dict.__contains__(self, key)

    def __setitem__(self, key, value):
        self._write([key])
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._write([key])
        dict.__delitem__(self, key)

    def setdefault(self, key, default=None):
        self._note("get", key)
        self._write([key])
        return dict.setdefault(self, key, default)

    def pop(self, key, *default):
        self._note("get", key)
        self._write([key])
        return dict.pop(self, key, *default)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        self._write(changes)
        dict.update(self, changes)

    def __ior__(self, other):
        self.update(other)
        return self

    def popitem(self):
        self._note("all")
        self._write(dict.keys(self))
        return dict.popitem(self)

    def clear(self):
        self._write(dict.keys(self))
        dict.clear(self)

    # Each of these reveals every key, so it records the whole entry.
    def __iter__(self):
        self._note("all")
        return dict.__iter__(self)

    def keys(self):
        self._note("all")
        return dict.keys(self)

    def values(self):
        self._note("all")
        return dict.values(self)

    def items(self):
        self._note("all")
        return dict.items(self)

    def __len__(self):
        self._note("all")
        return dict.__len__(self)

    def __eq__(self, other):
        self._note("all")
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._note("all")
        return dict.__ne__(self, other)

    def __repr__(self):
        self._note("all")
        return dict.__repr__(self)

    def __reversed__(self):
        self._note("all")
        return dict.__reversed__(self)

    def copy(self):
        self._note("all")
        return dict(dict.items(self))

    def __or__(self, other):
        self._note("all")
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        self._note("all")
        return other | dict(dict.items(self))

    def __deepcopy__(self, memo):
        import copy

        out = Tracked({k: copy.deepcopy(v, memo) for k, v in dict.items(self)})
        out.owner, out.path, out.written, out.copied = self.owner, self.path, set(self.written), True
        return out

    def __reduce__(self):
        return _rebuild, (dict(dict.items(self)), self.owner, self.path, set(self.written), self.copied)

    __hash__ = None


def _rebuild(content, owner, path, written, copied):
    out = Tracked(content)
    out.owner, out.path, out.written, out.copied = owner, path, written, copied
    return out


def _wrap(entries, index, path, owner):
    found = entries[index]
    if not isinstance(found, dict):
        return
    if not isinstance(found, Tracked):
        found = Tracked(dict.items(found))
        list.__setitem__(entries, index, found) if isinstance(entries, list) else dict.__setitem__(entries, index, found)
    found.owner, found.path, found.copied, found.written = owner, path, False, set()


def track(recipe, owner):
    """Makes `recipe`'s settings, stem, bus and send entries record reads as `owner`'s. Its structure is untouched."""
    for index, step in enumerate(dict.get(recipe, "steps", [])):
        if isinstance(step, dict) and "settings" in step:
            _wrap(step, "settings", ("settings", index), owner)
    for kind in ("stems", "buses"):
        entries = dict.get(recipe, kind, {})
        for name in list(dict.keys(entries)):
            _wrap(entries, name, (kind, name), owner)
            for index, _ in enumerate(dict.get(dict.__getitem__(entries, name), "sends", []) or []):
                _wrap(dict.get(dict.__getitem__(entries, name), "sends"), index, ("send", kind, name, index), owner)
    return recipe


def lookup(recipe, path):
    try:
        if path[0] == "settings":
            return dict.__getitem__(dict.__getitem__(recipe, "steps")[path[1]], "settings")
        if path[0] == "send":
            return dict.__getitem__(dict.__getitem__(dict.__getitem__(recipe, path[1]), path[2]), "sends")[path[3]]
        return dict.__getitem__(dict.__getitem__(recipe, path[0]), path[1])
    except (KeyError, IndexError, TypeError):
        return None


def observe(entry, path, op, key):
    if not isinstance(entry, dict):
        return ["no entry"]
    structural = STRUCTURAL[path[0]]
    if op == "all":
        return {k: plain(v) for k, v in dict.items(entry) if k not in structural}
    if op == "has":
        return dict.__contains__(entry, key)
    return plain(dict.__getitem__(entry, key)) if dict.__contains__(entry, key) else MISSING


def structure(recipe):
    """The recipe without the values contracts' reads are recorded for: those are checked one by one instead."""
    out = plain({k: v for k, v in dict.items(recipe) if k not in ("moves", "notes", "reviews")})
    text = {"note", "reason", "review", "tool_reason", "chain_reason", "split_reason", "role_reason"}
    out["steps"] = [{k: v for k, v in step.items() if k not in text and k != "settings"} for step in out.get("steps", [])]
    for kind in ("stems", "buses"):
        out[kind] = {name: {k: ([{"bus": send.get("bus")} for send in v] if k == "sends" else v)
                            for k, v in entry.items() if k in STRUCTURAL[kind]}
                     for name, entry in out.get(kind, {}).items()}
    if "sources" in out:
        out["sources"] = {name: {k: v for k, v in source.items() if k not in text} for name, source in out["sources"].items()}
    return out


class DryMix(render.Mix):
    """The render's walk, in its order, with each node's key in place of its samples (None where no key exists)."""

    def __init__(self, recipe, song, rate, length, taps, probes):
        super().__init__(recipe, song, rate, length, taps, probes, None)
        self.probe_keys = {}

    def stem(self, name):
        if name in self.done:
            return
        self.done.add(name)
        settings = self.recipe["stems"][name]
        # The stem files are in every contract's static key, so the name stands for their samples.
        start = digest("stem", name, plain(settings), self.rate, self.length)
        key = self.node(name, None, start)[1]
        if name in self.keys:
            self.keyed[name] = key
        if name in self.taps:
            self.tapped[name] = key and digest(key, "tap", plain(settings))
        self.send(name, None, settings, key)

    def node(self, name, source, key):
        for step in self.steps:
            if step["target"] != name:
                continue
            upstream = None
            if step["plugin"] == "Duck":
                # A keyed step renders its key's node here, which also decides the order buses sum in.
                self.pull(step["settings"]["key"])
                upstream = self.signatures.get(step["settings"]["key"])
            before = key
            text = {"note", "reason", "review", "tool_reason", "chain_reason", "split_reason", "role_reason"}
            whole = {k: plain(v) for k, v in dict.items(step) if k not in text}
            reads_file = render.names_file(step)
            key = None if key is None or reads_file or (step["plugin"] == "Duck" and upstream is None) else \
                digest(key, step["plugin"], whole, upstream, plain(self.sections) if step["plugin"] == "Ride" else None)
            if step.get("id") in self.probes:
                self.probe_keys[step["id"]] = before and key and digest("probe", before, key)
        self.signatures[name] = key
        return None, key

    def send(self, name, audio, settings, key=None):
        self.add(settings.get("bus", "mix"), None, key)
        for send in settings.get("sends", []):
            self.add(send["bus"], None, key and [key, plain(send)])

    def add(self, bus, audio, part=None):
        self.parts.setdefault(bus, []).append(part)

    def summed(self, name):
        parts = self.parts.pop(name, [])
        return None, (None if None in parts else digest("bus", self.rate, self.length, parts))

    def bus(self, name):
        if name in self.done:
            return
        self.done.add(name)
        for source, settings in self.feeds.items():
            if name == settings.get("bus", "mix") or name in {send["bus"] for send in settings.get("sends", [])}:
                self.pull(source)
        key = self.node(name, *self.summed(name))[1]
        if name in self.keys:
            self.keyed[name] = key
        if name in self.taps:
            self.tapped[name] = key and digest(key, "tap", plain(self.feeds[name]))
        self.send(name, None, self.feeds[name], key)

    def render(self):
        for name in self.recipe["stems"]:
            self.stem(name)
        for name in render.bus_order(self.recipe):
            self.bus(name)
        return self.node("mix", *self.summed("mix"))[1]


class Keys:
    """Every signal a variant's view can be read for, keyed by what decides its samples, made once per variant."""

    def __init__(self, variants):
        self.variants, self.found = variants, {}

    def of(self, key):
        if key not in self.found:
            variants = self.variants
            recipe = variants.recipes[key]
            with quiet():
                taps = sorted(({tap for tap, _ in variants.layout[0].values()} | variants.taps) & set(render.sources(recipe)))
                rate, length = render.timeline(recipe, variants.song)
                mix = DryMix(recipe, variants.song, rate, length, taps, set(variants.probes.get(key, {})))
                mixed = mix.render()
                masters = [plain(step) for step in recipe.get("steps", []) if step.get("stage") == "master"]
                # A file's contents can change under the same path, so a master that reads one has no key.
                if any(render.names_file(step) for step in masters):
                    mixed_master = None
                else:
                    mixed_master = mixed
                master = mixed_master and digest("master", mixed_master, masters, variants.target, variants.boundary)
            found = {f"tap:{name}": mix.tapped.get(name, "untapped") for name in render.sources(recipe)}
            found |= {"mix": mixed, "premaster": mixed, "trial": master, "master-in": master,
                      "actual": mixed_master and digest("actual", mixed_master, masters, variants.boundary)}
            # A master step's probe is read on the matched master pass; a mix step's on its own input and output.
            ids = {step.get("id") for step in recipe.get("steps", []) if step.get("stage") == "master"}
            found |= {f"probe:{i}": (master and digest("probe", master, i)) if i in ids else mix.probe_keys.get(i)
                      for i in variants.probes.get(key, {})}
            self.found[key] = found
        return self.found[key]

    def signal(self, key, name):
        found = self.of(key)
        name = name if name in ("mix", "premaster", "trial", "master-in", "actual") or name.startswith("probe:") else f"tap:{name}"
        # A name no view taps reads as None, which depends on nothing.
        return found.get(name, "untapped" if not name.startswith("probe:") else "unprobed")


class Trace:
    """What one contract's evaluation reads: recipe values by variant and path, and signals by variant and name."""

    def __init__(self):
        self.values, self.signals, self.complex = {}, {}, None

    def value(self, owner, path, op, key, value):
        self.values.setdefault(json.dumps([owner, list(path), op, key]), value)

    def signal(self, owner, name):
        self.signals.setdefault(json.dumps([owner, name]), None)

    def give_up(self, why):
        self.complex = self.complex or why


def roles(keys):
    out = {"current": "current"}
    for role, key in keys.items():
        if isinstance(key, str):
            out.setdefault(key, role)
    return out


@contextlib.contextmanager
def recording():
    trace = Trace()
    _STATE["trace"] = trace
    try:
        yield trace
    finally:
        _STATE["trace"] = None


def reading(owner, name):
    trace = _STATE["trace"]
    if trace is not None and not _STATE["quiet"]:
        if owner is None:
            trace.give_up(f"a read of {name} on a view with no variant")
        else:
            trace.signal(owner, name)


def give_up(why):
    if _STATE["trace"] is not None:
        _STATE["trace"].give_up(why)


def finish(trace, keys, variant_keys, static, payload):
    """The trace file for a contract, or None when its outcome rests on reads no key names."""
    if trace.complex:
        return None
    by_role = roles(variant_keys)
    values, signals = [], []
    for spec, value in trace.values.items():
        owner, path, op, key = json.loads(spec)
        if owner not in by_role:
            return None
        values.append([by_role[owner], path, op, key, value])
    for spec in trace.signals:
        owner, name = json.loads(spec)
        if owner not in by_role:
            return None
        found = keys.signal(owner, name)
        if found is None:
            return None
        signals.append([by_role[owner], name, found])
    # The outcome names its variants by key; anything else naming one would be stale after an edit.
    named = {k for k in variant_keys.values() if isinstance(k, str) and k != "current"}
    rest = {k: v for k, v in payload.items() if k not in ("needed_read_on", "with", "without")}
    text = json.dumps(rest)
    if any(name in text for name in named):
        return None
    return {"static": static, "values": values, "signals": signals, "payload": payload}


def seal(value):
    """The JSON of `value` under its sha256, so a damaged file reads as a miss, never as other numbers."""
    text = json.dumps(value)
    return f"{hashlib.sha256(text.encode()).hexdigest()}\n{text}"


def unseal(path):
    """What `seal` wrote at `path`, or None when the file is missing, unreadable or damaged."""
    try:
        found, text = path.read_text().split("\n", 1)
        if hashlib.sha256(text.encode()).hexdigest() != found:
            return None
        return json.loads(text)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def valid(trace):
    """True when `trace` has the shape `finish` writes."""
    return (isinstance(trace, dict) and isinstance(trace.get("static"), str) and isinstance(trace.get("payload"), dict)
            and isinstance(trace.get("values"), list) and isinstance(trace.get("signals"), list)
            and all(isinstance(v, list) and len(v) == 5 and isinstance(v[1], list) for v in trace["values"])
            and all(isinstance(v, list) and len(v) == 3 for v in trace["signals"]))


def holds(trace, keys, variant_keys, variants, static):
    """True when every input `trace` recorded is the same for this recipe."""
    if trace.get("static") != static:
        return False
    by_role = {"current": "current"} | {role: key for role, key in variant_keys.items() if isinstance(key, str)}
    for role, path, op, key, value in trace["values"]:
        if role not in by_role or observe(lookup(variants.recipes[by_role[role]], path), path, op, key) != value:
            return False
    for role, name, found in trace["signals"]:
        if role not in by_role or keys.signal(by_role[role], name) != found:
            return False
    return True
