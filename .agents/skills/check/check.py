import json
import re
import sys
from pathlib import Path

import chains
import contracts
import evidence
import review
import runlog
import structure
import workers
from evidence import inputs, progress, render, report

USAGE = "usage: uv run .agents/skills/check/check.py [--master | --review-prompt] [--no-report] songs/<id>/recipe.json"
FLAGS = ("--master", "--no-report", "--review-prompt")
RUN = {}


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
    RUN["contracts"] = len(numbers["contracts"])
    fails = [f"{c['move']} on {c['source']}: {fail}" for c in numbers["contracts"] for fail in c["fails"]]
    for key, text in sorted(review.questions(numbers).items()):
        print(f"review: {key}: {text}")
    print("\n".join(f"fail: {fail}" for fail in fails))
    print(path)
    if fails:
        sys.exit(1)
    if asking:
        fp = review.fingerprint(recipe, inputs(song, recipe))
        prompt, digest = review.write_prompt(numbers, path, fp, notes, path.parent)
        entry = {"stage": "mix", "fingerprint": fp, "trial_master": review.trial_master(recipe),
                 "by": [{"agent": agent, "session": "<its session uuid>"} for agent in review.AGENTS]}
        print(f"review prompt: {prompt} (sha256 {digest})")
        print("send it, unchanged, as the first message of one fresh Claude and one fresh Codex session, then add to reviews:")
        print(json.dumps(entry))


def cache_files(recipe_path):
    """Contract and battery files in the check's cache: the ones a run adds are the parts it computed cold."""
    cache = Path(recipe_path).parent / "renders" / "check"
    names = {p.name for p in cache.glob("*.json")} if cache.is_dir() else set()
    return {name for name in names if name.startswith("contract-") or re.fullmatch(r"[0-9a-f]{64}\.json", name)}


def details(recipe_path, flags, before):
    added = cache_files(recipe_path) - before
    try:
        moves = len(json.loads(Path(recipe_path).read_text()).get("moves", []))
    except (OSError, ValueError, AttributeError):
        moves = None
    return {"scope": {"flags": flags, "moves": moves},
            "counts": {"contracts": RUN.get("contracts"), "contracts_cold": sum(n.startswith("contract-") for n in added),
                       "batteries_cold": sum(not n.startswith("contract-") for n in added),
                       "variant_renders": evidence.RENDERS[0] + workers.RENDERS[0]}}


if __name__ == "__main__":
    recipe_path = next((arg for arg in sys.argv[1:] if not arg.startswith("--")), None)
    flags = [arg for arg in sys.argv[1:] if arg.startswith("--")]
    before = cache_files(recipe_path) if recipe_path else set()
    try:
        with runlog.logged("check", recipe_path, lambda: details(recipe_path, flags, before)):
            main()
    finally:
        progress("done")
