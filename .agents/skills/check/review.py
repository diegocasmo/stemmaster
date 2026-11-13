import copy
import hashlib
import json
import os
import re
from pathlib import Path

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AGENTS = ("claude", "codex")
VERDICT = re.compile(r"```json\s*(\{.*?\})\s*```\s*$", re.S)
# A reviewer that ran elsewhere read another AGENTS.md or CLAUDE.md, which could have told it what to say.
REPO = Path(__file__).resolve().parents[3]
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def digest(value):
    return sha(json.dumps(value, sort_keys=True))


def trial_master(recipe):
    record = next((r for r in recipe.get("moves", []) if r.get("move") == "mix-bus.trial-master"), None)
    ids = set(record.get("steps", [])) if record else set()
    return {"record": record, "steps": [s for s in recipe.get("steps", []) if s.get("id") in ids]}


def mix_part(recipe, reviewed_trial):
    # During mastering the master steps replace the trial master, so the reviewed trial comes back from the review entry.
    mix = copy.deepcopy(recipe)
    for key in ("reviews", "notes"):
        mix.pop(key, None)
    mix["steps"] = [s for s in mix.get("steps", []) if s.get("stage") != "master"] + reviewed_trial["steps"]
    mix["moves"] = [r for r in mix.get("moves", []) if not str(r.get("move", "")).startswith("master.")
                    and r.get("move") != "mix-bus.trial-master"] + [reviewed_trial["record"]]
    return mix


def fingerprint(mix, outside):
    return digest({"recipe": mix, "inputs": outside})


def report_digest(numbers):
    return digest({key: numbers.get(key) for key in ("current", "records", "contracts", "questions")})


def questions(numbers):
    found = dict(numbers.get("questions", {}))
    for entry in numbers["contracts"]:
        where = f"{entry['move']}@{entry['source']}"
        for leaf in entry["leaves"]:
            if leaf["state"] == "review":
                measured = "not measured" if leaf["value"] is None else f"measured {leaf['value']}"
                why = f"; {leaf['why']}" if leaf.get("why") else ""
                found[f"{where}:{leaf['id']}"] = f"{entry['move']} on {entry['source']}: {leaf['text']} ({measured}{why})"
        if entry.get("override"):
            found[f"{where}:override"] = f"{entry['move']} on {entry['source']}: override '{entry['override'].get('reason')}' against {entry['override'].get('leaves')}"
    return found


FAST_PATHS = [
    "Fast paths: read the evidence pack first, then report.json, which holds every number the check computed. The pack "
    "is a map, not proof: verify any number you rely on in report.json, and fail the mix if the pack's sha256 differs.",
    "To test a claim, render (.agents/skills/render/render.py, about 30 s) or measure a tap (.agents/skills/analyze/) "
    "before any check. If you must run a check, run it on a copy of the song cut short (.agents/skills/check/cut.py) "
    "with STEMMASTER_CHECK_WORKERS=1, never a cold full check in the song folder.",
]
VERDICT_BLOCK = [
    "List every problem you find in findings, each with its numbers, including any answer that fails: a problem not "
    "listed there counts as passed. On a fail, the next review gets your findings word for word.",
    "End your final message with exactly one fenced json block and nothing after it:",
    '```json\n{"verdict": "pass or fail", "fingerprint": "<the mix fingerprint>", "report": "<the report digest>", '
    '"answers": {"<each question id>": "<your measured answer>"}, "findings": ["<each problem you found, with its numbers>"]}\n```',
]


def pack_path(report_path):
    return Path(report_path).resolve().parent / "evidence-pack.md"


def header(numbers, report_path, fp):
    return [
        "You are an adversarial reviewer of one stemmaster mix. Judge it against docs/mixing-chains/ and the check report.",
        "Read only this song's files. Do not edit the repo. Never post renders.",
        f"Report: {Path(report_path).resolve()} (and report.json next to it)",
        f"Evidence pack: {pack_path(report_path)} (sha256 {sha(evidence_pack(numbers, fp))})",
        f"Mix fingerprint: {fp}",
        f"Report digest: {report_digest(numbers)}",
        "",
        *FAST_PATHS,
        "",
    ]


def prompt(numbers, report_path, fp, notes):
    lines = [
        *header(numbers, report_path, fp),
        "Answer every question below from the report's numbers (and your own measurements if you need them):",
        *[f"- {key}: {text}" for key, text in sorted(questions(numbers).items())],
        "",
        "Also judge these reasons the recipe gives:",
        *[f"- {note}" for note in notes],
        "",
        *VERDICT_BLOCK,
    ]
    return "\n".join(lines) + "\n"


def write_prompt(text, cache):
    path = cache / "review-prompt.md"
    path.write_text(text)
    return path, sha(text)


THRESHOLD = re.compile(r"^\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
# A leaf this close to its threshold could flip on a small change: within a quarter of it, and never under 0.5.
NEAR = 0.25
NEAR_FLOOR = 2.0


def near(leaf):
    match = THRESHOLD.match(str(leaf.get("test", "")))
    value = leaf.get("value")
    if not match or leaf.get("state") not in ("true", "false") or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    threshold = float(match[2])
    return abs(value - threshold) <= NEAR * max(abs(threshold), NEAR_FLOOR)


def leaf_line(leaf):
    return f"{leaf.get('id')} {leaf.get('text')}: {leaf.get('state')}, {shown(leaf.get('value'))} ({leaf.get('test', 'no test')})"


def evidence_pack(numbers, fp):
    """A short map of the report for reviewers, generated from its numbers alone."""
    leaves = {f"{c['move']}@{c['source']}:{leaf['id']}": (c, leaf) for c in numbers["contracts"] for leaf in c["leaves"]}
    entries = {f"{c['move']}@{c['source']}": c for c in numbers["contracts"]}
    lines = ["# Evidence pack", "", f"Mix fingerprint: {fp}", "",
             "Generated by check.py from report.json. A map, not proof: verify any number you rely on in report.json. "
             "Override reasons are what the mixer says, not evidence.", "", "## Questions and the numbers behind them", ""]
    for key, question in sorted(questions(numbers).items()):
        lines.append(f"- {key}: {question}")
        if key in leaves:
            entry, leaf = leaves[key]
            lines += [f"  - {leaf_line(leaf)}", f"  - record: decision {entry.get('decision')}, default outcome {entry.get('outcome')}"]
        elif key.endswith(":override"):
            entry = entries.get(key.removesuffix(":override"), {})
            named = set((entry.get("override") or {}).get("leaves") or [])
            lines.append(f"  - record: decision {entry.get('decision')}, default outcome {entry.get('outcome')}")
            lines += [f"  - against {leaf_line(leaf)}" for leaf in entry.get("leaves", []) if leaf.get("id") in named]
    lines += ["", "## Failing and near-failing records", ""]
    for entry in numbers["contracts"]:
        close = [leaf for leaf in entry["leaves"] if near(leaf)]
        if entry["fails"] or close:
            lines.append(f"- {entry['move']}@{entry['source']}: decision {entry.get('decision')}, default outcome {entry.get('outcome')}")
            lines += [f"  - fail: {fail}" for fail in entry["fails"]] + [f"  - near: {leaf_line(leaf)}" for leaf in close]
    current = numbers.get("current") or {}
    mix = {key: value for key, value in (current.get("mix") or {}).items() if key in ("integrated_lufs", "side_mid_db", "side_mid_below_120hz_db", "correlation")}
    lines += ["", "## Trial master and mix", ""]
    lines += [f"- {key}: {shown(value)}" for key, value in sorted(flat({"trial": current.get("trial"), "mix": mix}).items())]
    return "\n".join(lines) + "\n"


def write_pack(numbers, fp, report_path):
    path = pack_path(report_path)
    path.write_text(evidence_pack(numbers, fp))
    return path


REPORTED = ("current", "records", "contracts", "questions")


def snapshot(mix, outside, files, report_path, numbers, notes, base=None):
    """What a review saw: enough to rebuild its prompt, and to list what a later mix changed."""
    return {"fingerprint": fingerprint(mix, outside), "base": base, "mix": mix, "inputs": outside, "files": files,
            "report": str(Path(report_path).resolve()), "numbers": {key: numbers.get(key) for key in REPORTED}, "notes": notes}


def snapshot_path(cache, fp):
    return cache / "reviews" / f"{fp}.json"


def save_snapshot(cache, snap):
    path = snapshot_path(cache, snap["fingerprint"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap))
    return path


def load_snapshot(cache, fp):
    """A snapshot whose recipe and inputs still hash to its fingerprint; its numbers are bound by the verdicts' report digest."""
    try:
        snap = json.loads(snapshot_path(cache, fp).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or snap.get("fingerprint") != fp or fingerprint(snap.get("mix"), snap.get("inputs")) != fp:
        return None
    return snap


def record_key(record, seen):
    key = f"{record.get('move')}@{record.get('source')}"
    seen[key] = seen.get(key, 0) + 1
    return key if seen[key] == 1 else f"{key}#{seen[key]}"


def keyed_records(mix):
    seen = {}
    return {record_key(r, seen): r for r in mix.get("moves", []) if isinstance(r, dict)}


def field_value(mix, field):
    kind, rest = field.split(".", 1)
    entries = mix.get(kind, {}) if kind in ("stems", "buses") else {}
    if ".sends." in rest:
        name, bus = rest.split(".sends.", 1)
        return [send for send in (entries.get(name) or {}).get("sends", []) if send.get("bus") == bus]
    name, key = rest.rsplit(".", 1)
    return (entries.get(name) or {}).get(key)


def claimed(mix, record):
    steps = {step.get("id"): step for step in mix.get("steps", [])}
    return {"steps": [steps.get(step_id) for step_id in record.get("steps", [])],
            "fields": {field: field_value(mix, field) for field in record.get("fields", []) if isinstance(field, str) and "." in field}}


def flat(value, prefix=""):
    if isinstance(value, dict) and value:
        out = {}
        for key, inner in value.items():
            out |= flat(inner, f"{prefix}.{key}" if prefix else str(key))
        return out
    return {prefix: value}


def shown(value):
    return json.dumps(value, sort_keys=True)


# What a contract judged. Its variant labels (needed_read_on, with, without) hash the whole recipe, so they change
# with any edit and say nothing about this move.
JUDGED = ("decision", "outcome", "needed", "worked", "fails", "engaged", "override", "leaves")


def judged(entry):
    return {key: (entry or {}).get(key) for key in JUDGED} if entry else None


def leaf_changes(old, new):
    old, new = old or {}, new or {}
    lines = [f"{key}: {shown(old.get(key))} -> {shown(new.get(key))}"
             for key in JUDGED[:-1] if old.get(key) != new.get(key)]
    old_leaves = {leaf["id"]: leaf for leaf in old.get("leaves", [])}
    new_leaves = {leaf["id"]: leaf for leaf in new.get("leaves", [])}
    show = lambda leaf: "absent" if leaf is None else f"{leaf.get('state')} {shown(leaf.get('value'))}"
    return lines + [f"{leaf_id}: {show(old_leaves.get(leaf_id))} -> {show(new_leaves.get(leaf_id))}"
                    for leaf_id in sorted(set(old_leaves) | set(new_leaves)) if old_leaves.get(leaf_id) != new_leaves.get(leaf_id)]


def contract_index(numbers):
    seen = {}
    return {record_key(c, seen): c for c in numbers.get("contracts") or []}


MAX_SCOPED = 3
MAX_CHANGED = 0.4
SPOT_CHECKS = 5


def scope(now, base, state):
    """What changed from the reviewed mix `base` (whose review left `state`) to `now`, or None when only a full review
    can judge it: changed stems, plugins, profile or chain README, a code change the snapshots can't name file by file,
    MAX_SCOPED scoped rounds in a row, or more than MAX_CHANGED of the records changed."""
    if base is None or now["inputs"][1:] != base["inputs"][1:] or now["report"] != base["report"]:
        return None
    if state["scoped_run"] >= MAX_SCOPED:
        return None
    files = sorted(path for path in set(now["files"]) | set(base["files"]) if now["files"].get(path) != base["files"].get(path))
    if (now["inputs"][0] != base["inputs"][0]) != bool(files) or "docs/mixing-chains/README.md" in files:
        return None
    docs = {Path(path).stem for path in files if path.startswith("docs/mixing-chains/")}
    mix, old = now["mix"], base["mix"]
    records, old_records = keyed_records(mix), keyed_records(old)
    contracts, old_contracts = contract_index(now["numbers"]), contract_index(base["numbers"])
    moves = {}
    for key, record in records.items():
        why = []
        if record != old_records.get(key):
            why.append("recipe")
        elif claimed(mix, record) != claimed(old, record):
            why.append("settings")
        if judged(contracts.get(key)) != judged(old_contracts.get(key)):
            why.append("check")
        if str(record.get("move", "")).split(".", 1)[0] in docs:
            why.append("doc")
        if why:
            moves[key] = {"why": why, "measured": leaf_changes(old_contracts.get(key), contracts.get(key))}
    removed = sorted(set(old_records) - set(records))
    total = len(set(records) | set(old_records))
    if len(moves) + len(removed) > MAX_CHANGED * total:
        return None
    rest = lambda recipe: flat({k: v for k, v in recipe.items() if k not in ("moves", "steps")})
    after, before = rest(mix), rest(old)
    steps = {s.get("id"): s for s in mix.get("steps", [])}
    old_steps = {s.get("id"): s for s in old.get("steps", [])}
    recipe = [f"{path}: {shown(before.get(path))} -> {shown(after.get(path))}"
              for path in sorted(set(after) | set(before)) if after.get(path) != before.get(path)]
    recipe += [f"steps.{step_id}: {shown(old_steps.get(step_id))} -> {shown(steps.get(step_id))}"
               for step_id in sorted(set(steps) | set(old_steps), key=str) if steps.get(step_id) != old_steps.get(step_id)]
    if [i for i in steps if i in old_steps] != [i for i in old_steps if i in steps]:
        recipe.append(f"step order: {shown(list(old_steps))} -> {shown(list(steps))}")
    current, old_current = flat(now["numbers"].get("current") or {}), flat(base["numbers"].get("current") or {})
    mix_numbers = [f"{path}: {shown(old_current.get(path))} -> {shown(current.get(path))}"
                   for path in sorted(set(current) | set(old_current)) if current.get(path) != old_current.get(path)]
    asked = questions(now["numbers"])
    changed = {k: v for k, v in asked.items() if state["covered"].get(k) != v}
    # Chosen by the fingerprint, so the mixer can't pick which unchanged answers get a second look.
    spot = sorted((k for k in asked if k not in changed), key=lambda k: sha(now["fingerprint"] + k))[:SPOT_CHECKS]
    return {"files": files, "recipe": recipe, "moves": moves, "removed": removed, "records": total, "mix": mix_numbers,
            "questions": changed, "spot": {k: asked[k] for k in sorted(spot)}, "all_questions": len(asked),
            "findings": {f"finding:{i}": text for i, text in enumerate(state["findings"], 1)},
            "notes": [note for note in now["notes"] if note not in base["notes"]], "round": state["scoped_run"] + 1,
            "base_passed": state["passed"]}


def scoped_prompt(now, base, found):
    """A review of only what changed since the review of `base`; it answers for the whole mix's fingerprint."""
    contracts = contract_index(now["numbers"])
    moves = []
    for key, change in found["moves"].items():
        entry = contracts.get(key) or {}
        moves.append(f"- {key} ({', '.join(change['why'])} changed): decision {entry.get('decision')}, default outcome {entry.get('outcome')}")
        moves += [f"  - {line}" for line in change["measured"]]
    section = lambda title, lines: [title, *(lines or ["- none"]), ""]
    asked = len(found["questions"]) + len(found["spot"])
    lines = [
        *header(now["numbers"], now["report"], now["fingerprint"]),
        f"Last review: {base['fingerprint']} ({'passed' if found['base_passed'] else 'failed'}). "
        f"Scoped round {found['round']} of at most {MAX_SCOPED} in a row.",
        f"{len(found['moves']) + len(found['removed'])} of {found['records']} records changed, {asked} of "
        f"{found['all_questions']} questions asked ({len(found['spot'])} spot checks), {len(found['findings'])} findings carried.",
        "",
        "This review is scoped. Every move not listed below has the same recipe entry, settings and check results as in the "
        "last review, and both of its reviewers answered every question not asked here, with the same text. Judge what "
        "changed, whether each carried finding is fixed, and whether any change breaks something. Fail the mix if a "
        "change needs more than this list and the report to judge.",
        "",
        *section("Changed check code or chain docs:", [f"- {path}" for path in found["files"]]),
        *section("Recipe changes (old -> new):", [f"- {line}" for line in found["recipe"]]),
        *section("Changed moves (each has its own section in the report), with the check results that moved (old -> new):", moves),
        *section("Removed moves:", [f"- {key}" for key in found["removed"]]),
        *section("Mix and trial master numbers that moved (old -> new):", [f"- {line}" for line in found["mix"]]),
        *section("Findings of the last review, word for word from its reviewers. Answer each id: fixed or not, with numbers:",
                 [f"- {key}: " + text.replace("\n", "\n  ") for key, text in found["findings"].items()]),
        "Answer every question below from the report's numbers (and your own measurements if you need them):",
        *[f"- {key}: {value}" for key, value in sorted(found["questions"].items())],
        "",
        "Spot checks, unchanged questions chosen by the mix fingerprint. Answer them as well:",
        *[f"- {key}: {value}" for key, value in found["spot"].items()],
        "",
        "Also judge these reasons the recipe gives:",
        *[f"- {note}" for note in found["notes"]],
        "",
        *VERDICT_BLOCK,
    ]
    return "\n".join(lines) + "\n"


def prompt_for(now, base=None, state=None):
    """The prompt a review of `now` is sent and the ids it must answer: scoped on `base` when given, else full."""
    if base is None:
        return prompt(now["numbers"], now["report"], now["fingerprint"], now["notes"]), questions(now["numbers"])
    found = scope(now, base, state)
    return scoped_prompt(now, base, found), found["questions"] | found["spot"] | found["findings"]


def answered(verdict):
    return {k for k, v in (verdict.get("answers") or {}).items() if isinstance(v, str) and v.strip()}


def review_link(recipe, now, cache, seen=()):
    """Reads the review of `now` and, for a scoped one, each review below it down to a full one. Returns errors that
    make the chain not count, and the review's state: which questions both reviewers answered (with their text), its
    findings if it didn't pass, its problems, and how many scoped rounds in a row it ends."""
    fp, base_fp = now["fingerprint"], now.get("base")
    base = state = None
    if base_fp is not None:
        base = load_snapshot(cache, base_fp) if base_fp not in seen else None
        if base is None:
            return [f"the review of {fp} builds on {base_fp}, whose snapshot is missing or changed: review the mix in full"], None
        errors, state = review_link(recipe, base, cache, {*seen, fp})
        if errors:
            return [f"review of {base_fp} (built on): {error}" for error in errors], None
        if scope(now, base, state) is None:
            return [f"the review of {fp} builds on {base_fp}, but needs a full review (stems, plugins, profile or chain "
                    f"README changed, {MAX_SCOPED} scoped rounds in a row, or over {MAX_CHANGED:.0%} of records changed)"], None
    text_now, asked = prompt_for(now, base, state)
    errors, verdicts = read_reviews(recipe, fp, report_digest(now["numbers"]), sha(text_now))
    if errors:
        return errors, None
    problems = [f"mix review by {agent}: verdict is {v.get('verdict')}" for agent, v in verdicts.items() if v.get("verdict") != "pass"]
    problems += [f"mix review by {agent}: unanswered {sorted(missing)}" for agent, v in verdicts.items()
                 if (missing := set(asked) - answered(v))]
    both = set.intersection(*(answered(v) for v in verdicts.values()))
    covered = {k: v for k, v in questions(now["numbers"]).items()
               if (k in both if k in asked else state is not None and state["covered"].get(k) == v)}
    findings = []
    if problems:
        for agent, verdict in verdicts.items():
            listed = [f for f in verdict.get("findings") or [] if isinstance(f, str) and f.strip()]
            findings += [f"{agent}: {f}" for f in listed or ([verdict["message"]] if verdict.get("verdict") != "pass" and verdict["message"] else [])]
        # A carried finding no reviewer answered is carried again, as its reviewers wrote it.
        if state is not None:
            findings += [f for i, f in enumerate(state["findings"], 1) if f"finding:{i}" not in both]
    return [], {"covered": covered, "findings": findings, "passed": not problems, "problems": problems,
                "scoped_run": 0 if base is None else state["scoped_run"] + 1}


def verify_chain(recipe, now, cache):
    """The mix passes review when its latest review passes and every review below it, down to a full one, counts."""
    errors, state = review_link(recipe, now, cache)
    return errors or state["problems"]


def latest_base(recipe, now, cache):
    """The latest other reviewed mix, passed or failed, whose reviews down to a full one count, and its state; or
    (None, None) when there is none or the next review must be full."""
    reviewed = [r.get("fingerprint") for r in recipe.get("reviews", []) if r.get("stage") == "mix"]
    for fp in dict.fromkeys(reversed(reviewed)):
        if fp == now["fingerprint"] or not isinstance(fp, str) or (base := load_snapshot(cache, fp)) is None:
            continue
        errors, state = review_link(recipe, base, cache)
        if errors:
            continue
        return (base, state) if scope(now, base, state) is not None else (None, None)
    return None, None


PASTED = re.compile(r'^\s*<pasted_content id="(\w+)">\n(.*)\n</pasted_content id="\1">\s*$', re.S)


def unwrap(text):
    """Claude Code stores a pasted prompt inside a pasted_content wrapper; only a message that is that wrapper alone counts."""
    match = PASTED.match(text)
    return match.group(2) if match else text


TEXT_PARTS = ("text", "input_text", "output_text")


def text_of(content, user=False):
    """A message's text; None for a user message that also holds an image or file, whose words no hash can bind."""
    if isinstance(content, str):
        return content
    if user and any(part.get("type") not in TEXT_PARTS for part in content):
        return None
    return "\n".join(part.get("text", "") for part in content if part.get("type") in TEXT_PARTS)


def claude_messages(session):
    paths = list(CLAUDE_PROJECTS.glob(f"*/{session}.jsonl"))
    if len(paths) != 1:
        return None
    users, assistants = [], []
    for line in paths[0].read_text().splitlines():
        entry = json.loads(line)
        if entry.get("sessionId", session) != session or entry.get("cwd", str(REPO)) != str(REPO):
            return None
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = entry.get("message", {}).get("content")
        # Tool results come back as user entries; anything else a user entry holds is a message to the reviewer.
        if entry.get("type") == "user" and (isinstance(content, str) or any(p.get("type") != "tool_result" for p in content or [])):
            users.append(text_of(content, user=True))
        elif entry.get("type") == "assistant" and any(p.get("type") == "text" for p in content or []):
            assistants.append(text_of(content))
    return users, assistants


ENVIRONMENT_TAGS = {"environment_context", "cwd", "shell", "current_date", "timezone", "filesystem", "workspace_roots", "root",
                    "permission_profile", "file_system", "entry", "special", "path", "approval_policy", "sandbox_mode",
                    "network_access", "writable_roots"}
TAG = re.compile(r"<(/?)([a-z_]+)((?:\s+[a-z_]+=\"[^\"<>]*\")*)\s*>")
ATTRIBUTE = re.compile(r'([a-z_]+)="([^"]*)"')
ENVIRONMENT_ATTRIBUTES = {"type", "access"}
PATH = re.compile(r"^(/|:)[\w/:.~+@-]*$")
POLICY = re.compile(r"^(never|on-request|on-failure|untrusted|read|write|read-only|workspace-write|danger-full-access|enabled|disabled|restricted|managed|true|false)$")
VALUES = {"shell": re.compile(r"^(zsh|bash|sh|fish)$"), "current_date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
          "timezone": re.compile(r"^[A-Za-z]+(/[A-Za-z_+-]+)*$"), "approval_policy": POLICY, "sandbox_mode": POLICY,
          "network_access": POLICY}


def known_environment(block):
    """Codex's environment block: only its own tags and attributes, holding plain values, with the repo as cwd."""
    position, opened = 0, None
    for match in TAG.finditer(block):
        closing, name, attributes = match.groups()
        text = block[position : match.start()].strip()
        if name not in ENVIRONMENT_TAGS or (text and not (closing and opened == name and VALUES.get(name, PATH).match(text))):
            return False
        opened = None if closing else name
        if any(key not in ENVIRONMENT_ATTRIBUTES or not POLICY.match(value) for key, value in ATTRIBUTE.findall(attributes)):
            return False
        position = match.end()
    if block[position:].strip():
        return False
    cwd = re.search(r"<cwd>([^<]*)</cwd>", block)
    return cwd is None or cwd.group(1).strip() == str(REPO)


def injected(text):
    """Codex's own context before the prompt, matched exactly: the AGENTS.md files as Codex wraps them, then an
    environment block made only of Codex's tags. Any other text there could tell the reviewer what to say."""
    head, marker, environment = text.partition("<environment_context>")
    if not marker or not known_environment(marker + environment):
        return False
    if not head.strip():
        return True
    repo = (REPO / "AGENTS.md").read_text().strip()
    found = (Path.home() / ".codex" / "AGENTS.md")
    docs = (found.read_text().strip() + "\n\n--- project-doc ---\n\n" if found.exists() else "") + repo
    return head == f"# AGENTS.md instructions for {REPO}\n\n<INSTRUCTIONS>\n{docs}\n\n</INSTRUCTIONS>\n"


def codex_messages(session):
    paths = list(CODEX_SESSIONS.glob(f"**/rollout-*-{session}.jsonl"))
    if len(paths) != 1:
        return None
    users, assistants, turns = [], [], 0
    for line in paths[0].read_text().splitlines():
        entry = json.loads(line)
        payload = entry.get("payload", {})
        turns += entry.get("type") == "turn_context"
        if entry.get("type") == "session_meta" and (payload.get("id") != session or payload.get("cwd") != str(REPO)):
            return None
        if entry.get("type") != "response_item" or payload.get("type") != "message":
            continue
        text = text_of(payload.get("content", []), user=payload.get("role") == "user")
        # Codex injects its AGENTS.md and environment as user messages before the first prompt; after it, every
        # user message is a turn, whatever it starts with, so none can tell the reviewer its verdict.
        if payload.get("role") == "user" and not (not users and text is not None and injected(text)):
            users.append(text)
        elif payload.get("role") == "assistant":
            assistants.append(text)
    # One review is one turn; a second turn is a second instruction, whatever it looks like.
    return (users, assistants) if turns == 1 else (users + ["another turn"], assistants)


def read_reviews(recipe, fp, digest_now, prompt_sha):
    """Each reviewer's verdict block, plus its final message without the block, for the latest review entry of `fp`.
    Errors are anything that makes the review not count; the verdict itself is the caller's to judge."""
    reviews = [r for r in recipe.get("reviews", []) if r.get("stage") == "mix" and r.get("fingerprint") == fp]
    if not reviews:
        return [f"--master needs a mix review of fingerprint {fp} (run check --review-prompt, then the reviews)"], {}
    errors, verdicts = [], {}
    by = {entry.get("agent"): entry.get("session") for entry in reviews[-1].get("by", [])}
    own = os.environ.get("CLAUDE_CODE_SESSION_ID")
    for agent in AGENTS:
        session = by.get(agent)
        label = f"mix review by {agent}"
        if not session or not UUID.match(session):
            errors.append(f"{label}: needs a full session uuid")
            continue
        if session == own:
            errors.append(f"{label}: the reviewing session cannot be the mixing session")
            continue
        found = (claude_messages if agent == "claude" else codex_messages)(session)
        if found is None:
            errors.append(f"{label}: no transcript for session {session} on this machine")
            continue
        users, assistants = found
        if None in users:
            errors.append(f"{label}: a message to the reviewer holds an image or file, which the prompt hash cannot bind")
            continue
        # One turn only: a later message could tell the reviewer what verdict to give.
        if len(users) != 1:
            errors.append(f"{label}: the session must hold the review prompt and nothing else from the user")
            continue
        # Pasting can drop the prompt's trailing newline, so both forms count as the exact prompt.
        message = unwrap(users[0])
        if prompt_sha not in (sha(message), sha(message.strip() + "\n")):
            errors.append(f"{label}: its first message is not the review prompt")
            continue
        match = VERDICT.search(assistants[-1]) if assistants else None
        try:
            verdict = json.loads(match.group(1)) if match else None
        except json.JSONDecodeError:
            verdict = None
        if not isinstance(verdict, dict):
            errors.append(f"{label}: its last message has no json verdict block")
            continue
        if verdict.get("fingerprint") != fp or verdict.get("report") != digest_now:
            errors.append(f"{label}: its verdict is for another mix or report")
            continue
        verdicts[agent] = verdict | {"message": assistants[-1][: match.start()].strip()}
    return errors, verdicts


def verify(recipe, fp, digest_now, prompt_sha, asked):
    errors, verdicts = read_reviews(recipe, fp, digest_now, prompt_sha)
    for agent, verdict in verdicts.items():
        if verdict.get("verdict") != "pass":
            errors.append(f"mix review by {agent}: verdict is {verdict.get('verdict')}")
        if missing := set(asked) - answered(verdict):
            errors.append(f"mix review by {agent}: unanswered {sorted(missing)}")
    return errors
