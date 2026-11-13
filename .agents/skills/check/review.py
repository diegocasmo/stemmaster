import copy
import hashlib
import json
import os
import re
import sys
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


MARKER = "You are an adversarial reviewer of one stemmaster mix. Judge it against docs/mixing-chains/ and the check report."
FAST_PATHS = [
    "Fast paths: read the evidence pack first, then this mix's numbers file. The pack is a map, not proof: verify any "
    "number you rely on in the numbers file, and fail the mix if the pack's sha256 differs.",
    "To test a claim, render (.agents/skills/render/render.py, about 30 s) or measure a tap (.agents/skills/analyze/) "
    "before any check. If you must run a check, run it on a copy of the song cut short (.agents/skills/check/cut.py) "
    "with STEMMASTER_CHECK_WORKERS=1, never a cold full check in the song folder.",
]
VERDICT_BLOCK = [
    "List every problem you find in findings, each with its numbers, including any answer that fails. A pass lists no "
    "findings. Every finding is carried, word for word, into each later review until both reviewers of a round give it "
    'status "fixed" in the block below.',
    "End your final message with exactly one fenced json block and nothing after it:",
    '```json\n{"verdict": "pass or fail", "fingerprint": "<the mix fingerprint>", "report": "<the report digest>", '
    '"answers": {"<each question id>": "<your measured answer>"}, "findings": ["<each problem you found, with its numbers>"], '
    '"status": {"<each open finding id>": "fixed or not fixed"}}\n```',
]
REPORTED = ("current", "records", "contracts", "questions")
SNAPSHOT_FIELDS = {"fingerprint": str, "mix": dict, "inputs": list, "files": dict, "report": str, "numbers": dict,
                   "notes": list, "plan": dict}
MAX_SCOPED = 3
MAX_CHANGED = 0.4
SPOT_CHECKS = 5
TEXT = {"note", "reason", "review", "tool_reason", "chain_reason", "split_reason", "role_reason"}


def reviews_folder(report):
    # Write-once files: a snapshot or pack is named by the hash of its own content.
    return Path(report).parent / "reviews"


def write_once(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x") as file:
            file.write(text)
    except FileExistsError:
        pass
    return path


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
             "Generated by check.py from the report's numbers. A map, not proof: verify any number you rely on in the "
             "numbers file. Override reasons are what the mixer says, not evidence.", "", "## Questions and the numbers behind them", ""]
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


def pack_path(snap):
    return reviews_folder(snap["report"]) / f"pack-{sha(evidence_pack(snap['numbers'], snap['fingerprint']))}.md"


def snapshot(mix, outside, files, report_path, numbers, notes):
    """What a review sees, without its plan: enough to rebuild its prompt and to list what a later mix changed."""
    return {"fingerprint": fingerprint(mix, outside), "mix": mix, "inputs": outside, "files": files,
            "report": str(Path(report_path).resolve()), "numbers": {key: numbers.get(key) for key in REPORTED}, "notes": notes}


def snapshot_path(snap):
    return reviews_folder(snap["report"]) / f"{digest(snap)}.json"


def save_review(snap):
    """Writes the snapshot and pack once; a later mix with the same fingerprint gets its own snapshot, never this one."""
    write_once(pack_path(snap), evidence_pack(snap["numbers"], snap["fingerprint"]))
    return write_once(snapshot_path(snap), json.dumps(snap))


def load_snapshot(folder, key):
    """The snapshot whose content hashes to `key`, with every field a prompt is built from; None otherwise."""
    if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        return None
    try:
        snap = json.loads((folder / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or digest(snap) != key or any(not isinstance(snap.get(k), t) for k, t in SNAPSHOT_FIELDS.items()):
        return None
    if fingerprint(snap["mix"], snap["inputs"]) != snap["fingerprint"] or not all(k in snap["numbers"] for k in REPORTED):
        return None
    return snap


def record_key(record, seen):
    key = f"{record.get('move')}@{record.get('source')}"
    seen[key] = seen.get(key, 0) + 1
    return key if seen[key] == 1 else f"{key}#{seen[key]}"


def keyed_records(mix):
    """Records a contract judges, by move and source; balance entries change nothing on their own."""
    seen = {}
    return {record_key(r, seen): r for r in mix.get("moves", []) if isinstance(r, dict) and r.get("move") != "balance"}


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
            "fields": {field: field_value(mix, field) for field in record.get("fields", []) if isinstance(field, str) and field.count(".") >= 2}}


def flat(value, prefix=""):
    if isinstance(value, dict) and value:
        out = {}
        for key, inner in value.items():
            out |= flat(inner, f"{prefix}.{key}" if prefix else str(key))
        return out
    return {prefix: value}


def shown(value):
    return json.dumps(value, sort_keys=True)


def untexted(value):
    """The recipe without the mixer's prose, which reaches reviewers only as reasons to judge."""
    if isinstance(value, dict):
        return {k: untexted(v) for k, v in value.items() if k not in TEXT}
    if isinstance(value, list):
        return [untexted(v) for v in value]
    return value


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


def chain_doc(snap, move):
    return snap["files"].get(f"docs/mixing-chains/{str(move).split('.', 1)[0]}.md")


def question_digests(snap):
    """Each question with a digest of everything it rests on: its text, and for a move's question the move's recipe
    entry, claimed settings, judged check result and chain doc. A question whose digest changed is asked again."""
    records, contracts = keyed_records(snap["mix"]), contract_index(snap["numbers"])
    out = {}
    for key, text in questions(snap["numbers"]).items():
        record = key.split(":", 1)[0]
        basis = [text]
        if record in contracts:
            move = records.get(record)
            basis += [untexted(move), move and claimed(snap["mix"], move), judged(contracts[record]),
                      chain_doc(snap, contracts[record]["move"])]
        out[key] = (text, digest(basis))
    return out


def diff(now, base):
    """What changed from the mix of snapshot `base` to `now`, or None when only a full review can judge it: changed
    stems, plugins, profile or chain README, a code hash its file list doesn't explain, or more than MAX_CHANGED of the
    judged records changed."""
    if now["inputs"][1:] != base["inputs"][1:] or now["report"] != base["report"]:
        return None
    files = sorted(path for path in set(now["files"]) | set(base["files"]) if now["files"].get(path) != base["files"].get(path))
    if (now["inputs"][0] != base["inputs"][0]) != bool(files) or "docs/mixing-chains/README.md" in files:
        return None
    mix, old = now["mix"], base["mix"]
    records, old_records = keyed_records(mix), keyed_records(old)
    contracts, old_contracts = contract_index(now["numbers"]), contract_index(base["numbers"])
    moves = {}
    for key, record in records.items():
        why = []
        if untexted(record) != untexted(old_records.get(key)):
            why.append("recipe")
        elif claimed(mix, record) != claimed(old, record):
            why.append("settings")
        if judged(contracts.get(key)) != judged(old_contracts.get(key)):
            why.append("check")
        if chain_doc(now, record.get("move")) != chain_doc(base, record.get("move")):
            why.append("doc")
        if why:
            moves[key] = {"why": why, "measured": leaf_changes(old_contracts.get(key), contracts.get(key))}
    removed = sorted(set(old_records) - set(records))
    if len(moves) + len(removed) > MAX_CHANGED * len(set(records) | set(old_records)):
        return None
    rest = lambda recipe: flat(untexted({k: v for k, v in recipe.items() if k not in ("moves", "steps")}))
    after, before = rest(mix), rest(old)
    steps = {s.get("id"): untexted(s) for s in mix.get("steps", [])}
    old_steps = {s.get("id"): untexted(s) for s in old.get("steps", [])}
    recipe = [f"{path}: {shown(before.get(path))} -> {shown(after.get(path))}"
              for path in sorted(set(after) | set(before)) if after.get(path) != before.get(path)]
    recipe += [f"steps.{step_id}: {shown(old_steps.get(step_id))} -> {shown(steps.get(step_id))}"
               for step_id in sorted(set(steps) | set(old_steps), key=str) if steps.get(step_id) != old_steps.get(step_id)]
    if [i for i in steps if i in old_steps] != [i for i in old_steps if i in steps]:
        recipe.append(f"step order: {shown(list(old_steps))} -> {shown(list(steps))}")
    balance = lambda recipe: [untexted(r) for r in recipe.get("moves", []) if isinstance(r, dict) and r.get("move") == "balance"]
    if balance(mix) != balance(old):
        recipe.append(f"balance records: {shown(balance(old))} -> {shown(balance(mix))}")
    current, old_current = flat(now["numbers"].get("current") or {}), flat(base["numbers"].get("current") or {})
    mix_numbers = [f"{path}: {shown(old_current.get(path))} -> {shown(current.get(path))}"
                   for path in sorted(set(current) | set(old_current)) if current.get(path) != old_current.get(path)]
    return {"files": files, "recipe": recipe, "moves": moves, "removed": removed, "records": len(set(records) | set(old_records)),
            "mix": mix_numbers, "notes": [note for note in now["notes"] if note not in base["notes"]]}


def plan_scoped(now, base, last, pending):
    """A plan scoped on the latest attempt `last` (whose snapshot is `base`), or None when the next review must be full:
    the change is too large or of the wrong kind, MAX_SCOPED scoped rounds in a row, or a scoped pass of this very mix,
    which only a full review can take to --master."""
    found = diff(now, base)
    round_ = base["plan"].get("round", 0)
    if found is None or round_ >= MAX_SCOPED or (round_ and last["passed"] and base["fingerprint"] == now["fingerprint"]):
        return None
    digests, old = question_digests(now), question_digests(base)
    asked = sorted(k for k, (_, d) in digests.items() if k not in last["answered"] or old.get(k, (None, None))[1] != d)
    unchanged = [k for k in digests if k not in asked]
    seed = digest([now["fingerprint"], report_digest(now["numbers"]), sorted(last["sessions"])])
    spot = sorted(sorted(unchanged, key=lambda k: sha(seed + k))[:SPOT_CHECKS])
    return {"base": last["id"], "base_passed": last["passed"], "round": round_ + 1, "findings": pending,
            "questions": asked, "spot": spot, "moves": sorted(found["moves"])}


def asked_ids(snap):
    plan = snap["plan"]
    found = questions(snap["numbers"])
    asked = {k: found[k] for k in (plan.get("questions", []) + plan.get("spot", []))} if plan["base"] else dict(found)
    asked |= dict(plan["findings"])
    if plan["base"]:
        asked |= {f"move:{key}": "judge this changed move with its numbers" for key in plan["moves"]}
        asked["changes"] = "judge the recipe, code and mix number changes listed above"
    return asked


def render(snap, base):
    """The prompt for `snap` (with its plan); `base` is the snapshot of the attempt a scoped plan builds on."""
    plan, numbers, fp = snap["plan"], snap["numbers"], snap["fingerprint"]
    found = questions(numbers)
    lines = [
        MARKER,
        "Read only this song's files. Do not edit the repo. Never post renders.",
        f"Report: {snap['report']} (and report.json next to it; a later check may have rewritten them)",
        f"This mix's numbers (write-once): {snapshot_path(snap)}",
        f"Evidence pack: {pack_path(snap)} (sha256 {sha(evidence_pack(numbers, fp))})",
        f"Mix fingerprint: {fp}",
        f"Report digest: {report_digest(numbers)}",
        "",
        *FAST_PATHS,
        "",
    ]
    section = lambda title, items: [title, *(items or ["- none"]), ""]
    if plan["base"]:
        change = diff(snap, base)
        contracts = contract_index(numbers)
        moves = []
        for key in plan["moves"]:
            entry = contracts.get(key) or {}
            moves.append(f"- move:{key} ({', '.join(change['moves'][key]['why'])} changed): decision {entry.get('decision')}, "
                         f"default outcome {entry.get('outcome')}")
            moves += [f"  - {line}" for line in change["moves"][key]["measured"]]
        asked = len(plan["questions"]) + len(plan["spot"])
        lines += [
            f"Last review: {plan['base']} of mix {base['fingerprint']} ({'passed' if plan['base_passed'] else 'did not pass'}). "
            f"Scoped round {plan['round']} of at most {MAX_SCOPED} in a row.",
            f"{len(plan['moves']) + len(change['removed'])} of {change['records']} records changed, {asked} of {len(found)} "
            f"questions asked ({len(plan['spot'])} spot checks), {len(plan['findings'])} findings open.",
            "",
            "This review is scoped, to fix what the last review found; only a full review can clear the mix for "
            "mastering. Every move not listed below has the same recipe entry, settings, check results and chain doc as "
            "in the last review, whose reviewers answered every question not asked here. Judge what changed, whether "
            "each open finding is fixed, and whether any change breaks something. Fail the mix if a change needs more "
            "than this list and the numbers to judge.",
            "",
            *section("Changed check code or chain docs:", [f"- {shown(path)}" for path in change["files"]]),
            *section("Recipe changes (old -> new), answered under id changes:", [f"- {line}" for line in change["recipe"]]),
            *section("Changed moves (each has its own section in the report), with the check results that moved (old -> new). Answer each move id:", moves),
            *section("Removed moves:", [f"- {key}" for key in change["removed"]]),
            *section("Mix and trial master numbers that moved (old -> new):", [f"- {line}" for line in change["mix"]]),
        ]
        notes = change["notes"]
    else:
        notes = snap["notes"]
    if plan["findings"]:
        lines += section('Open findings from earlier reviews, word for word. Answer each id with its numbers, and give '
                         'it status "fixed" or "not fixed" in the verdict block:', [f"- {key}: " + text.replace("\n", "\n  ") for key, text in plan["findings"].items()])
    ids = plan["questions"] if plan["base"] else sorted(found)
    lines += ["Answer every question below from the numbers (and your own measurements if you need them):",
              *[f"- {key}: {found[key]}" for key in ids], ""]
    if plan["base"]:
        lines += ["Spot checks, unchanged questions chosen by the report and the last reviewers' sessions. Answer them as well:",
                  *[f"- {key}: {found[key]}" for key in plan["spot"]], ""]
    lines += ["Also judge these reasons the recipe gives:", *[f"- {note}" for note in notes], "", *VERDICT_BLOCK]
    return "\n".join(lines) + "\n"


def write_prompt(text, cache):
    path = cache / "review-prompt.md"
    path.write_text(text)
    return path, sha(text)


def canonical(message):
    # Pasting can drop the prompt's trailing newline, so both forms are the same prompt.
    return message.strip() + "\n"


SNAPSHOT_LINE = re.compile(r"^This mix's numbers \(write-once\): (.+)$", re.M)
STATUS = ("fixed", "not fixed")


def log(message):
    print(f"review: {message}", file=sys.stderr)


def findings_of(verdict, message, agent):
    """A reviewer's findings, word for word; a fail with none carries its final message, or says it gave no reason."""
    if verdict is None:
        return [f"{agent}: {message.strip() or 'ended with no verdict'}"]
    raw = verdict.get("findings")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else [] if raw is None else [shown(raw)]
    texts = [text for text in (item if isinstance(item, str) else shown(item) for item in items) if text.strip()]
    if not texts and verdict.get("verdict") != "pass":
        texts = [message] if message.strip() else [f"failed with no reason given (verdict {shown(verdict.get('verdict'))})"]
    return [f"{agent}: {text}" for text in texts]


def finding_id(text):
    return f"finding:{sha(text)[:12]}"


def verdict_of(assistants):
    """The verdict block of a reviewer's last message, or None, and the message without it."""
    message = assistants[-1] if assistants and isinstance(assistants[-1], str) else ""
    match = VERDICT.search(message)
    try:
        verdict = json.loads(match.group(1)) if match else None
    except ValueError:
        verdict = None
    return (verdict, message[: match.start()].strip()) if isinstance(verdict, dict) else (None, message)


def answered(verdict):
    answers = (verdict or {}).get("answers")
    return {k for k, v in answers.items() if isinstance(v, str) and v.strip()} if isinstance(answers, dict) else set()


def status_of(verdict, key):
    status = (verdict or {}).get("status")
    return status.get(key) if isinstance(status, dict) else None


def history(report):
    """Every review attempt for this song, in the order its first session started: sessions in this repo whose first
    message is a review prompt for this report, grouped by prompt. Built to plan prompts and to keep findings open; a
    transcript that can't be read is logged and skipped."""
    found = {}
    for agent, session, started in review_sessions(report):
        try:
            read = (claude_messages if agent == "claude" else codex_messages)(session, strict=False)
            # Text before the prompt (a message Codex didn't inject) keeps the session in its attempt, but it never counts.
            prompt = next((canonical(unwrap(t)) for t in (read[0] if read else []) if t is not None and canonical(unwrap(t)).startswith(MARKER)), None)
        except Exception as error:
            log(f"skipped {agent} session {session}: {type(error).__name__}: {error}")
            continue
        if prompt is None:
            continue
        users, assistants = read
        verdict, message = verdict_of(assistants)
        clean = verdict is not None and len(users) == 1 and session != os.environ.get("CLAUDE_CODE_SESSION_ID")
        attempt = found.setdefault(sha(prompt), {"id": sha(prompt), "prompt": prompt, "started": started, "sessions": {}, "findings": []})
        attempt["started"] = min(attempt["started"], started)
        attempt["sessions"][session] = {"agent": agent, "verdict": verdict if clean else None}
        attempt["findings"] += findings_of(verdict, message, agent)
    attempts = sorted(found.values(), key=lambda a: (a["started"], a["id"]))
    for attempt in attempts:
        verdicts = [s["verdict"] for agent in AGENTS for s in attempt["sessions"].values() if s["agent"] == agent] \
            if all(any(s["agent"] == agent for s in attempt["sessions"].values()) for agent in AGENTS) else [None]
        attempt["clean"] = [v for v in verdicts] if None not in verdicts else None
        attempt["passed"] = attempt["clean"] is not None and not attempt["findings"] and all(v.get("verdict") == "pass" for v in verdicts)
        attempt["answered"] = set.intersection(*(answered(v) for v in verdicts)) if attempt["clean"] else set()
    return attempts


def open_findings(attempts):
    """Findings still open after `attempts`: each stays until every clean session of a later attempt that carried it
    gives it status "fixed" in its verdict block."""
    pending = {}
    for attempt in attempts:
        for key in [k for k in pending if f"\n- {k}: " in attempt["prompt"]]:
            if attempt["clean"] and all(status_of(v, key) == "fixed" for v in attempt["clean"]):
                pending.pop(key)
        for text in attempt["findings"]:
            pending.setdefault(finding_id(text), text)
    return pending


def readable_history(report):
    try:
        return history(report)
    except Exception as error:
        log(f"review history unreadable, planning a full review: {type(error).__name__}: {error}")
        return None


def plan_review(now):
    """Plans and writes the next review of the mix in snapshot `now`: scoped on the latest attempt when its snapshot
    loads and the change allows, else full. Every open finding rides along. Returns the prompt, asked ids and plan."""
    attempts = readable_history(now["report"])
    pending = open_findings(attempts or [])
    plan, base = {"base": None, "round": 0, "findings": pending}, None
    if attempts:
        last = attempts[-1]
        line = SNAPSHOT_LINE.search(last["prompt"])
        base = load_snapshot(reviews_folder(now["report"]), Path(line[1]).stem) if line else None
        try:
            scoped = plan_scoped(now, base, last, pending) if base is not None else None
        except Exception as error:
            log(f"the latest review can't be scoped on, planning a full review: {type(error).__name__}: {error}")
            scoped = None
        plan = scoped or plan
    snap = dict(now, plan=plan)
    save_review(snap)
    return render(snap, base if plan["base"] else None), asked_ids(snap), plan


def full_review_errors(recipe, now, attempts):
    """The recipe's review of this mix must be a full review whose prompt is exactly today's full prompt for it, with
    every finding open before it, and both reviewers' sessions must pass it under the original checks."""
    entries = [r for r in recipe.get("reviews", []) if r.get("stage") == "mix" and r.get("fingerprint") == now["fingerprint"]]
    if not entries:
        return [f"--master needs a mix review of fingerprint {now['fingerprint']} (run check --review-prompt, then the reviews)"], None
    by = {entry.get("agent"): entry.get("session") for entry in entries[-1].get("by", [])}
    index = next((i for i, a in enumerate(attempts) if any(s in a["sessions"] for s in by.values())), None)
    before = open_findings(attempts[:index] if index is not None else attempts)
    snap = dict(now, plan={"base": None, "round": 0, "findings": before})
    text = render(snap, None)
    errors = []
    for agent in AGENTS:
        label = f"mix review by {agent}"
        session = by.get(agent)
        if not session or not UUID.match(session):
            errors.append(f"{label}: needs a full session uuid")
            continue
        if session == os.environ.get("CLAUDE_CODE_SESSION_ID"):
            errors.append(f"{label}: the reviewing session cannot be the mixing session")
            continue
        try:
            found = (claude_messages if agent == "claude" else codex_messages)(session)
        except (OSError, ValueError, AttributeError, TypeError) as error:
            errors.append(f"{label}: its transcript can't be read: {type(error).__name__}")
            continue
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
        if canonical(unwrap(users[0])) != text:
            errors.append(f"{label}: its first message is not this mix's full review prompt (a scoped review never leads to --master)")
            continue
        verdict, _ = verdict_of(assistants)
        if verdict is None:
            errors.append(f"{label}: its last message has no json verdict block")
            continue
        if verdict.get("verdict") != "pass":
            errors.append(f"{label}: verdict is {verdict.get('verdict')}")
        if verdict.get("fingerprint") != now["fingerprint"] or verdict.get("report") != report_digest(now["numbers"]):
            errors.append(f"{label}: its verdict is for another mix or report")
        if missing := set(asked_ids(snap)) - answered(verdict):
            errors.append(f"{label}: unanswered {sorted(missing)}")
        if verdict.get("findings") not in (None, []):
            errors.append(f"{label}: a pass lists no findings, got {shown(verdict.get('findings'))}")
        if unfixed := sorted(k for k in before if status_of(verdict, k) != "fixed"):
            errors.append(f"{label}: findings not given status fixed: {unfixed}")
    return errors, index


def master_errors(recipe, now):
    """--master: the latest review of this mix is a full review that passed, and no finding is open."""
    attempts = readable_history(now["report"])
    if attempts is None:
        return ["the review history can't be read"]
    errors, index = full_review_errors(recipe, now, attempts)
    if index is not None and (later := [a["id"] for a in attempts[index + 1 :] if f"\nMix fingerprint: {now['fingerprint']}\n" in a["prompt"]]):
        errors.append(f"a later review of this mix comes after the full review: {later}")
    errors += [f"open finding {key}: {text}" for key, text in open_findings(attempts).items()]
    return errors


def review_sessions(report):
    """(agent, session, start time) of each session in this repo whose first message is a review prompt for `report`."""
    wanted = f"Report: {report} ("
    out = []
    for path in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        head = prompt_head(path)
        if head and MARKER in head[0] and wanted in head[0]:
            out.append(("claude", path.stem, head[1]))
    for path in CODEX_SESSIONS.glob("**/rollout-*.jsonl"):
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
        head = prompt_head(path)
        if match and head and MARKER in head[0] and wanted in head[0]:
            out.append(("codex", match[1], head[1]))
    return out


HEAD_LINES = 40


def prompt_head(path):
    """The first line that holds the review marker, and the session's first timestamp; a cheap filter before the
    transcript is read in full."""
    try:
        with open(path, errors="replace") as file:
            started = None
            for number, line in enumerate(file):
                if number >= HEAD_LINES:
                    return None
                if started is None:
                    started = (re.search(r'"timestamp":\s*"([^"]+)"', line) or [None, None])[1]
                if MARKER in line:
                    return line, started or ""
    except OSError:
        return None
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


def entries(path, strict):
    """A transcript's json lines; when not strict, a line cut off or damaged (a reviewer mid-write) is skipped."""
    out = []
    for line in path.read_text(errors="strict" if strict else "replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            if strict:
                raise
    return out


def claude_messages(session, strict=True):
    """Its user and assistant texts. Strict: every entry is this session in the repo. Not strict (the history), only
    entries up to the first user message must be, so a reviewer that ran cd still shows its verdict and findings."""
    paths = list(CLAUDE_PROJECTS.glob(f"*/{session}.jsonl"))
    if len(paths) != 1:
        return None
    users, assistants = [], []
    for entry in entries(paths[0], strict):
        if not isinstance(entry, dict):
            continue
        if entry.get("sessionId", session) != session or entry.get("cwd", str(REPO)) != str(REPO) and (strict or not users):
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


def codex_messages(session, strict=True):
    paths = list(CODEX_SESSIONS.glob(f"**/rollout-*-{session}.jsonl"))
    if len(paths) != 1:
        return None
    users, assistants, turns = [], [], 0
    for entry in entries(paths[0], strict):
        if not isinstance(entry, dict) or not isinstance(entry.get("payload", {}), dict):
            continue
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
