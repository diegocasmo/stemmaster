import hashlib
import json
import sys
from pathlib import Path

import chains
import contracts
import review
import structure
from evidence import CODE, ROOT, inputs, progress, render, report

USAGE = "usage: uv run .agents/skills/check/check.py [--master | --review-prompt] [--no-report] songs/<id>/recipe.json"
FLAGS = ("--master", "--no-report", "--review-prompt")


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


def code_files():
    """Each file the fingerprint's code and docs hash covers, so a scoped review can name the ones that changed."""
    docs = sorted((ROOT / "docs" / "mixing-chains").glob("*.md"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in CODE + docs}


def mix_review(recipe, song):
    entries = [r for r in recipe.get("reviews", []) if r.get("stage") == "mix"]
    if not entries or "trial_master" not in entries[-1]:
        return ["--master needs a mix review entry with its fingerprint and trial_master (check --review-prompt prints it)"]
    entry = entries[-1]
    mix = review.mix_part(recipe, entry["trial_master"])
    nodes, errors, notes = structural(mix, song, False)
    if errors:
        return [f"the reviewed mix no longer checks: {error}" for error in errors]
    outside = inputs(song, mix)
    if review.fingerprint(mix, outside) != entry.get("fingerprint"):
        return ["the mix changed after its review: review it again (check --review-prompt)"]
    path, numbers = report(mix, song, nodes)
    if any(c["fails"] for c in numbers["contracts"]):
        return ["the reviewed mix fails its own check"]
    return review.master_errors(recipe, review.snapshot(mix, outside, code_files(), path, numbers, notes))


def main():
    args = sys.argv[1:]
    master, skip_report, asking = (flag in args for flag in FLAGS)
    args = [arg for arg in args if arg not in FLAGS]
    if len(args) != 1 or (master and asking):
        sys.exit(USAGE)
    path = Path(args[0])
    recipe, song = json.loads(path.read_text()), path.parent
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
    fails = [f"{c['move']} on {c['source']}: {fail}" for c in numbers["contracts"] for fail in c["fails"]]
    for key, text in sorted(review.questions(numbers).items()):
        print(f"review: {key}: {text}")
    print("\n".join(f"fail: {fail}" for fail in fails))
    print(path)
    if fails:
        sys.exit(1)
    if asking:
        now = review.snapshot(recipe, inputs(song, recipe), code_files(), path, numbers, notes)
        text, asked, plan = review.plan_review(now)
        prompt, digest = review.write_prompt(text, path.parent)
        entry = {"stage": "mix", "fingerprint": now["fingerprint"], "trial_master": review.trial_master(recipe)}
        scope = f"scoped round {plan['round']} on review {plan['base']}" if plan["base"] else "full"
        print(f"{scope}: {len(asked)} ids to answer, {len(plan['findings'])} open findings")
        entry["by"] = [{"agent": agent, "session": "<its session uuid>"} for agent in review.AGENTS]
        print(f"review prompt: {prompt} (sha256 {digest})")
        print("send it, unchanged, as the first message of one fresh Claude and one fresh Codex session, then add to reviews:")
        print(json.dumps(entry))


if __name__ == "__main__":
    try:
        main()
    finally:
        progress("done")
