import copy
import json
import re

import chains
import contracts
import evidence
import outcomes as outcome_tools
import structure
from structure import Routing, apply_patch, identity

KEPT = ("kept", "changed")
NO_FAULT = (outcome_tools.FALSE, outcome_tools.SKIPPED)
TOOL_REASON = "the check's starting trial (contracts.DEFAULT_TRIALS), kept as the check measured it"
# README's Order: drums, then bass with the drums playing, then guitars, then synths.
SOURCE_ORDER = ["kick", "snare", "toms", "overheads", "bass", "guitars", "synths"]
LEVELS = {"drum-bus": 2, "mix-bus": 3, "master": 4}
FRAGILE = 0.25
# Leaves whose bar is an equality or a count, where a distance from the bar says nothing about how close a flip is.
NOT_GRADED = {"stem_count", "take_sum", "loudness_error"}
GLOBAL_MEASURES = {"loud_rel", "off_hit_level", "mono_lufs", "lowest_notes_level", "hit_swing", "section_error",
                   "section_error_change", "delay_gap_tail", "alt_off_hit", "group_ratio_range", "dip", "crest_loss",
                   "range_outside", "range_outside_abs", "edge_bands_outside", "any_outside", "limiter_work", "true_peak",
                   "loudness_error", "bands_in_range", "dip_vs_tried"}


def formatted(recipe, text):
    """The recipe as the repo writes it, or None when `text` isn't in that form (so a rewrite would touch every line)."""
    for ascii_only in (False, True):
        if json.dumps(recipe, indent=2, ensure_ascii=ascii_only) + "\n" == text:
            return lambda value, ascii_only=ascii_only: json.dumps(value, indent=2, ensure_ascii=ascii_only) + "\n"
    return None


def trial_patch(recipe, variant):
    """What a trial variant adds to the recipe, as a patch apply_patch rebuilds that variant from, or None."""
    old = {step.get("id") for step in recipe.get("steps", [])}
    steps = variant.get("steps", [])
    patch = {"steps": [], "buses": {}, "sends": {}, "routing": {}}
    for index, step in enumerate(steps):
        if step.get("id") in old:
            continue
        if index:
            anchor = {"after": steps[index - 1]["id"]}
        else:
            following = next((s["id"] for s in steps if s.get("id") in old), None)
            anchor = {"before": following} if following else {}
        patch["steps"].append(dict(step) | anchor)
    for kind in ("stems", "buses"):
        before = recipe.get(kind, {})
        for name, settings in variant.get(kind, {}).items():
            if name not in before:
                if kind == "stems":
                    return None
                patch["buses"][name] = settings
                continue
            old_sends = before[name].get("sends", [])
            sends = settings.get("sends", [])
            if sends[: len(old_sends)] != old_sends:
                return None
            if sends[len(old_sends):]:
                patch["sends"][name] = sends[len(old_sends):]
            if settings.get("bus", "mix") != before[name].get("bus", "mix"):
                patch["routing"][name] = settings["bus"]
    patch = {key: value for key, value in patch.items() if value}
    # The patch must rebuild exactly the measured variant, or the kept recipe would render something the check never read.
    rebuilt = apply_patch(recipe, patch)
    if evidence.fingerprint(evidence.numeric(rebuilt)) != evidence.fingerprint(evidence.numeric(variant)):
        return None
    return patch


def claims_of(recipe, patch):
    """The steps and sends a kept trial adds, as its record claims them."""
    fields = []
    for name, sends in patch.get("sends", {}).items():
        kind = "stems" if name in recipe["stems"] else "buses"
        fields += [f"{kind}.{name}.sends.{send['bus']}" for send in sends]
    for name, settings in patch.get("buses", {}).items():
        fields += [f"buses.{name}.sends.{send['bus']}" for send in settings.get("sends", [])]
    return [step["id"] for step in patch.get("steps", [])], fields


def off_tool(record, patch):
    move = record["move"]
    bullets = chains.parse()[0][move.split(".")[0]][move]
    plugins, _ = chains.tools(bullets)
    return [step["id"] for step in patch.get("steps", []) if identity(step) not in plugins and step["plugin"] != "Gain"]


def label(record):
    return f"{record['move']} on {record['source']}"


def failing(c, part):
    state = c["needed"] if part == "needed" else c["worked"]
    return sorted(leaf["id"] for leaf in c["leaves"] if leaf["id"].startswith(part) and leaf["state"] == state)


def decide(recipe, numbers, variants, master_only=False, validate=None):
    """The recipe with the first defaults in signal order written in, what changed, what was left and why, and what
    waits for a later round.

    A round changes at most one record per source, the first in its chain's order, only on the lowest level (blends,
    sources, drum-bus, mix-bus, master) that has a change, and no source a changed source keys a Duck on: every record
    on a node reads its output, so two changes there at once can undo each other. Records with an override are left as
    written. `validate(recipe)` lists a recipe's errors; a change that adds one is left out, and the next one in order
    goes instead, so one record that can't take its default never stops the rest.
    """
    moves = recipe.get("moves", [])
    index = {(r.get("move"), r.get("source")): i for i, r in enumerate(moves)}
    keep, drop, left = [], [], []

    def leave(record, why):
        left.append((record, why))

    for c in numbers["contracts"]:
        record = moves[index[(c["move"], c["source"])]]
        decision, computed = record.get("decision"), c["outcome"]
        if "override" in record:
            if c["fails"]:
                leave(record, override_report(record, c))
            continue
        if master_only and not c["move"].startswith("master."):
            if c["fails"]:
                leave(record, "with --master only master moves take their defaults")
            continue
        if decision in KEPT and computed == "dropped":
            if c["needed"] in NO_FAULT:
                drop.append((record, "no fault found"))
            elif c["move"] in contracts.DEFAULT_TRIALS:
                drop.append((record, f"its Worked if fails ({failing(c, 'worked')}); the check's own trial judges it from now on"))
            else:
                leave(record, f"its Worked if fails ({failing(c, 'worked')}); a dropped move with a fault keeps those "
                              "steps as its trial, and a failed trial needs an override naming those leaves")
        elif decision == "dropped" and computed == "kept":
            if c["with"] == "current":
                leave(record, "its default is kept with the options already in the recipe; keep it by hand, claiming the "
                              "fields that set them")
            else:
                keep.append((record, c))
        elif computed not in ("kept", "dropped"):
            leave(record, f"default {computed}")
        elif c["fails"]:
            leave(record, "; ".join(c["fails"]))
    known = set(validate(recipe)) if validate else set()
    patches = {}
    for record, c in keep:
        variant = variants.recipes.get(c["with"])
        patch = trial_patch(recipe, variant) if variant is not None else None
        if not patch:
            leave(record, "its trial can't be written as recipe steps, buses, sends and routing")
        else:
            patches[id(record)] = (record, patch, variant)

    def assemble(units):
        """The recipe with these units applied: keeps first, each where its trial was measured, then drops, each group
        as one off variant (pruning a return between its records would strand the next one's claims). None, with why,
        when a keep can't be written as measured."""
        out = copy.deepcopy(recipe)
        for kind, records in units:
            if kind != "keep":
                continue
            record, patch, _ = patches[id(records[0])]
            taken = {step.get("id") for step in out.get("steps", [])}
            names = set(out["stems"]) | set(out.get("buses", {}))
            clash = sorted(({s["id"] for s in patch.get("steps", [])} & taken) | (set(patch.get("buses", {})) & names))
            if clash:
                return None, ("left", f"its trial reuses names another kept trial added: {clash}")
            out = apply_patch(out, patch)
            steps, fields = claims_of(recipe, patch)
            entry = out["moves"][index[(record["move"], record["source"])]]
            entry.pop("trial", None)
            entry["decision"] = "kept"
            entry["steps"] = steps
            if fields or "fields" in entry:
                entry["fields"] = fields
            if off_tool(entry, patch) and not entry.get("tool_reason"):
                entry["tool_reason"] = TOOL_REASON
        for kind, records in units:
            if kind != "drop":
                continue
            out = drop_claims(out, records)
            for record in records:
                entry = out["moves"][index[(record["move"], record["source"])]]
                entry["decision"] = "dropped"
                for key in ("steps", "fields"):
                    if key in entry:
                        entry[key] = []
        for kind, records in units:
            if kind == "keep" and not placed(out, patches[id(records[0])][2]):
                return None, ("deferred", "its trial would not sit where it was measured next to this round's other changes")
        return out, None

    reasons = {id(record): why for record, why in drop}
    units = [("keep", [patches[id(record)][0]]) for record, _ in keep if id(record) in patches]
    units += [("drop", [record for record, _ in group]) for group in drop_groups(drop, copy.deepcopy(recipe), leave)]
    rank = Order(recipe)
    units.sort(key=lambda unit: min(rank.of(record) for record in unit[1]))
    chosen, deferred, level, busy, busy_nodes = [], [], None, set(), set()
    out = copy.deepcopy(recipe)
    for unit in units:
        records = unit[1]
        sources = {record["source"] for record in records}
        unit_level = min(rank.level(record) for record in records)
        acted = nodes_of(recipe, unit[0], records, patches)
        if (level is not None and unit_level > level) or sources & busy or rank.upstream(sources) & busy \
                or acted & busy_nodes:
            deferred += [(record, "waits for " + ("a lower level's changes" if level is not None and unit_level > level
                                                   else "a change before it in signal order")) for record in records]
            continue
        candidate, refused = assemble(chosen + [unit])
        if candidate is not None and validate and (new := sorted(set(validate(candidate)) - known)):
            refused = ("left", f"not applied: with it the recipe fails: {new}")
        if refused:
            kind, why = refused
            (left if kind == "left" else deferred).extend((record, why) for record in records)
            continue
        chosen.append(unit)
        out = candidate
        level = unit_level
        busy |= sources
        busy_nodes |= acted
    changes = []
    for kind, records in chosen:
        for record in records:
            if kind == "keep":
                entry = out["moves"][index[(record["move"], record["source"])]]
                changes.append((record, f"kept: its trial worked; added steps {entry['steps']}"
                                + (f" and {entry['fields']}" if entry.get("fields") else "") + "; its note is as written"))
            else:
                what = [*record.get("steps", []), *record.get("fields", [])]
                changes.append((record, f"dropped: {reasons[id(record)]}" + (f"; removed {what}" if what else "")
                                + "; its note is as written"))
    order = {id(r): i for i, r in enumerate(moves)}
    return (out, sorted(changes, key=lambda c: order[id(c[0])]), sorted(left, key=lambda c: order[id(c[0])]),
            sorted(deferred, key=lambda c: order[id(c[0])]))


def field_names(recipe, fields):
    """The stems and buses fields name: stem names hold dots ("Snare.wav"), so a field names the one it starts with."""
    known = list(recipe["stems"]) + list(recipe.get("buses", {}))
    return {name for field in fields for name in known if field.split(".", 1)[1].startswith(f"{name}.")}


def nodes_of(recipe, kind, records, patches):
    """The stems and buses a change acts on, where its records' readings go through: its steps' targets (a shared bus
    like the tom bus among them), the stems and buses its fields name, and each record's own node."""
    steps = {step["id"]: step for step in recipe.get("steps", [])}
    routing = Routing(recipe)
    found = set()
    for record in records:
        if kind == "keep":
            patch = patches[id(record)][1]
            found |= {step["target"] for step in patch.get("steps", [])}
            found |= field_names(recipe, claims_of(recipe, patch)[1]) | set(patch.get("buses", {}))
        found |= {steps[i]["target"] for i in record.get("steps", []) if i in steps}
        found |= field_names(recipe, record.get("fields", []))
        stems = [stem for stem in recipe.get("sources", {}).get(record["source"], {}).get("stems", []) if stem in recipe["stems"]]
        if stems:
            found.add(structure.node_of(routing, stems))
    return found - {None, "mix"}


def placed(recipe, variant):
    """Whether every step the trial variant has sits in the recipe in the variant's order on its target, against the
    steps both share: a removed anchor makes apply_patch append, which can move a trial past another processor."""
    ids = {step.get("id") for step in recipe.get("steps", [])}
    shared = ids & {step.get("id") for step in variant.get("steps", [])}
    targets = {step["target"] for step in variant.get("steps", []) if step.get("id") in shared}
    on = lambda r, target: [s["id"] for s in r.get("steps", []) if s["target"] == target and s.get("id") in shared]
    return all(on(recipe, target) == on(variant, target) for target in targets)

class Order:
    """Signal order for applying defaults: level, then sources by Duck keys (README's order breaking ties and cycles),
    then each chain's heading order."""

    def __init__(self, recipe):
        docs = chains.parse()[0]
        self.chain_rank = {move: i for chain in docs.values() for i, move in enumerate(chain)}
        sources = recipe.get("sources", {})
        routing = Routing(recipe)
        owner = {}
        for name in list(recipe["stems"]) + list(recipe.get("buses", {})):
            reach = {name} if name in recipe["stems"] else routing.stems_into(name)
            found = [source for source, settings in sources.items() if reach and reach <= set(settings.get("stems", []))]
            if len(found) == 1:
                owner[name] = found[0]
        self.edges = {}
        for step in recipe.get("steps", []):
            if step.get("plugin") == "Duck":
                key, target = owner.get(step["settings"].get("key")), owner.get(step["target"])
                if key and target and key != target:
                    self.edges.setdefault(key, set()).add(target)
        listed = list(sources)

        def tie(source):
            chain = sources[source].get("chain")
            return (SOURCE_ORDER.index(chain) if chain in SOURCE_ORDER else len(SOURCE_ORDER), listed.index(source))

        # Strongly connected parts of the Duck graph: a cycle's sources keep README's order among themselves only, and
        # the parts, a lone source being one, go in dependency order.
        reach = {source: self.reachable(source) for source in sources}
        part = {source: frozenset(s for s in sources if s == source or (s in reach[source] and source in reach[s]))
                for source in sources}
        parts = sorted(set(part.values()), key=lambda p: min(map(tie, p)))
        before = {p: {part[key] for key in sources for target in self.edges.get(key, ()) if part[target] == p and part[key] != p}
                  for p in parts}
        ordered, done = [], set()
        while len(done) < len(parts):
            p = next(p for p in parts if p not in done and before[p] <= done)
            done.add(p)
            ordered += sorted(p, key=tie)
        self.source_rank = {source: i for i, source in enumerate(ordered)}

    def level(self, record):
        if contracts.CONTRACTS.get(record["move"], {}).get("compare") == "options":
            return 0
        return LEVELS.get(record["source"], 1)

    def of(self, record):
        return (self.level(record), self.source_rank.get(record["source"], len(self.source_rank)),
                self.chain_rank.get(record["move"], 0))

    def reachable(self, source):
        found, todo = set(), [source]
        while todo:
            for target in self.edges.get(todo.pop(), ()):
                if target not in found:
                    found.add(target)
                    todo.append(target)
        return found

    def upstream(self, sources):
        """Every source whose output a Duck in these sources is keyed on, directly or through other Ducks."""
        found, todo = set(), list(sources)
        while todo:
            current = todo.pop()
            for key, targets in self.edges.items():
                if current in targets and key not in found:
                    found.add(key)
                    todo.append(key)
        return found


def claims(record):
    return set(record.get("steps", []) + record.get("fields", []))


def drop_groups(drop, recipe, leave):
    """The drops that can go, as groups sharing a claim, which go together or not at all.

    A drop that shares a claim with a record that stays is left, and so, in turn, is any drop sharing a claim with it,
    so no record that stays ever claims a removed step.
    """
    dropping = {(r["move"], r["source"]): (r, why) for r, why in drop}
    staying = [r for r in recipe.get("moves", []) if (r.get("move"), r.get("source")) not in dropping]
    changed = True
    while changed:
        changed = False
        for key, (record, _) in list(dropping.items()):
            shared = [r for r in staying if claims(record) & claims(r)]
            if shared:
                leave(record, f"it shares {sorted(claims(record) & set().union(*map(claims, shared)))} with "
                              f"{[label(r) for r in shared]}, which stay")
                staying.append(dropping.pop(key)[0])
                changed = True
    groups, seen = [], set()
    for key in dropping:
        if key in seen:
            continue
        group, todo = [], [key]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            group.append(dropping[current])
            todo += [k for k, (r, _) in dropping.items() if k not in seen and claims(r) & claims(dropping[current][0])]
        groups.append(group)
    return groups


def fed(recipe):
    entries = list(recipe["stems"].values()) + list(recipe.get("buses", {}).values())
    return {settings.get("bus", "mix") for settings in entries} | {send["bus"] for settings in entries for send in settings.get("sends", [])}


def drop_claims(recipe, records):
    """The records' off variant, as the check measured it, without the empty sends lists and unfed returns that leaves.

    A return the drop leaves unfed renders silence, and the renderer refuses an unfed bus in a song's own recipe, so it
    goes when no step acts on it.
    """
    before = fed(recipe)
    union = {key: list(dict.fromkeys(c for record in records for c in record.get(key, []))) for key in ("steps", "fields")}
    out = evidence.off_variant(recipe, union)
    for field in union["fields"]:
        kind, rest = field.split(".", 1)
        if ".sends." in rest:
            name = rest.split(".sends.")[0]
            entries = out["stems"] if kind == "stems" else out["buses"]
            if entries[name].get("sends") == []:
                del entries[name]["sends"]
    targets = {step["target"] for step in out.get("steps", [])}
    while unfed := [bus for bus in out.get("buses", {}) if bus in before and bus not in fed(out) and bus not in targets]:
        for bus in unfed:
            del out["buses"][bus]
    return out


def override_report(record, c):
    """A hand override the check fails: what it cites, what the check now asks it to cite, and those leaves' values."""
    cited = record["override"].get("leaves", [])
    named = set(cited) | {name for fail in c["fails"] for name in re.findall(r"'([a-z]+(?:\.\d+)*)'", fail)}
    values = [f"{leaf['id']} {leaf.get('value')} ({leaf.get('test', leaf['state'])})" for leaf in c["leaves"] if leaf["id"] in named]
    return f"its override is as written, citing {cited}; {'; '.join(c['fails'])}; values: {', '.join(values)}"


def default_of(c, needed, worked):
    return evidence.outcome(None, needed, worked, c.get("with") is not None)


def deciding_part(c):
    return default_of(c, c["needed"], c.get("worked"))[1]


def deciding(c):
    part = deciding_part(c)
    return [leaf for leaf in c["leaves"] if leaf["id"].split(".")[0] == part]


def clearance(leaf):
    """How far a leaf's value sits from its bar, in the leaf's own units, positive on its passing side; None when the
    leaf has no graded bar."""
    if leaf.get("measure") in NOT_GRADED or not isinstance(leaf.get("value"), (int, float)) or "test" not in leaf:
        return None
    op, bar = leaf["test"].split(" ", 1)
    if op not in ("<", "<=", ">", ">="):
        return None
    bar = float(bar)
    return round(leaf["value"] - bar if op in (">", ">=") else bar - leaf["value"], 3)


def fragile(c):
    """The leaves within FRAGILE of their bar whose flip alone would flip the record's default, in Needed if or Worked
    if."""
    contract = contracts.CONTRACTS[c["move"]]
    states = {leaf["id"]: leaf["state"] for leaf in c["leaves"]}

    def default(states):
        worked = outcome_tools.restate(contract["worked"], states, "worked") if c.get("worked") is not None else None
        return default_of(c, outcome_tools.restate(contract["needed"], states, "needed"), worked)[0]

    now = default(states)
    found = []
    for leaf in c["leaves"]:
        gap = clearance(leaf)
        if gap is None or abs(gap) >= FRAGILE or leaf["state"] not in (outcome_tools.TRUE, outcome_tools.FALSE):
            continue
        flipped = outcome_tools.FALSE if leaf["state"] == outcome_tools.TRUE else outcome_tools.TRUE
        if default(states | {leaf["id"]: flipped}) != now:
            found.append((leaf, gap))
    return found


def readings(c):
    """The deciding leaves as one line: id, value, test and clearance."""
    return ", ".join(f"{leaf['id']} {leaf.get('value')} {leaf.get('test', '')}".rstrip()
                     + (f" (clears {clearance(leaf)})" if clearance(leaf) is not None else "") for leaf in deciding(c))


def reads(recipe, record):
    """The stems, buses and signals a record's leaves read, for marking it provisional: 'mix' stands for the mix, the
    trial master and the rest of the band, which every change reaches."""
    contract = contracts.CONTRACTS.get(record["move"])
    if contract is None:
        return set()
    sources = recipe.get("sources", {})
    routing = Routing(recipe)

    def own(names):
        stems = {stem for source in names for stem in sources.get(source, {}).get("stems", [])}
        return stems | {bus for bus in recipe.get("buses", {}) if routing.stems_into(bus) and routing.stems_into(bus) <= stems}

    source = record["source"]
    if source in ("mix-bus", "master"):
        return {"mix"}
    mine = own([s for s, v in sources.items() if v.get("chain") in {"kick", "snare", "toms", "overheads"}]) \
        if source == "drum-bus" else own([source])
    found = set()
    for tree in (contract["needed"], contract["worked"]):
        for leaf in evidence.walk(tree):
            if leaf["kind"] == "judged":
                continue
            if leaf["kind"] == "fact":
                if leaf["name"] in ("no_space", "kit_reverb_unfed", "no_kit_reverb"):
                    found.add("mix")
                elif leaf["name"] == "move_active":
                    found.add(f"move:{leaf['args']['move']}")
                continue
            args = leaf.get("args", {})
            if leaf["measure"] in GLOBAL_MEASURES:
                found.add("mix")
            for point in (args.get("point", "node"), args.get("other")):
                if point is None:
                    continue
                if point in ("mix", "trial", "premaster", "master-in", "rest", "join"):
                    found.add("mix")
                elif point.startswith("chain:"):
                    chains_read = set(point[6:].split("+"))
                    found |= own([s for s, v in sources.items() if v.get("chain") in chains_read])
                else:
                    found |= mine
    return found


def touched(before, after, changed):
    """Every stem, bus and signal the changed records' claims reach, through buses, sends and Duck keys."""
    names = set()
    for record in changed:
        new = next((r for r in after.get("moves", []) if (r.get("move"), r.get("source")) == (record["move"], record["source"])), {})
        names.add(f"move:{record['move']}")
        for recipe, claimed in ((before, record), (after, new)):
            steps = {step["id"]: step for step in recipe.get("steps", [])}
            names |= {steps[i]["target"] for i in claimed.get("steps", []) if i in steps}
            names |= field_names(recipe, claimed.get("fields", []))
    if names - {n for n in names if n.startswith("move:")}:
        names.add("mix")
    for recipe in (before, after):
        entries = dict(recipe["stems"]) | recipe.get("buses", {})
        keyed = {}
        for step in recipe.get("steps", []):
            if step.get("plugin") == "Duck":
                keyed.setdefault(step["settings"].get("key"), set()).add(step["target"])
        todo = list(names)
        while todo:
            name = todo.pop()
            settings = entries.get(name, {})
            for following in {settings.get("bus", "mix")} | {s["bus"] for s in settings.get("sends", [])} | keyed.get(name, set()):
                if following not in names:
                    names.add(following)
                    todo.append(following)
    return names


def provisional(before, after, changed):
    """Records whose reads include something these changes touched: their defaults are as of before the changes."""
    names = touched(before, after, changed)
    skip = {(r["move"], r["source"]) for r in changed}
    return [record for record in after.get("moves", []) if record.get("move") != "balance"
            and (record.get("move"), record.get("source")) not in skip and reads(after, record) & names]
