import copy
import json

import chains
import contracts
import evidence
import outcomes as outcome_tools
from structure import apply_patch, identity

KEPT = ("kept", "changed")
NO_FAULT = (outcome_tools.FALSE, outcome_tools.SKIPPED)
TOOL_REASON = "the check's starting trial (contracts.DEFAULT_TRIALS), kept as the check measured it"


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


def decide(recipe, numbers, variants, frozen=None, master_only=False, validate=None):
    """The recipe with each record's default outcome written in, what changed, and what was left and why.

    Records with an override are left as written, and so are those in `frozen` ({(move, source): round} applied in an
    earlier round). `validate(recipe)` lists a recipe's errors; a change that adds one is left out, so one record that
    can't take its default never stops the rest.
    """
    frozen = frozen or {}
    moves = recipe.get("moves", [])
    index = {(r.get("move"), r.get("source")): i for i, r in enumerate(moves)}
    keep, drop, left = [], [], []

    def leave(record, why):
        key = (record["move"], record["source"])
        left.append((record, why + (f" (applied in round {frozen[key]})" if key in frozen else "")))

    for c in numbers["contracts"]:
        record = moves[index[(c["move"], c["source"])]]
        decision, computed = record.get("decision"), c["outcome"]
        if "override" in record:
            continue
        if master_only and not c["move"].startswith("master."):
            if c["fails"]:
                leave(record, "with --master only master moves take their defaults")
            continue
        contradicts = (decision in KEPT and computed == "dropped") or (decision == "dropped" and computed == "kept")
        if contradicts and (c["move"], c["source"]) in frozen:
            leave(record, f"its default flipped to {computed} after it was applied as {decision}; decide it by hand, "
                          "with an override giving the measured reason")
        elif decision in KEPT and computed == "dropped":
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
    out = copy.deepcopy(recipe)
    known = set(validate(recipe)) if validate else set()
    changes = []

    def valid(candidate, records):
        new = sorted(set(validate(candidate)) - known) if validate else []
        for record in records:
            if new:
                leave(record, f"not applied: with it the recipe fails: {new}")
        return not new

    # Trials go in before any step is removed, so a step anchored on a removed one still lands where it was measured.
    for record, c in keep:
        variant = variants.recipes.get(c["with"])
        patch = trial_patch(recipe, variant) if variant is not None else None
        if not patch:
            leave(record, "its trial can't be written as recipe steps, buses, sends and routing")
            continue
        taken = {step.get("id") for step in out.get("steps", [])}
        names = set(out["stems"]) | set(out.get("buses", {}))
        clash = sorted(({s["id"] for s in patch.get("steps", [])} & taken) | (set(patch.get("buses", {})) & names))
        if clash:
            leave(record, f"its trial reuses names another kept trial added: {clash}")
            continue
        candidate = apply_patch(out, patch)
        steps, fields = claims_of(recipe, patch)
        entry = candidate["moves"][index[(record["move"], record["source"])]]
        entry.pop("trial", None)
        entry["decision"] = "kept"
        entry["steps"] = steps
        if fields or "fields" in entry:
            entry["fields"] = fields
        if off_tool(entry, patch) and not entry.get("tool_reason"):
            entry["tool_reason"] = TOOL_REASON
        if valid(candidate, [record]):
            out = candidate
            changes.append((record, f"kept: its trial worked; added steps {steps}" + (f" and {fields}" if fields else "")
                            + "; its note is as written"))
    for group in drop_groups(drop, out, leave):
        # One off variant for the whole group: pruning a return between its records would strand the next one's claims.
        candidate = drop_claims(out, [record for record, _ in group])
        for record, _ in group:
            entry = candidate["moves"][index[(record["move"], record["source"])]]
            entry["decision"] = "dropped"
            for key in ("steps", "fields"):
                if key in entry:
                    entry[key] = []
        if valid(candidate, [record for record, _ in group]):
            out = candidate
            for record, why in group:
                what = [*record.get("steps", []), *record.get("fields", [])]
                changes.append((record, f"dropped: {why}" + (f"; removed {what}" if what else "") + "; its note is as written"))
    order = {id(r): i for i, r in enumerate(moves)}
    return out, sorted(changes, key=lambda c: order[id(c[0])]), sorted(left, key=lambda c: order[id(c[0])])


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
