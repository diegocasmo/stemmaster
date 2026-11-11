import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHAINS = ROOT / "docs" / "mixing-chains"
BULLETS = ["Why", "Start", "Tool", "Needed if", "Worked if"]
HEADING = re.compile(r"^### ([a-z0-9-]+)\.([a-z0-9]+(?:-[a-z0-9]+)*)$")
FIELD_TOOLS = {"gain_db", "invert", "offset_samples", "pan"}


def parse():
    chains, errors, seen = {}, [], set()
    for path in sorted(CHAINS.glob("*.md")):
        if path.name == "README.md":
            continue
        chain, moves, current = path.stem, {}, None
        for number, line in enumerate(path.read_text().splitlines(), 1):
            where = f"{path.name}:{number}"
            if line.startswith("#"):
                if number == 1 and line.startswith("# "):
                    continue
                match = HEADING.match(line)
                if not match or match.group(1) != chain:
                    errors.append(f"{where}: headings must be '### {chain}.<kebab-id>', got {line!r}")
                    current = None
                    continue
                move = f"{chain}.{match.group(2)}"
                if move in seen:
                    errors.append(f"{where}: duplicate move {move}")
                seen.add(move)
                current = moves[move] = {}
            elif current is not None and line.startswith("- **"):
                bullet = re.match(r"- \*\*([^*]+):\*\* (.+)$", line)
                if not bullet:
                    errors.append(f"{where}: malformed bullet {line!r}")
                    continue
                current[bullet.group(1)] = bullet.group(2)
                if list(current) != BULLETS[: len(current)]:
                    errors.append(f"{where}: bullets must be {', '.join(BULLETS)} in that order")
        for move, bullets in moves.items():
            if list(bullets) != BULLETS:
                errors.append(f"{path.name}: {move} needs exactly the bullets {', '.join(BULLETS)}")
        chains[chain] = moves
    return chains, errors


def tools(move_bullets):
    text = move_bullets["Tool"]
    names = set(re.findall(r"`([^`]+)`", text))
    fields = names & FIELD_TOOLS
    if names & {"bus", "buses"}:
        fields.add("bus")
    if re.search(r"\bsends?\b", text):
        fields.add("sends")
    return names - fields, fields
