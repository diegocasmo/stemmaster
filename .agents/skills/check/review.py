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
    # Unsorted: render sums stems and orders buses and settings in recipe order, so order is part of the mix.
    return sha(json.dumps({"recipe": mix, "inputs": outside}))


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


def write_prompt(numbers, report_path, fp, notes, cache):
    text = prompt(numbers, report_path, fp, notes)
    path = cache / "review-prompt.md"
    path.write_text(text)
    return path, sha(text)


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
