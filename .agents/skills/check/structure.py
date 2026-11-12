import re
from pathlib import Path

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
ROLE_WORDS = [
    ("di", r"\bdi\b"), ("amp", r"\bamp\b"), ("top", r"\b(up|top)\b"), ("bottom", r"\b(down|bottom|bot)\b"),
    ("sample", r"sample"), ("sub", r"\bsub\b"), ("in", r"\bin\b"), ("out", r"\bout\b"), ("hat", r"hat|\bhh\b"),
    ("spot", r"ride|crash|china|splash|cymbal"), ("room", r"room"), ("lead", r"lead|solo"),
]
SOURCE_FIELDS = {"chain", "stems", "chain_reason", "split_reason", "timing", "roles", "role_reason", "role"}
RECORD_FIELDS = {"move", "source", "decision", "steps", "fields", "note", "tool_reason", "trial", "override", "take"}
SYNTH_ROLES = {"bass", "lead", "pad"}


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
    if not any(source.get("chain") in DRUM_CHAINS for source in sources.values()):
        errors.append("every song has drums: give the drum stems a source")
    for name, source in sources.items():
        if name in SINGLE + ("balance",):
            errors.append(f"source {name}: that name belongs to the {name} moves; name the source after its instrument")
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
        # DIs are never used, so one can't sit in a source: exclude it as "di". Read from the name alone, so no
        # role override can bring one back.
        for stem, role in roles_of({k: v for k, v in source.items() if k != "roles"}).items():
            if role == "di":
                errors.append(f"source {name}: {stem} is a DI; DIs are never used, so exclude it as 'di'")
        if source.get("timing") and source["timing"] not in source.get("stems", []):
            errors.append(f"source {name}: timing stem {source['timing']} is not one of its stems")
        check_roles(name, source, errors, notes)
    for file, reason in excluded.items():
        if file in owner:
            errors.append(f"{file} is both excluded and in source {owner[file]}")
        if reason == "vocal":
            if name_chain(file) != (None, True):
                errors.append(f"excluded {file}: its name does not read as a vocal")
        elif reason == "di":
            if roles_of({"stems": [file]}).get(file) != "di":
                errors.append(f"excluded {file}: its name does not read as a DI")
        else:
            errors.append(f"excluded {file}: reason must be 'vocal' or 'di'")
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


def roles_of(source):
    found = {}
    for stem in source.get("stems", []):
        # CamelCase names (SnareUp, KickIn, ElecGtr3DI) split into words before matching.
        name = Path(stem).stem
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
        words = re.sub(r"[_\-.\d]+", " ", spaced.lower())
        role = next((name for name, pattern in ROLE_WORDS if re.search(pattern, words)), None)
        # An all-caps name runs DI into the word before it (GTRDI, GTRLEADDI), and DI wins over any role word in it;
        # MIDI is not a DI.
        if re.search(r"(?<!MI)DI$", re.sub(r"[_\-.\d]+", "", name)):
            role = "di"
        # A guitar track's role is the part it plays: recording words (amp, room, top) don't change it.
        if source.get("chain") == "guitars" and role != "di":
            role = "lead" if re.search(r"lead|solo", words) else "rhythm"
        found[stem] = source.get("roles", {}).get(stem, role)
    return found


def check_roles(name, source, errors, notes):
    named = roles_of({k: v for k, v in source.items() if k != "roles"})
    changed = {stem: role for stem, role in source.get("roles", {}).items() if named.get(stem) != role}
    if unknown := set(source.get("roles", {})) - set(source.get("stems", [])):
        errors.append(f"source {name}: roles name stems it doesn't have: {sorted(unknown)}")
    # File names set roles; a recipe that relabels one could switch a move off, so it must say why.
    if changed and "role_reason" not in source:
        errors.append(f"source {name}: roles {changed} differ from the file names; give role_reason")
    elif changed:
        notes.append(f"source {name}: role override {changed}: {source['role_reason']}")
    if source.get("chain") == "synths" and source.get("role") not in SYNTH_ROLES:
        errors.append(f"source {name}: a synths source needs role bass, lead or pad")
    elif "role" in source and source.get("chain") != "synths":
        errors.append(f"source {name}: role is only for synths sources")


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


# The chains a synth's role adds on its own source, and which of their moves it gets.
ROLE_CHAINS = {"bass": ("bass", None), "lead": ("guitars", {"guitars.lead"})}


def instances(recipe):
    sources = recipe.get("sources", {})
    found = [(name, source.get("chain")) for name, source in sources.items()]
    found += [(name, ROLE_CHAINS[source["role"]][0]) for name, source in sources.items()
              if source.get("chain") == "synths" and source.get("role") in ROLE_CHAINS]
    if any(source.get("chain") in DRUM_CHAINS for source in sources.values()):
        found.append(("drum-bus", "drum-bus"))
    return found + [("mix-bus", "mix-bus"), ("master", "master")]


def moves_for(recipe, source, chain, docs):
    own = recipe.get("sources", {}).get(source, {})
    if own.get("chain") == "synths" and chain != "synths":
        return [move for move in docs[chain] if ROLE_CHAINS[own["role"]][1] is None or move in ROLE_CHAINS[own["role"]][1]]
    return list(docs[chain])


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


def coverage(recipe, routing, master, errors, notes, song=None):
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
        if (source, chain) not in instances(recipe) or move not in docs.get(chain, {}) \
                or move not in moves_for(recipe, source, chain, docs):
            errors.append(f"{label}: no such move for that source")
            continue
        if chain == "master" and not master:
            errors.append(f"{label}: master moves start only after the mix passes review (check --master)")
            continue
        if (source, move) in records:
            errors.append(f"{label}: recorded twice")
        take = record.get("take")
        moved = {stem for stem in recipe["stems"] if any(field.startswith(f"stems.{stem}.") for field in record.get("fields", []))}
        if take is not None and (move != "guitars.align" or not isinstance(take, list) or len(set(take)) != len(take) or len(take) < 2
                                 or not set(take) <= set(recipe.get("sources", {}).get(source, {}).get("stems", []))
                                 or not moved <= set(take)):
            errors.append(f"{label}: take is for guitars.align: two or more different stems of the source, the reference first, "
                          "including every stem the record moves")
        records[(source, move)] = record
        if record.get("decision") not in DECISIONS:
            errors.append(f"{label}: decision must be kept, changed or dropped")
        elif record["decision"] == "dropped":
            if claims:
                errors.append(f"{label}: a dropped move claims nothing")
            if "trial" in record:
                check_trial(recipe, song, record, docs[chain][move], label, errors, notes)
        elif "trial" in record:
            errors.append(f"{label}: only a dropped move gives a trial")
        else:
            check_claims(recipe, routing, record, docs[chain][move], steps, claimable, label, errors, notes)
    for source, chain in instances(recipe):
        if chain == "master" and not master:
            continue
        for move in moves_for(recipe, source, chain, docs):
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


PATCH_FIELDS = {"steps", "buses", "sends", "routing"}


def apply_patch(recipe, patch):
    """The recipe with a record trial's steps (placed before or after a step id, else last), buses, sends and routing."""
    import copy

    variant = copy.deepcopy(recipe)
    # The patch is copied too: attaching its buses by reference would let later edits write back into the record.
    patch = copy.deepcopy(patch)
    steps = variant.get("steps", [])
    for step in patch.get("steps", []):
        anchor = step.get("before") or step.get("after")
        clean = {k: v for k, v in step.items() if k not in ("before", "after")}
        ids = [s.get("id") for s in steps]
        index = ids.index(anchor) + (0 if step.get("before") else 1) if anchor in ids else len(steps)
        steps = steps[:index] + [clean] + steps[index:]
    variant["steps"] = steps
    for name, settings in patch.get("buses", {}).items():
        variant.setdefault("buses", {})[name] = settings
    for name, sends in patch.get("sends", {}).items():
        entries = variant["stems"] if name in variant["stems"] else variant.setdefault("buses", {})
        entries.setdefault(name, {}).setdefault("sends", []).extend(sends)
    for name, bus in patch.get("routing", {}).items():
        entries = variant["stems"] if name in variant["stems"] else variant.setdefault("buses", {})
        entries.setdefault(name, {})["bus"] = bus
    return variant


def returns_of(recipe):
    """Buses fed by sends: a reverb move may send into the kit's shared return."""
    return {send["bus"] for settings in list(recipe["stems"].values()) + list(recipe.get("buses", {}).values())
            for send in settings.get("sends", [])}


def check_trial(recipe, song, record, bullets, label, errors, notes):
    """A record trial is a real try of its move: a listed processor, on the record's own source, in a graph that renders."""
    import contracts
    import render

    patch = record["trial"]
    if record["move"] in contracts.DEFAULT_TRIALS:
        errors.append(f"{label}: an always-tried move is judged by the check's own starting trial; drop the trial")
        return
    _, field_kinds = chain_docs.tools(bullets)
    # A move whose Tool is a send (snare.reverb, toms.reverb) is tried by its send alone.
    by_send = isinstance(patch, dict) and "sends" in field_kinds and isinstance(patch.get("sends"), dict) \
        and any(isinstance(sends, list) and sends for sends in patch["sends"].values())
    if not isinstance(patch, dict) or set(patch) - PATCH_FIELDS or not (patch.get("steps") or by_send):
        errors.append(f"{label}: trial needs steps (or sends, for a send move), and only {sorted(PATCH_FIELDS)}")
        return
    ids = {step.get("id") for step in recipe.get("steps", [])}
    new = [step.get("id") for step in patch.get("steps", [])]
    if None in new or len(set(new)) != len(new) or set(new) & ids:
        errors.append(f"{label}: trial steps need new, unique ids")
        return
    if set(patch.get("buses", {})) & (set(render.bus_order(recipe)) | set(recipe["stems"])):
        errors.append(f"{label}: trial buses must be new")
        return
    variant = apply_patch(recipe, patch)
    routing = Routing(variant)
    source = record["source"]
    own = set(recipe.get("sources", {}).get(source, {}).get("stems", []))
    for name in list(patch.get("sends", {})) + list(patch.get("routing", {})):
        if name not in patch.get("buses", {}) and not fits(recipe, Routing(recipe), source, name) and name not in own:
            errors.append(f"{label}: trial routes or sends from {name}, outside this source")
    for name, bus in patch.get("routing", {}).items():
        if name not in own or bus not in patch.get("buses", {}):
            errors.append(f"{label}: trial routing may only send this source's stems to the trial's own buses")
    plugins, _ = chain_docs.tools(bullets)
    if not by_send and not any(identity(step) in plugins for step in patch["steps"]):
        errors.append(f"{label}: trial needs a step from the move's Tool list")
    if by_send:
        reverbs = {step["target"] for step in variant.get("steps", []) if any(w in step["plugin"] for w in ("Reverb", "EarlyReflections"))}
        for name, sends in patch["sends"].items():
            if any(send.get("bus") not in reverbs for send in sends):
                errors.append(f"{label}: a send trial must feed a reverb return")
    off_list = [step.get("id") for step in patch.get("steps", []) if identity(step) not in plugins and step.get("plugin") != "Gain"]
    if off_list and not record.get("tool_reason"):
        errors.append(f"{label}: trial steps {off_list} are not in the move's Tool list; use one or give tool_reason")
    elif off_list:
        notes.append(f"{label}: trial tool exception for {off_list}: {record['tool_reason']}")
    for name, settings in patch.get("buses", {}).items():
        settings = settings if isinstance(settings, dict) else {}
        destinations = [settings.get("bus", "mix")] + [send.get("bus") for send in settings.get("sends", [])]
        for destination in destinations:
            if destination != "mix" and destination not in patch.get("buses", {}) and not fits(recipe, Routing(recipe), source, destination):
                errors.append(f"{label}: trial bus {name} feeds {destination}, outside this source")
    for name, sends in patch.get("sends", {}).items():
        for send in sends:
            destination = send.get("bus")
            if destination not in patch.get("buses", {}) and not fits(recipe, Routing(recipe), source, destination) \
                    and not (destination in returns_of(recipe) and record["move"].endswith(".reverb")):
                errors.append(f"{label}: trial sends {name} to {destination}, outside this source")
    for step in patch.get("steps", []):
        target = step.get("target")
        key = step.get("plugin") == "Duck" and step.get("settings", {}).get("key")
        if key:
            check_duck(variant, routing, record, step, label, errors)
        elif target not in patch.get("buses", {}) and not fits(variant, routing, source, target):
            errors.append(f"{label}: trial step {step.get('id')} acts on {target}, outside this source")
    try:
        render.check(variant, *render.timeline(variant, song))
    except SystemExit as error:
        errors.append(f"{label}: trial does not render: {error}")


def check_duck(recipe, routing, record, step, label, errors):
    import contracts

    sources = recipe.get("sources", {})
    spec = contracts.CONTRACTS.get(record["move"], {}).get("duck")

    def side(node):
        stems = routing.stems_into(node)
        owners = {name for name, src in sources.items() if stems & set(src.get("stems", []))}
        roles = {role for name in owners for stem, role in roles_of(sources[name]).items() if stem in stems}
        # A bass synth is the bass to the moves that duck or read the bass.
        chains = {"bass" if sources[name].get("chain") == "synths" and sources[name].get("role") == "bass"
                  else sources[name].get("chain") for name in owners}
        return owners, chains, roles

    key_owners, key_chains, key_roles = side(step["settings"]["key"])
    target_owners, target_chains, target_roles = side(step["target"])
    where = f"{label}: Duck {step['id']} on {step['target']} keyed from {step['settings']['key']}"
    if spec is None:
        if key_owners != {record["source"]} or target_owners != {record["source"]}:
            errors.append(f"{where} reaches outside its source; only kick.duck-bass, overheads.snare-control and guitars.lead-room duck another part")
        for move, contract in contracts.CONTRACTS.items():
            duck = contract.get("duck")
            if duck and key_chains == {duck["key"][0]} and target_chains == {duck["target"][0]} \
                    and (not duck["key"][1] or key_roles == {duck["key"][1]}) and (not duck["target"][1] or target_roles == {duck["target"][1]}):
                errors.append(f"{where}: that is {move}'s Duck; record it there")
        return
    for (chain, role), chains, roles, what in ((spec["key"], key_chains, key_roles, "key"), (spec["target"], target_chains, target_roles, "target")):
        if chains != {chain} or (role and roles != {role}):
            errors.append(f"{where}: its {what} must be the {chain}{' ' + role if role else ''} only")
    # Each Duck move lives on one side: kick.duck-bass on the kick (its key), overheads.snare-control on the overheads (its target).
    owners = key_owners if spec.get("owner", "key") == "key" else target_owners
    if record["source"] not in owners:
        errors.append(f"{where}: record it on the {spec.get('owner', 'key')}'s source")


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
        if target == "mix" and source not in ("mix-bus", "master"):
            errors.append(f"{label}: only mix-bus and master moves act on mix, not {step_id}")
        elif key:
            check_duck(recipe, routing, record, step, label, errors)
        elif not fits(recipe, routing, source, target):
            errors.append(f"{label}: step {step_id} acts on {target}, outside this source")
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
