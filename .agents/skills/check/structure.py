import re

import chains as chain_docs

AUDIO = {".wav", ".flac", ".aif", ".aiff"}
DRUM_CHAINS = {"kick", "snare", "toms", "overheads"}
SINGLE = ("drum-bus", "mix-bus", "master")
DECISIONS = {"kept", "changed", "dropped"}
CLAIMED_FIELDS = {"invert": False, "offset_samples": 0, "pan": 0}
NAME_CHAINS = [
    (r"vox|vocal", None),
    (r"kick", "kick"),
    (r"snare", "snare"),
    (r"tom", "toms"),
    (r"overhead|\boh\b|hat|cymbal|ride|crash|room", "overheads"),
    (r"bass", "bass"),
    (r"gtr|guitar", "guitars"),
    (r"synth|pad|keys|piano|sfx|\bfx\b", "synths"),
]
SOURCE_FIELDS = {"chain", "stems", "chain_reason", "split_reason", "timing"}
RECORD_FIELDS = {"move", "source", "decision", "steps", "fields", "note", "tool_reason"}


def name_chain(file):
    words = re.sub(r"[_\-.\d]+", " ", file.lower())
    for pattern, chain in NAME_CHAINS:
        if re.search(pattern, words):
            return chain, True
    return None, False


class Routing:
    def __init__(self, recipe):
        self.recipe = recipe
        self.edges = {}
        for name, settings in list(recipe["stems"].items()) + list(recipe.get("buses", {}).items()):
            self.edges.setdefault(name, set()).add(settings.get("bus", "mix"))
            self.edges[name] |= {send["bus"] for send in settings.get("sends", [])}
        self.reach = {}
        for stem in recipe["stems"]:
            seen, todo = set(), [stem]
            while todo:
                node = todo.pop()
                for following in self.edges.get(node, ()):
                    if following not in seen:
                        seen.add(following)
                        todo.append(following)
            for node in seen | {stem}:
                self.reach.setdefault(node, set()).add(stem)

    def stems_into(self, node):
        return self.reach.get(node, set())

    def path(self, stem):
        """The stem's chain of parent buses up to mix, ignoring sends."""
        nodes, node = [], self.recipe["stems"][stem].get("bus", "mix")
        while node != "mix":
            nodes.append(node)
            node = self.recipe.get("buses", {}).get(node, {}).get("bus", "mix")
        return nodes


def node_of(routing, stems):
    """The last point in the routing that carries all and only these stems, so a source's own buses are measured."""
    shared = [bus for bus in routing.path(stems[0]) if all(bus in routing.path(stem) for stem in stems[1:])]
    node = stems[0] if len(stems) == 1 else (shared[0] if shared else None)
    for bus in shared[1:] if len(stems) > 1 else shared:
        if routing.stems_into(bus) != set(stems):
            break
        node = bus
    return node


def inventory(recipe, song, errors, notes):
    sources, excluded = recipe.get("sources", {}), recipe.get("excluded", {})
    files = sorted(p.name for p in (song / "stems").iterdir() if p.suffix.lower() in AUDIO)
    owner = {}
    for name, source in sources.items():
        if unknown := set(source) - SOURCE_FIELDS:
            errors.append(f"source {name}: unknown fields {sorted(unknown)}")
        chain = source.get("chain")
        if chain not in chain_docs.parse()[0] or chain in SINGLE:
            errors.append(f"source {name}: chain must name a source chain, got {chain!r}")
        for stem in source.get("stems", []):
            if stem in owner:
                errors.append(f"{stem} is in sources {owner[stem]} and {name}")
            owner[stem] = name
            if stem not in recipe["stems"]:
                errors.append(f"source {name}: {stem} is not in the recipe's stems")
            expected, known = name_chain(stem)
            if known and expected != chain and "chain_reason" not in source:
                errors.append(f"source {name}: {stem} reads as {expected or 'a vocal'}, not {chain}; give chain_reason")
            elif not known:
                notes.append(f"{stem}: chain {chain} chosen by the recipe (no name match)")
        if "chain_reason" in source:
            notes.append(f"source {name}: chain override: {source['chain_reason']}")
        if not source.get("stems"):
            errors.append(f"source {name}: needs stems")
        if source.get("timing") and source["timing"] not in source.get("stems", []):
            errors.append(f"source {name}: timing stem {source['timing']} is not one of its stems")
    amps = {}
    for file, reason in excluded.items():
        if file in owner:
            errors.append(f"{file} is both excluded and in source {owner[file]}")
        if reason == "vocal":
            if name_chain(file) != (None, True):
                errors.append(f"excluded {file}: its name does not read as a vocal")
            continue
        amp = reason.get("guitar-di") if isinstance(reason, dict) else None
        if not amp or sources.get(owner.get(amp), {}).get("chain") != "guitars":
            errors.append(f"excluded {file}: reason must be 'vocal' or {{'guitar-di': <amp stem in a guitars source>}}")
        elif name_chain(file)[0] != "guitars" or not re.search(r"\bdi\b", re.sub(r"[_\-.\d]+", " ", file.lower())):
            errors.append(f"excluded {file}: its name does not read as a guitar DI")
        elif amp in amps:
            errors.append(f"excluded {file}: {amp} already has the DI {amps[amp]}")
        amps[amp] = file
    for file in files:
        if file not in owner and file not in excluded:
            errors.append(f"{file} is in stems/ but in no source and not excluded")
    for stem in recipe["stems"]:
        if stem not in owner:
            errors.append(f"recipe stem {stem} is in no source")
    by_chain = {}
    for name, source in sources.items():
        by_chain.setdefault(source.get("chain"), []).append(name)
    for chain, names in by_chain.items():
        if chain == "toms":
            for name in names:
                if len(sources[name].get("stems", [])) != 1:
                    errors.append(f"source {name}: each tom is its own source")
        elif len(names) > 1 and not all("split_reason" in sources[name] for name in names):
            errors.append(f"chain {chain} has several sources {names}; one source per instrument, or give each a split_reason")


def nodes(recipe, routing, errors):
    found = {}
    for name, source in recipe.get("sources", {}).items():
        stems = [stem for stem in source.get("stems", []) if stem in recipe["stems"]]
        if not stems:
            continue
        node = node_of(routing, stems)
        if node is None:
            errors.append(f"source {name}: its stems share no bus below mix; route them to one group bus")
        elif routing.stems_into(node) != set(stems):
            others = sorted(routing.stems_into(node) - set(stems))
            errors.append(f"source {name}: node {node} is also fed by {others}")
        found[name] = node
    return found


def instances(recipe):
    sources = recipe.get("sources", {})
    found = [(name, source.get("chain")) for name, source in sources.items()]
    if any(source.get("chain") in DRUM_CHAINS for source in sources.values()):
        found.append(("drum-bus", "drum-bus"))
    return found + [("mix-bus", "mix-bus"), ("master", "master")]


def fits(recipe, routing, record_source, node):
    """Whether a record may act on a stem or bus: a source on its own, the shared buses track the rest."""
    if node == "mix":
        return record_source in ("mix-bus", "master")
    reaching = routing.stems_into(node)
    sources = recipe.get("sources", {})
    feeding = {name for name, source in sources.items() if reaching & set(source.get("stems", []))}
    chains = {sources[name].get("chain") for name in feeding}
    if record_source == "drum-bus":
        returns = {send["bus"] for settings in list(recipe["stems"].values()) + list(recipe.get("buses", {}).values())
                   for send in settings.get("sends", [])}
        # Several drum chains, not several sources: the five toms' shared bus belongs to the toms.
        return chains <= DRUM_CHAINS and (len(chains) > 1 or (bool(chains) and node in returns))
    if record_source in ("mix-bus", "master"):
        return False
    # Sources of one chain may share a bus, as toms share their bus compressor.
    return record_source in feeding and chains == {sources[record_source].get("chain")}


def identity(step):
    if step["plugin"] == "Airwindows Consolidated":
        return step.get("settings", {}).get("effect")
    return step["plugin"]


def fields_of(recipe):
    found = {}
    for kind, entries in (("stems", recipe["stems"]), ("buses", recipe.get("buses", {}))):
        for name, settings in entries.items():
            if settings.get("bus", "mix") != "mix":
                found[f"{kind}.{name}.bus"] = (name, "bus")
            if kind == "stems":
                for field, default in CLAIMED_FIELDS.items():
                    if settings.get(field, default) != default:
                        found[f"stems.{name}.{field}"] = (name, field)
            for send in settings.get("sends", []):
                found[f"{kind}.{name}.sends.{send['bus']}"] = (name, "sends")
    return found


def coverage(recipe, routing, master, errors, notes):
    docs = chain_docs.parse()[0]
    steps = {step.get("id"): step for step in recipe.get("steps", [])}
    if len(steps) != len(recipe.get("steps", [])) or None in steps:
        errors.append("every step needs a unique id")
    claimable = fields_of(recipe)
    for name, settings in list(recipe["stems"].items()) + list(recipe.get("buses", {}).items()):
        targets = [send["bus"] for send in settings.get("sends", [])]
        if len(targets) != len(set(targets)):
            errors.append(f"{name}: send to one bus at most once")
    records, claimed = {}, set()
    for index, record in enumerate(recipe.get("moves", [])):
        label = f"moves[{index}] {record.get('move')} on {record.get('source')}"
        if unknown := set(record) - RECORD_FIELDS:
            errors.append(f"{label}: unknown fields {sorted(unknown)}")
        source, move = record.get("source"), record.get("move")
        if not isinstance(record.get("note"), str) or not record["note"].strip():
            errors.append(f"{label}: needs a note with the measured reason")
        claims = record.get("steps", []) + record.get("fields", [])
        claimed |= set(claims)
        if move == "balance":
            check_balance(recipe, record, steps, label, errors)
            continue
        chain = move.split(".")[0] if isinstance(move, str) else None
        if (source, chain) not in instances(recipe) or move not in docs.get(chain, {}):
            errors.append(f"{label}: no such move for that source")
            continue
        if chain == "master" and not master:
            errors.append(f"{label}: master moves start only after the mix passes review (check --master)")
            continue
        if (source, move) in records:
            errors.append(f"{label}: recorded twice")
        records[(source, move)] = record
        if record.get("decision") not in DECISIONS:
            errors.append(f"{label}: decision must be kept, changed or dropped")
        elif record["decision"] == "dropped":
            if claims:
                errors.append(f"{label}: a dropped move claims nothing")
        else:
            check_claims(recipe, routing, record, docs[chain][move], steps, claimable, label, errors, notes)
    for source, chain in instances(recipe):
        if chain == "master" and not master:
            continue
        for move in docs[chain]:
            if (source, move) not in records:
                errors.append(f"{move} on {source}: not recorded")
    by_master = {s for r in recipe.get("moves", []) if str(r.get("move", "")).startswith("master.") for s in r.get("steps", [])}
    for step_id, step in steps.items():
        if step_id not in claimed:
            errors.append(f"step {step_id}: claimed by no move")
        elif master and step.get("stage") == "master" and step_id not in by_master:
            errors.append(f"step {step_id}: the master's steps belong to master moves, not the trial master")
    for field in claimable:
        # Routing is checked through each source's node, so recording it is optional.
        if field not in claimed and claimable[field][1] != "bus":
            errors.append(f"{field}: claimed by no move")


def check_balance(recipe, record, steps, label, errors):
    for step_id in record.get("steps", []):
        step = steps.get(step_id, {})
        if step.get("plugin") != "Gain" or step.get("stage", "mix") != "mix" or step.get("target") == "mix":
            errors.append(f"{label}: balance claims only mix-stage Gain steps on a stem or bus, not {step_id}")
    for field in record.get("fields", []):
        if not re.fullmatch(r"stems\..+\.(gain_db|pan)", field):
            errors.append(f"{label}: balance claims only stem gain_db and pan, not {field}")
        elif field.rsplit(".", 1)[0].removeprefix("stems.") not in recipe["stems"]:
            errors.append(f"{label}: no stem for {field}")


def check_claims(recipe, routing, record, bullets, steps, claimable, label, errors, notes):
    source = record["source"]
    plugins, field_kinds = chain_docs.tools(bullets)
    chain = record["move"].split(".")[0]
    matched = False
    for step_id in record.get("steps", []):
        step = steps.get(step_id)
        if step is None:
            errors.append(f"{label}: no step {step_id}")
            continue
        stage = step.get("stage", "mix")
        if stage == "master" and chain not in ("master", "mix-bus"):
            errors.append(f"{label}: only master and mix-bus moves claim master steps, not {step_id}")
        if chain == "master" and stage != "master":
            errors.append(f"{label}: master moves claim only master-stage steps, not {step_id}")
        target = step["target"]
        key = step["plugin"] == "Duck" and step.get("settings", {}).get("key")
        # A Duck may act on another source when this source is its key, as the kick ducks the bass.
        if target == "mix" and source not in ("mix-bus", "master"):
            errors.append(f"{label}: only mix-bus and master moves act on mix, not {step_id}")
        elif not fits(recipe, routing, source, target) and not (key and fits(recipe, routing, source, key)):
            errors.append(f"{label}: step {step_id} acts on {target}, outside this source")
        elif key and not fits(recipe, routing, source, target):
            notes.append(f"{label}: Duck {step_id} acts on {target}, keyed from {key}, outside this source")
        if identity(step) in plugins:
            matched = True
        elif step["plugin"] != "Gain" and not record.get("tool_reason"):
            errors.append(f"{label}: {identity(step)} is not in the move's Tool list; use one or give tool_reason")
    for field in record.get("fields", []):
        if field in claimable:
            name, kind = claimable[field]
        elif field.startswith("stems.") and field.endswith(".gain_db") and field[len("stems.") : -len(".gain_db")] in recipe["stems"]:
            name, kind = field[len("stems.") : -len(".gain_db")], "gain_db"
        else:
            errors.append(f"{label}: {field} is not a set stem field or send")
            continue
        drums = {s for src in recipe.get("sources", {}).values() if src.get("chain") in DRUM_CHAINS for s in src["stems"]}
        # drum-bus.groups routes every drum stem, so its bus fields reach any drum source.
        grouping = source == "drum-bus" and kind == "bus" and routing.stems_into(name) <= drums
        if not grouping and not fits(recipe, routing, source, name):
            errors.append(f"{label}: {field} is outside this source")
        if kind in field_kinds:
            matched = True
        elif not record.get("tool_reason"):
            errors.append(f"{label}: {kind} is not in the move's Tool list; give tool_reason")
    if record.get("tool_reason"):
        notes.append(f"{label}: tool exception: {record['tool_reason']}")
        matched = matched or bool(record.get("steps") or record.get("fields"))
    if not matched:
        errors.append(f"{label}: {record['decision']} needs a step or field from the move's Tool list")
