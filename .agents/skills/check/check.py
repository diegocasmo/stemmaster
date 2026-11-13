import gc
import json
import sys
from pathlib import Path

import chains
import contracts
import defaults
import review
import structure
from evidence import fingerprint, inputs, measured, numeric, progress, record_part, release, render, report

USAGE = "usage: uv run .agents/skills/check/check.py [--master | --review-prompt] [--no-report | --apply-defaults] songs/<id>/recipe.json"
FLAGS = ("--master", "--no-report", "--review-prompt", "--apply-defaults")
# Checks in one --apply-defaults run, the last only measuring: each round changes at most one record per source.
MAX_ROUNDS = 6


def structural(recipe, song, master):
    docs, errors = chains.parse()
    errors, notes = errors + contracts.lint(docs, chains.tools), []
    # Routing walks parent buses, so a cycle must stop the check before it does.
    render.check_cycles(recipe)
    routing = structure.Routing(recipe)
    structure.inventory(recipe, song, errors, notes)
    nodes = structure.nodes(recipe, routing, errors)
    structure.coverage(recipe, routing, master, errors, notes, song)
    return nodes, errors, notes


def mix_review(recipe, song):
    entries = [r for r in recipe.get("reviews", []) if r.get("stage") == "mix"]
    if not entries or "trial_master" not in entries[-1]:
        return ["--master needs a mix review entry with its fingerprint and trial_master (check --review-prompt prints it)"]
    entry = entries[-1]
    mix = review.mix_part(recipe, entry["trial_master"])
    nodes, errors, notes = structural(mix, song, False)
    if errors:
        return [f"the reviewed mix no longer checks: {error}" for error in errors]
    fp = review.fingerprint(mix, inputs(song, mix))
    if fp != entry.get("fingerprint"):
        return ["the mix changed after its review: review it again (check --review-prompt)"]
    path, numbers = report(mix, song, nodes)
    if any(c["fails"] for c in numbers["contracts"]):
        return ["the reviewed mix fails its own check"]
    text = review.prompt(numbers, path, fp, notes)
    return review.verify(recipe, fp, review.report_digest(numbers), review.sha(text), review.questions(numbers))


def stop(errors, prefix=""):
    print("\n".join(f"fail: {prefix}{error}" for error in errors))
    sys.exit(1)


def renders(recipe, song, master):
    """A recipe's structural errors, and the renderer's own refusal of it, as the check's strict render would refuse."""
    try:
        errors = structural(recipe, song, master)[1]
        render.check(recipe, *render.timeline(recipe, song))
    except SystemExit as error:
        return [str(error)]
    return errors


def state(recipe):
    """What decides every reading: the rendered recipe and every record as its contract reads it."""
    return fingerprint(numeric(recipe), [record_part(r) for r in recipe.get("moves", [])])


def apply_defaults(path, recipe, song, master):
    """Writes the records' default outcomes into the recipe in signal order, re-checking after each round, until none
    changes, a state repeats, or MAX_ROUNDS checks have run. Prints what it left and the history of what still flips."""
    write = defaults.formatted(recipe, path.read_text())
    if write is None:
        sys.exit(f"{path} isn't in the form the repo writes (json.dumps with indent=2 and a final newline); write it that way "
                 "first, so --apply-defaults changes only the records it decides")
    seen = {state(recipe): 0}
    history, changed_in = {}, {}
    for round_ in range(1, MAX_ROUNDS + 1):
        nodes, errors, notes = structural(recipe, song, master)
        if errors or (master and (errors := mix_review(recipe, song))):
            stop(errors)
        report_path, numbers, variants = measured(recipe, song, nodes, master)
        for c in numbers["contracts"]:
            key = (c["move"], c["source"])
            if c["fails"] or key in history:
                history.setdefault(key, []).append((round_, c["decision"], c["outcome"], defaults.readings(c)))
        # A reviewed mix stays as reviewed: with --master only the master moves take their defaults.
        new, changes, left, deferred = defaults.decide(recipe, numbers, variants, master_only=master,
                                                       validate=lambda candidate: renders(candidate, song, master))
        # The next round renders the whole song again, so this one's renders must be gone before it starts.
        release(variants)
        del variants
        gc.collect()
        repeats = seen.get(state(new)) if changes else None
        if not changes or round_ == MAX_ROUNDS or repeats is not None:
            report_end(numbers, left, deferred, history, changed_in)
            if changes:
                why = f"its changes would bring back the recipe checked in round {repeats + 1}" if repeats is not None \
                    else f"{MAX_ROUNDS} checks ran"
                print(f"apply-defaults: stopped with {len(changes)} defaults not applied: {why}")
                for record, what in changes:
                    print(f"not applied: {defaults.label(record)}: {what}")
                for record in defaults.provisional(recipe, new, [record for record, _ in changes]):
                    print(f"provisional: {defaults.label(record)}: reads what those changes touch")
            return nodes, notes, report_path, numbers
        path.write_text(write(new))
        for record, what in changes:
            print(f"applied, round {round_}: {defaults.label(record)}: {what}")
            changed_in.setdefault((record["move"], record["source"]), []).append(round_)
            history.setdefault((record["move"], record["source"]), [])
        print(f"apply-defaults: wrote {path}; checking it again")
        seen[state(new)] = round_
        recipe = new


def report_end(numbers, left, deferred, history, changed_in):
    for record, why in left:
        print(f"left: {defaults.label(record)}: {why}")
    for record, why in deferred:
        print(f"deferred: {defaults.label(record)}: {why}")
    contracts_now = {(c["move"], c["source"]): c for c in numbers["contracts"]}
    for key, c in contracts_now.items():
        for leaf, gap in defaults.fragile(c):
            print(f"fragile: {c['move']} on {c['source']}: {leaf['id']} {leaf['text']}: {leaf['value']} {leaf['test']} "
                  f"(clears {gap}); a small change elsewhere can flip its default")
    contradicting = [key for key, c in contracts_now.items()
                     if any(fail.startswith(("the default outcome is", "the trial worked")) for fail in c["fails"])]
    for key in contradicting:
        # Only a default that moved between rounds flips; a steady one is deferred, left or not applied, and says so.
        if len({outcome for _, _, outcome, _ in history.get(key, [])}) < 2:
            continue
        rounds = "; ".join(f"round {r}: {decision}, default {outcome} ({values})" for r, decision, outcome, values in history[key])
        others = sorted({f"{move} on {source} (round {r})" for (move, source), rs in changed_in.items() if (move, source) != key
                         for r in rs})
        print(f"flips: {key[0]} on {key[1]}: {rounds}; changed meanwhile: {others or 'nothing'}. Decide it by hand: keep the "
              "decision whose readings you can defend and give an override with them")


def main():
    args = sys.argv[1:]
    master, skip_report, asking, applying = (flag in args for flag in FLAGS)
    args = [arg for arg in args if arg not in FLAGS]
    if len(args) != 1 or (master and asking) or (applying and (asking or skip_report)):
        sys.exit(USAGE)
    path = Path(args[0])
    recipe, song = json.loads(path.read_text()), path.parent
    if applying:
        nodes, notes, path, numbers = apply_defaults(path, recipe, song, master)
        for note in notes:
            print(f"review: {note}")
        finish(path, numbers)
        return
    nodes, errors, notes = structural(recipe, song, master)
    for note in notes:
        print(f"review: {note}")
    if errors:
        print("\n".join(f"fail: {error}" for error in errors))
        sys.exit(1)
    if skip_report:
        return
    if master and (errors := mix_review(recipe, song)):
        print("\n".join(f"fail: {error}" for error in errors))
        sys.exit(1)
    if asking:
        # The prompt is built from the mix in the same form --master rebuilds it, so the two agree byte for byte.
        recipe = review.mix_part(recipe, review.trial_master(recipe))
        nodes, errors, notes = structural(recipe, song, False)
        if errors:
            print("\n".join(f"fail: the mix without its master moves: {error}" for error in errors))
            sys.exit(1)
    path, numbers = report(recipe, song, nodes, master)
    finish(path, numbers)
    if asking:
        fp = review.fingerprint(recipe, inputs(song, recipe))
        prompt, digest = review.write_prompt(numbers, path, fp, notes, path.parent)
        entry = {"stage": "mix", "fingerprint": fp, "trial_master": review.trial_master(recipe),
                 "by": [{"agent": agent, "session": "<its session uuid>"} for agent in review.AGENTS]}
        print(f"review prompt: {prompt} (sha256 {digest})")
        print("send it, unchanged, as the first message of one fresh Claude and one fresh Codex session, then add to reviews:")
        print(json.dumps(entry))


def finish(path, numbers):
    fails = [f"{c['move']} on {c['source']}: {fail}" for c in numbers["contracts"] for fail in c["fails"]]
    for key, text in sorted(review.questions(numbers).items()):
        print(f"review: {key}: {text}")
    print("\n".join(f"fail: {fail}" for fail in fails))
    print(path)
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        progress("done")
