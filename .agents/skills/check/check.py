import gc
import json
import sys
from pathlib import Path

import chains
import contracts
import defaults
import review
import structure
from evidence import inputs, measured, progress, release, render, report

USAGE = "usage: uv run .agents/skills/check/check.py [--master | --review-prompt] [--no-report | --apply-defaults] songs/<id>/recipe.json"
FLAGS = ("--master", "--no-report", "--review-prompt", "--apply-defaults")
# Each round can only change records no earlier round changed, so this bounds a chain of knock-on defaults.
MAX_ROUNDS = 3


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


def apply_defaults(path, recipe, song, master):
    """Writes each record's default outcome into the recipe, re-checking after each round until none changes."""
    write = defaults.formatted(recipe, path.read_text())
    if write is None:
        sys.exit(f"{path} isn't in the form the repo writes (json.dumps with indent=2 and a final newline); write it that way "
                 "first, so --apply-defaults changes only the records it decides")
    frozen = {}
    for round_ in range(1, MAX_ROUNDS + 2):
        nodes, errors, notes = structural(recipe, song, master)
        if errors or (master and (errors := mix_review(recipe, song))):
            stop(errors)
        report_path, numbers, variants = measured(recipe, song, nodes, master)
        # A reviewed mix stays as reviewed: with --master only the master moves take their defaults.
        new, changes, left = defaults.decide(recipe, numbers, variants, frozen, master_only=master,
                                             validate=lambda candidate: renders(candidate, song, master))
        # The next round renders the whole song again, so this one's renders must be gone before it starts.
        release(variants)
        del variants
        gc.collect()
        if not changes or round_ > MAX_ROUNDS:
            for record, why in left:
                print(f"left: {defaults.label(record)}: {why}")
            if changes:
                print(f"apply-defaults: stopped after {MAX_ROUNDS} rounds with {len(changes)} more defaults to apply; run it again")
            return nodes, notes, report_path, numbers
        path.write_text(write(new))
        for record, what in changes:
            print(f"applied, round {round_}: {defaults.label(record)}: {what}")
        print(f"apply-defaults: wrote {path}; checking it again")
        frozen |= {(record["move"], record["source"]): round_ for record, _ in changes}
        recipe = new


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
