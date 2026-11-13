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


def decide(recipe, numbers, variants, frozen=(), master_only=False):
    """The recipe with each record's default outcome written in, what changed, and what was left and why.

    Records with an override, and those in `frozen` (applied in an earlier round), are left as written.
    """
    moves = recipe.get("moves", [])
    index = {(r.get("move"), r.get("source")): i for i, r in enumerate(moves)}
    keep, drop, left = [], [], []
    for c in numbers["contracts"]:
        record = moves[index[(c["move"], c["source"])]]
        decision, computed = record.get("decision"), c["outcome"]
        if "override" in record or (master_only and not c["move"].startswith("master.")):
            continue
        contradicts = (decision in KEPT and computed == "dropped") or (decision == "dropped" and computed == "kept")
        if contradicts and (c["move"], c["source"]) in frozen:
            left.append((record, f"its default flipped to {computed} after it was applied as {decision}; decide it by hand, "
                                 "with an override giving the measured reason"))
        elif decision in KEPT and computed == "dropped":
            if c["needed"] in NO_FAULT:
                drop.append((record, "no fault found"))
            elif c["move"] in contracts.DEFAULT_TRIALS:
                drop.append((record, f"its Worked if fails ({failing(c, 'worked')}); the check's own trial judges it from now on"))
            else:
                left.append((record, f"its Worked if fails ({failing(c, 'worked')}); a dropped move with a fault keeps those "
                                     "steps as its trial, and a failed trial needs an override naming those leaves"))
        elif decision == "dropped" and computed == "kept":
            keep.append((record, c))
        elif computed not in ("kept", "dropped"):
            left.append((record, f"default {computed}"))
        elif c["fails"]:
            left.append((record, "; ".join(c["fails"])))
    out = copy.deepcopy(recipe)
    changes = []
    # Trials go in before any step is removed, so a step anchored on a removed one still lands where it was measured.
    for record, c in keep:
        variant = variants.recipes.get(c["with"])
        patch = trial_patch(recipe, variant) if variant is not None else None
        if patch is None:
            left.append((record, "its trial can't be written as recipe steps, buses, sends and routing"))
            continue
        taken = {step.get("id") for step in out.get("steps", [])}
        names = set(out["stems"]) | set(out.get("buses", {}))
        clash = sorted(({s["id"] for s in patch.get("steps", [])} & taken) | (set(patch.get("buses", {})) & names))
        if clash:
            left.append((record, f"its trial reuses names another kept trial added: {clash}"))
            continue
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
        changes.append((record, f"kept: its trial worked; added steps {steps}" + (f" and {fields}" if fields else "")))
    dropping = {(r["move"], r["source"]) for r, _ in drop}
    for record, why in drop:
        claims = set(record.get("steps", []) + record.get("fields", []))
        others = [r for r in out["moves"] if (r.get("move"), r.get("source")) not in dropping
                  and claims & set(r.get("steps", []) + r.get("fields", []))]
        if others:
            shared = sorted(claims & {c for r in others for c in r.get("steps", []) + r.get("fields", [])})
            left.append((record, f"it shares {shared} with {[label(r) for r in others]}, which stay"))
            continue
        out = drop_claims(out, record)
        entry = out["moves"][index[(record["move"], record["source"])]]
        entry["decision"] = "dropped"
        for key in ("steps", "fields"):
            if key in entry:
                entry[key] = []
        what = [*record.get("steps", []), *record.get("fields", [])]
        changes.append((record, f"dropped: {why}" + (f"; removed {what}" if what else "")))
    order = {id(r): i for i, r in enumerate(moves)}
    return out, sorted(changes, key=lambda c: order[id(c[0])]), sorted(left, key=lambda c: order[id(c[0])])


def drop_claims(recipe, record):
    """The recipe as the check measured it without the record (its off variant), minus the empty sends lists that leaves."""
    out = evidence.off_variant(recipe, record)
    for field in record.get("fields", []):
        kind, rest = field.split(".", 1)
        if ".sends." in rest:
            name = rest.split(".sends.")[0]
            entries = out["stems"] if kind == "stems" else out["buses"]
            if entries[name].get("sends") == []:
                del entries[name]["sends"]
    return out
