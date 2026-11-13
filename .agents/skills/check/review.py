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


def prompt(numbers, report_path, fp, notes):
    asked = questions(numbers)
    lines = [
        "You are an adversarial reviewer of one stemmaster mix. Judge it against docs/mixing-chains/ and the check report.",
        "Read only this song's files. Do not edit the repo. Never post renders.",
        f"Report: {Path(report_path).resolve()}",
        f"Mix fingerprint: {fp}",
        f"Report digest: {report_digest(numbers)}",
        "",
        "Answer every question below from the report's numbers (and your own measurements if you need them):",
        *[f"- {key}: {text}" for key, text in sorted(asked.items())],
        "",
        "Also judge these reasons the recipe gives:",
        *[f"- {note}" for note in notes],
        "",
        "End your final message with exactly one fenced json block and nothing after it:",
        '```json\n{"verdict": "pass or fail", "fingerprint": "<the mix fingerprint>", "report": "<the report digest>", '
        '"answers": {"<each question id>": "<your measured answer>"}}\n```',
    ]
    return "\n".join(lines) + "\n"


def write_prompt(text, cache):
    path = cache / "review-prompt.md"
    path.write_text(text)
    return path, sha(text)


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


def leaf_changes(old, new):
    old, new = old or {}, new or {}
    lines = [f"{key}: {shown(old.get(key))} -> {shown(new.get(key))}"
             for key in ("decision", "outcome", "needed", "worked", "fails", "engaged") if old.get(key) != new.get(key)]
    old_leaves = {leaf["id"]: leaf for leaf in old.get("leaves", [])}
    new_leaves = {leaf["id"]: leaf for leaf in new.get("leaves", [])}
    show = lambda leaf: "absent" if leaf is None else f"{leaf.get('state')} {shown(leaf.get('value'))}"
    return lines + [f"{leaf_id}: {show(old_leaves.get(leaf_id))} -> {show(new_leaves.get(leaf_id))}"
                    for leaf_id in sorted(set(old_leaves) | set(new_leaves)) if old_leaves.get(leaf_id) != new_leaves.get(leaf_id)]


def contract_index(numbers):
    seen = {}
    return {record_key(c, seen): c for c in numbers.get("contracts") or []}


def scope(now, base):
    """What changed from the reviewed mix `base` to `now`, or None when only a full review can judge it: changed stems,
    plugins, profile or chain README, or a code change the snapshots can't name file by file."""
    if base is None or now["inputs"][1:] != base["inputs"][1:] or now["report"] != base["report"]:
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
        if contracts.get(key) != old_contracts.get(key):
            why.append("check")
        if str(record.get("move", "")).split(".", 1)[0] in docs:
            why.append("doc")
        if why:
            moves[key] = {"why": why, "measured": leaf_changes(old_contracts.get(key), contracts.get(key))}
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
    asked, old_asked = questions(now["numbers"]), questions(base["numbers"])
    return {"files": files, "recipe": recipe, "moves": moves, "removed": sorted(set(old_records) - set(records)),
            "mix": mix_numbers, "questions": {k: v for k, v in asked.items() if old_asked.get(k) != v},
            "notes": [note for note in now["notes"] if note not in base["notes"]]}


def scoped_prompt(now, base, found):
    """A review of only what changed since the passing review of `base`; it answers for the whole mix's fingerprint."""
    contracts = contract_index(now["numbers"])
    moves = []
    for key, change in found["moves"].items():
        entry = contracts.get(key) or {}
        moves.append(f"- {key} ({', '.join(change['why'])} changed): decision {entry.get('decision')}, default outcome {entry.get('outcome')}")
        moves += [f"  - {line}" for line in change["measured"]]
    section = lambda title, lines: [title, *(lines or ["- none"]), ""]
    lines = [
        "You are an adversarial reviewer of one stemmaster mix. Judge it against docs/mixing-chains/ and the check report.",
        "Read only this song's files. Do not edit the repo. Never post renders.",
        f"Report: {now['report']}",
        f"Mix fingerprint: {now['fingerprint']}",
        f"Report digest: {report_digest(now['numbers'])}",
        f"Last passing review: {base['fingerprint']}",
        "",
        "This review is scoped. Fresh Claude and Codex sessions passed the mix of the last passing review, and every move "
        "not listed below has the same recipe entry, settings and check results as then. Judge what changed, and whether "
        "any change breaks something that passed. Fail the mix if a change needs more than this list and the report to judge.",
        "",
        *section("Changed check code or chain docs:", [f"- {path}" for path in found["files"]]),
        *section("Recipe changes (old -> new):", [f"- {line}" for line in found["recipe"]]),
        *section("Changed moves (each has its own section in the report), with the check results that moved (old -> new):", moves),
        *section("Removed moves:", [f"- {key}" for key in found["removed"]]),
        *section("Mix and trial master numbers that moved (old -> new):", [f"- {line}" for line in found["mix"]]),
        "Answer every question below from the report's numbers (and your own measurements if you need them):",
        *[f"- {key}: {value}" for key, value in sorted(found["questions"].items())],
        "",
        "Also judge these reasons the recipe gives:",
        *[f"- {note}" for note in found["notes"]],
        "",
        "End your final message with exactly one fenced json block and nothing after it:",
        '```json\n{"verdict": "pass or fail", "fingerprint": "<the mix fingerprint>", "report": "<the report digest>", '
        '"answers": {"<each question id>": "<your measured answer>"}}\n```',
    ]
    return "\n".join(lines) + "\n"


def prompt_for(now, base):
    """The prompt a review of `now` is sent and the questions it asks: scoped on `base` when given, else full."""
    if base is None:
        return prompt(now["numbers"], now["report"], now["fingerprint"], now["notes"]), questions(now["numbers"])
    found = scope(now, base)
    return scoped_prompt(now, base, found), found["questions"]


def verify_chain(recipe, now, cache, seen=()):
    """Verifies the review of `now` and, for a scoped one, each review it builds on down to a full one. Together they
    answered every question the mix asks: a scoped prompt asks each one that is new or whose text changed."""
    fp, base_fp = now["fingerprint"], now.get("base")
    if base_fp is None:
        text_now, asked = prompt_for(now, None)
        return verify(recipe, fp, report_digest(now["numbers"]), sha(text_now), asked)
    base = load_snapshot(cache, base_fp) if base_fp not in seen else None
    if base is None:
        return [f"the review of {fp} builds on {base_fp}, whose snapshot is missing or changed: review the mix in full"]
    if scope(now, base) is None:
        return [f"the review of {fp} builds on {base_fp}, but stems, plugins, profile or the chain README changed since: review the mix in full"]
    text_now, asked = prompt_for(now, base)
    errors = verify(recipe, fp, report_digest(now["numbers"]), sha(text_now), asked)
    return errors + [f"review of {base_fp} (built on): {error}" for error in verify_chain(recipe, base, cache, {*seen, fp})]


def passing_base(recipe, now, cache):
    """The latest other reviewed mix whose reviews, down to a full one, still pass, and that a scoped review can build on."""
    reviewed = [r.get("fingerprint") for r in recipe.get("reviews", []) if r.get("stage") == "mix"]
    for fp in dict.fromkeys(reversed(reviewed)):
        if fp == now["fingerprint"] or not isinstance(fp, str):
            continue
        base = load_snapshot(cache, fp)
        if base is not None and scope(now, base) is not None and not verify_chain(recipe, base, cache):
            return base
    return None


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


def verify(recipe, fp, digest_now, prompt_sha, asked):
    reviews = [r for r in recipe.get("reviews", []) if r.get("stage") == "mix" and r.get("fingerprint") == fp]
    if not reviews:
        return [f"--master needs a mix review of fingerprint {fp} (run check --review-prompt, then the reviews)"]
    errors = []
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
        if verdict is None:
            errors.append(f"{label}: its last message has no json verdict block")
            continue
        if verdict.get("verdict") != "pass":
            errors.append(f"{label}: verdict is {verdict.get('verdict')}")
        if verdict.get("fingerprint") != fp or verdict.get("report") != digest_now:
            errors.append(f"{label}: its verdict is for another mix or report")
        if missing := set(asked) - {k for k, v in (verdict.get("answers") or {}).items() if isinstance(v, str) and v.strip()}:
            errors.append(f"{label}: unanswered {sorted(missing)}")
    return errors
