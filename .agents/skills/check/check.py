import json
import sys
from pathlib import Path

import chains
import structure
from evidence import render, report

USAGE = "usage: uv run .agents/skills/check/check.py [--master] [--no-report] songs/<id>/recipe.json"


def main():
    args = sys.argv[1:]
    master, skip_report = "--master" in args, "--no-report" in args
    args = [arg for arg in args if arg not in ("--master", "--no-report")]
    if len(args) != 1:
        sys.exit(USAGE)
    path = Path(args[0])
    recipe, song = json.loads(path.read_text()), path.parent
    errors, notes = list(chains.parse()[1]), []
    # Routing walks parent buses, so a cycle must stop the check before it does.
    render.check_cycles(recipe)
    routing = structure.Routing(recipe)
    structure.inventory(recipe, song, errors, notes)
    nodes = structure.nodes(recipe, routing, errors)
    structure.coverage(recipe, routing, master, errors, notes)
    for note in notes:
        print(f"review: {note}")
    if errors:
        print("\n".join(f"fail: {error}" for error in errors))
        sys.exit(1)
    if not skip_report:
        print(report(recipe, song, nodes))


if __name__ == "__main__":
    main()
