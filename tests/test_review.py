import json
import uuid

import pytest

import evidence
import review
import structure
from test_workers import band

REPO = str(review.REPO)
ENVIRONMENT = f"<environment_context>\n  <cwd>{REPO}</cwd>\n  <shell>zsh</shell>\n</environment_context>"
OTHERS = 9


def contract(move, source, value, state="review", test=None):
    leaf = {"id": "n1", "state": state, "value": value, "text": "is it audible?"} | ({"test": test} if test else {})
    return {"move": move, "source": source, "decision": "kept", "outcome": "kept", "fails": [], "leaves": [leaf]}


def song_snapshot(tmp_path, gate=-40, others=0.0, stems="stems-a", notes=(), balance=0):
    """A kick gate and nine bass moves, each with one review question; `others` changes every bass move."""
    sources = [f"b{i}" for i in range(OTHERS)]
    mix = {"stems": {"kick.wav": {"gain_db": 0}},
           "steps": [{"id": "gate-kick", "target": "kick.wav", "plugin": "NoiseGate", "settings": {"threshold_db": gate}}]
           + [{"id": f"comp-{s}", "target": "kick.wav", "plugin": "Compressor", "settings": {"ratio": 4 + others}} for s in sources],
           "moves": [{"move": "kick.gate", "source": "kick", "decision": "kept", "steps": ["gate-kick"]}]
           + [{"move": "bass.compress", "source": s, "decision": "kept", "steps": [f"comp-{s}"]} for s in sources]
           + [{"move": "balance", "steps": [], "fields": []}] * balance}
    numbers = {"current": {"mix": {"integrated_lufs": -10.0 + gate / 100}}, "records": [], "questions": {},
               "contracts": [contract("kick.gate", "kick", gate / 10)] + [contract("bass.compress", s, 2.0 + others) for s in sources]}
    files = {".agents/skills/check/evidence.py": "e1", "docs/mixing-chains/kick.md": "k1", "docs/mixing-chains/README.md": "r1"}
    return review.snapshot(mix, ["code", stems, "plugins", None], files, tmp_path / "check" / "report.md", numbers, list(notes))


def reply(snap, asked, verdict="pass", findings=(), prose="Measured.", answers=None, **block):
    found = {"verdict": verdict, "fingerprint": snap["fingerprint"], "report": review.report_digest(snap["numbers"]),
             "answers": {key: "measured, fine" for key in asked} | (answers or {}), "findings": findings if findings is None else list(findings)} | block
    return f"{prose}\n```json\n{json.dumps(found)}\n```"


class Reviews:
    """Writes reviewer transcripts as Claude Code and Codex store them, each later in time than the last."""

    def __init__(self, tmp_path):
        self.tmp, self.clock, self.recipe = tmp_path, 0, {"reviews": []}

    def time(self):
        self.clock += 1
        return f"2026-11-13T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z"

    def claude(self, entries):
        session, stamp = str(uuid.uuid4()), self.time()
        lines = [{"type": kind, "sessionId": session, "cwd": REPO, "timestamp": stamp, "message": {"content": content}}
                 for kind, content in entries]
        path = self.tmp / "claude" / "project" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(line) for line in lines))
        return session

    def codex(self, prompt, answer, cwd=REPO, environment=ENVIRONMENT, turns=1):
        session, stamp = str(uuid.uuid4()), self.time()
        message = lambda role, kind, text: {"type": "response_item", "timestamp": stamp, "payload": {
            "type": "message", "role": role, "content": [{"type": kind, "text": text}]}}
        lines = [{"type": "session_meta", "timestamp": stamp, "payload": {"id": session, "cwd": cwd}}] + [{"type": "turn_context"}] * turns \
            + [message("user", "input_text", environment), message("user", "input_text", prompt), message("assistant", "output_text", answer)]
        path = self.tmp / "codex" / "2026" / f"rollout-1-{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(line) for line in lines))
        return session

    def record(self, snap, claude, codex):
        self.recipe["reviews"].append({"stage": "mix", "fingerprint": snap["fingerprint"],
                                       "by": [{"agent": "claude", "session": claude}, {"agent": "codex", "session": codex}]})

    def review(self, snap, codex=None, **kwargs):
        """Plans the next review of `snap`, answers it in both sessions, and records the entry. Returns the prompt and plan."""
        text, asked, plan = review.plan_review(snap)
        answer = reply(snap, asked, **kwargs)
        self.record(snap, self.claude([user(text), said(answer)]), self.codex(text, codex or answer))
        return text, plan


def user(text):
    return ("user", text)


def said(text):
    return ("assistant", [{"type": "text", "text": text}])


@pytest.fixture
def reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "CLAUDE_PROJECTS", tmp_path / "claude")
    monkeypatch.setattr(review, "CODEX_SESSIONS", tmp_path / "codex")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    (tmp_path / "check").mkdir()
    return Reviews(tmp_path)


def test_a_full_review_that_passes_lets_the_mix_master(tmp_path, reviews):
    snap = song_snapshot(tmp_path)

    text, plan = reviews.review(snap)

    assert plan == {"base": None, "round": 0, "findings": {}} and "- kick.gate@kick:n1: kick.gate on kick" in text
    assert review.master_errors(reviews.recipe, snap) == []


def test_a_scoped_review_asks_what_changed_and_seeded_spot_checks(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    now = song_snapshot(tmp_path, gate=-30)

    text, plan = reviews.review(now)

    assert plan["round"] == 1 and plan["moves"] == ["kick.gate@kick"] and plan["questions"] == ["kick.gate@kick:n1"]
    assert len(plan["spot"]) == review.SPOT_CHECKS and "kick.gate@kick:n1" not in plan["spot"]
    assert "1 of 10 records changed, 6 of 10 questions asked (5 spot checks), 0 findings open." in text
    assert "- move:kick.gate@kick (settings, check changed)" in text and "  - n1: review -4.0 -> review -3.0" in text
    assert review.master_errors(reviews.recipe, now) == []


def refingerprinted(snap, change):
    change(snap)
    snap["fingerprint"] = review.fingerprint(snap["mix"], snap["inputs"])
    return snap


@pytest.mark.parametrize("change", [
    lambda snap: snap["inputs"].__setitem__(1, "other stems"),
    lambda snap: (snap["files"].__setitem__("docs/mixing-chains/README.md", "r2"), snap["inputs"].__setitem__(0, "code2")),
    lambda snap: snap["inputs"].__setitem__(0, "other code"),
], ids=["stems", "chain-readme", "code-not-named-file-by-file"])
def test_some_changes_need_a_full_review(tmp_path, reviews, change):
    reviews.review(song_snapshot(tmp_path))

    assert reviews.review(refingerprinted(song_snapshot(tmp_path, gate=-30), change))[1]["base"] is None


def test_over_40_percent_of_judged_records_needs_a_full_review_whatever_the_balance_padding(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path, balance=20))

    assert reviews.review(song_snapshot(tmp_path, others=1.0, balance=20))[1]["base"] is None


def test_the_fourth_scoped_round_in_a_row_is_full_and_losing_a_snapshot_doesnt_reset_it(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    assert [reviews.review(song_snapshot(tmp_path, gate=gate))[1]["round"] for gate in (-39, -38, -37)] == [1, 2, 3]
    assert reviews.review(song_snapshot(tmp_path, gate=-36))[1]["base"] is None
    reviews.review(song_snapshot(tmp_path, gate=-35))
    reviews.review(song_snapshot(tmp_path, gate=-34))
    for path in (tmp_path / "check" / "reviews").glob("*.json"):
        if json.loads(path.read_text())["plan"]["round"] == 2:
            path.unlink()

    assert reviews.review(song_snapshot(tmp_path, gate=-33))[1]["base"] is None


def test_the_recipe_reviews_list_never_picks_the_base(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    reviews.review(song_snapshot(tmp_path, gate=-39), verdict="fail", findings=["bass b8 attack loses 8 dB"])
    reviews.recipe["reviews"].reverse()
    reviews.recipe["reviews"].pop()

    text, plan = reviews.review(song_snapshot(tmp_path, gate=-38))

    assert plan["round"] == 2 and "claude: bass b8 attack loses 8 dB" in text and "codex: bass b8 attack loses 8 dB" in text


def test_a_failed_reviews_findings_stay_open_until_both_reviewers_answer_fixed(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    finding = "kick gate eats 3 soft hits\n(at 12.1 s, 14.0 s, 20.5 s: -9 dB)"
    reviews.review(song_snapshot(tmp_path, gate=-39), verdict="fail", findings=[finding])
    first = song_snapshot(tmp_path, gate=-30)

    text, plan = reviews.review(first)

    key = review.finding_id(f"claude: {finding}")
    assert f"- {key}: claude: kick gate eats 3 soft hits\n  (at 12.1 s" in text
    # "measured, fine" is not "fixed": both findings stay open, and the mix can't master.
    assert len(review.replay(first["report"]).pending) == 2
    assert any(e.startswith("open finding") for e in review.master_errors(reviews.recipe, first))
    now = song_snapshot(tmp_path, gate=-29)
    reviews.review(now, answers={k: "fixed: all hits within 0.5 dB" for k in plan["findings"]})
    assert review.master_errors(reviews.recipe, now) == []


def test_one_reviewer_saying_fixed_is_not_enough(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path), verdict="fail", findings=["tail cut by 6 dB"])
    now = song_snapshot(tmp_path, gate=-30)
    text, asked, plan = review.plan_review(now)
    reviews.claude([user(text), said(reply(now, asked, answers={k: "fixed: tail at -0.3 dB" for k in plan["findings"]}))])
    reviews.codex(text, reply(now, asked, answers={k: "not fixed: still -6 dB" for k in plan["findings"]}))

    assert len(review.replay(now["report"]).pending) == 2


def test_a_full_review_fallback_keeps_open_findings(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    reviews.review(song_snapshot(tmp_path, gate=-39), verdict="fail", findings=["UNFIXED: kick gate eats soft hits, -9 dB"])
    now = song_snapshot(tmp_path, gate=-39, others=1.0)

    text, plan = reviews.review(now)

    assert plan["base"] is None and len(plan["findings"]) == 2 and "claude: UNFIXED: kick gate eats soft hits, -9 dB" in text
    assert any(error.startswith("open finding") for error in review.master_errors(reviews.recipe, now))


def test_a_retry_of_the_same_mix_carries_its_failed_findings(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    same = song_snapshot(tmp_path, gate=-39)
    reviews.review(same, verdict="fail", findings=["kick still loses 3 hits"])

    text, plan = reviews.review(same)

    assert plan["round"] == 2 and "claude: kick still loses 3 hits" in text
    assert any(error.startswith("open finding") for error in review.master_errors(reviews.recipe, same))


def test_a_pass_that_lists_findings_is_rejected_and_carried(tmp_path, reviews):
    snap = song_snapshot(tmp_path)
    reviews.review(snap, findings=["Kick still loses 3 hits at -9 dB"])

    errors = review.master_errors(reviews.recipe, snap)

    assert any("a pass verdict cannot list findings" in error for error in errors)
    assert any("claude: Kick still loses 3 hits at -9 dB" in error for error in errors)


@pytest.mark.parametrize("kwargs, carried, full", [
    ({"prose": "The bass is masked, -4 dB at 300 Hz."}, "claude: The bass is masked, -4 dB at 300 Hz.", False),
    ({"prose": ""}, 'claude: failed with no reason given (verdict "fail")', True),
    ({"prose": "", "findings": None}, 'claude: failed with no reason given (verdict "fail")', True),
], ids=["prose", "nothing", "null-findings"])
def test_a_fail_without_findings_carries_its_message(tmp_path, reviews, kwargs, carried, full):
    reviews.review(song_snapshot(tmp_path), verdict="fail", **kwargs)

    text, plan = reviews.review(song_snapshot(tmp_path, gate=-30))

    assert carried in text and (plan["base"] is None) == full


def test_findings_that_are_not_a_list_are_carried_whole_and_rejected(tmp_path, reviews):
    snap = song_snapshot(tmp_path)
    text, asked, _ = review.plan_review(snap)
    reviews.claude([user(text), said(reply(snap, asked, verdict="fail").replace('"findings": []', '"findings": "tail cut by 6 dB"'))])
    reviews.codex(text, reply(snap, asked))

    state = review.replay(snap["report"])

    assert list(state.pending.values()) == ["claude: tail cut by 6 dB"]
    assert any("its findings are not a list of strings" in error for error in state.last["errors"])


def test_an_override_is_asked_again_when_its_numbers_move(tmp_path, reviews):
    def with_override(value):
        snap = song_snapshot(tmp_path)
        entry = snap["numbers"]["contracts"][1]
        entry["leaves"].append({"id": "n2", "state": "false", "value": value, "test": "<= 3", "text": "ratio holds"})
        entry["override"] = {"reason": "bleed", "leaves": ["n2"]}
        return snap

    reviews.review(with_override(3.5))
    plan = reviews.review(with_override(12.0))[1]

    assert "bass.compress@b0:override" in plan["questions"] and plan["moves"] == ["bass.compress@b0"]


def test_an_edited_or_incomplete_snapshot_forces_a_full_review(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    [path] = (tmp_path / "check" / "reviews").glob("*.json")
    snap = json.loads(path.read_text())
    snap["files"]["docs/mixing-chains/README.md"] = "r2"
    path.write_text(json.dumps(snap))

    assert reviews.review(song_snapshot(tmp_path, gate=-30))[1]["base"] is None
    del snap["files"]
    (path.parent / f"{review.digest(snap)}.json").write_text(json.dumps(snap))
    assert review.load_snapshot(path.parent, review.digest(snap)) is None


def test_revisiting_a_mix_keeps_every_snapshot(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    reviews.review(song_snapshot(tmp_path, gate=-30))
    before = {p.name: p.read_bytes() for p in (tmp_path / "check" / "reviews").iterdir()}

    plan = reviews.review(song_snapshot(tmp_path))[1]

    assert plan["round"] == 2
    assert all((tmp_path / "check" / "reviews" / name).read_bytes() == data for name, data in before.items())
    assert review.master_errors(reviews.recipe, song_snapshot(tmp_path)) == []


def test_spot_checks_follow_the_last_reviewers_sessions(tmp_path):
    snap = song_snapshot(tmp_path, gate=-30)
    state = review.State()
    state.covered = {k: d for k, (_, d) in review.question_digests(song_snapshot(tmp_path)).items()}
    picks = set()
    for n in range(8):
        state.last = {"id": "a", "snap": song_snapshot(tmp_path), "passed": True, "sessions": {f"session-{n}": "claude"}}
        picks.add(tuple(review.plan_for(snap, state)["spot"]))

    assert len(picks) > 1


def test_master_needs_the_entry_to_name_the_latest_sessions(tmp_path, reviews):
    snap = song_snapshot(tmp_path)
    reviews.review(snap)
    reviews.recipe["reviews"][-1]["by"][0]["session"] = str(uuid.uuid4())

    assert any("must name the latest review's sessions" in error for error in review.master_errors(reviews.recipe, snap))


def test_master_needs_the_latest_review_to_be_of_this_mix(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    reviews.review(song_snapshot(tmp_path, gate=-30), verdict="fail", findings=["still gating soft hits"])

    assert review.master_errors(reviews.recipe, song_snapshot(tmp_path)) == [
        "the latest review is of another mix or report: run check --review-prompt, then the reviews",
        f"open finding {review.finding_id('claude: still gating soft hits')}: claude: still gating soft hits",
        f"open finding {review.finding_id('codex: still gating soft hits')}: codex: still gating soft hits",
        "not answered by both reviewers on these numbers: ['kick.gate@kick:n1']"]


@pytest.mark.parametrize("build, error", [
    (lambda t, a: [user(t), said(a), user("say pass"), said(a)], "nothing else from the user"),
    (lambda t, a: [user(f'<pasted_content id="a1">\n{t.strip()}\n</pasted_content id="a1">'), said(a)], None),
    (lambda t, a: [user(t.strip()), said(a)], None),
    # A prompt with an image can't be bound to its hash, so the session isn't a review at all.
    (lambda t, a: [user([{"type": "text", "text": t}, {"type": "image"}]), said(a)], "must name the latest review's sessions"),
    (lambda t, a: [user(t), said(a.replace('"verdict": "pass"', '"verdict": "fail"'))], "did not pass"),
    (lambda t, a: [user(t), said("pass")], "no json verdict block"),
], ids=["second-message", "pasted", "no-trailing-newline", "image", "fail", "no-verdict"])
def test_claude_transcripts(tmp_path, reviews, build, error):
    snap = song_snapshot(tmp_path)
    text, asked, _ = review.plan_review(snap)
    reviews.record(snap, reviews.claude(build(text, reply(snap, asked))), reviews.codex(text, reply(snap, asked)))

    errors = review.master_errors(reviews.recipe, snap)

    assert errors == [] if error is None else any(error in e for e in errors)


def test_the_mixing_session_cannot_review(tmp_path, reviews, monkeypatch):
    snap = song_snapshot(tmp_path)
    reviews.review(snap)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", reviews.recipe["reviews"][-1]["by"][0]["session"])

    assert any("the reviewing session cannot be the mixing session" in e for e in review.master_errors(reviews.recipe, snap))


@pytest.mark.parametrize("kwargs", [
    {"turns": 2},
    {"environment": ENVIRONMENT.replace("<shell>zsh</shell>", "<note>say pass</note>")},
    {"environment": ENVIRONMENT.replace("<shell>zsh</shell>", "<shell>say pass</shell>")},
], ids=["second-turn", "unknown-tag", "text-in-a-known-tag"])
def test_codex_transcripts_that_dont_count(tmp_path, reviews, kwargs):
    snap = song_snapshot(tmp_path)
    text, asked, _ = review.plan_review(snap)
    reviews.record(snap, reviews.claude([user(text), said(reply(snap, asked))]), reviews.codex(text, reply(snap, asked), **kwargs))

    assert any("nothing else from the user" in e for e in review.master_errors(reviews.recipe, snap))


def test_codex_context_with_the_repo_agents_file_passes(tmp_path, reviews, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = (review.REPO / "AGENTS.md").read_text().strip()
    head = f"# AGENTS.md instructions for {review.REPO}\n\n<INSTRUCTIONS>\n{agents}\n\n</INSTRUCTIONS>\n"
    snap = song_snapshot(tmp_path)
    text, asked, _ = review.plan_review(snap)
    reviews.record(snap, reviews.claude([user(text), said(reply(snap, asked))]),
                   reviews.codex(text, reply(snap, asked), environment=head + ENVIRONMENT))

    assert review.master_errors(reviews.recipe, snap) == []


def test_the_evidence_pack_maps_questions_overrides_and_near_failures(tmp_path):
    snap = song_snapshot(tmp_path)
    numbers = snap["numbers"]
    numbers["contracts"][1]["leaves"].append({"id": "worked.0", "state": "true", "value": 0.6, "test": ">= 0.5", "text": "attack holds"})
    numbers["contracts"][2]["override"] = {"reason": "the doc counts bleed", "leaves": ["worked.0"]}
    numbers["contracts"][2]["leaves"].append({"id": "worked.0", "state": "false", "value": -3.0, "test": ">= 0.5", "text": "decay holds"})

    pack = review.evidence_pack(numbers, snap["fingerprint"])

    assert "- bass.compress@b1:override: bass.compress on b1: override 'the doc counts bleed' against ['worked.0']" in pack
    assert "  - against worked.0 decay holds: false, -3.0 (>= 0.5)" in pack
    assert "  - near: worked.0 attack holds: true, 0.6 (>= 0.5)" in pack
    assert [line for line in pack.split("near-failing records")[1].split("## ")[0].splitlines() if line.startswith("- ")] == [
        "- bass.compress@b0: decision kept, default outcome kept"]


def test_a_scoped_prompt_on_a_tiny_song(tmp_path, reviews, monkeypatch):
    monkeypatch.setenv("STEMMASTER_NODE_MEMO_DIR", str(tmp_path / "memo"))
    song = tmp_path / "song"
    mix = band(song, 2)

    def snap(mix):
        nodes = structure.nodes(mix, structure.Routing(mix), [])
        path, numbers = evidence.report(json.loads(json.dumps(mix)), song, nodes)
        return review.snapshot(json.loads(json.dumps(mix)), evidence.inputs(song, mix), {}, path, numbers, [])

    reviews.review(snap(mix))
    next(s for s in mix["steps"] if s["id"] == "gtr-hpf")["settings"]["cutoff_frequency_hz"] = 120
    now = snap(mix)
    text, plan = reviews.review(now)

    assert plan["round"] == 1 and "- move:guitars.filters@guitars (settings, check changed)" in text
    assert "- move:kick.compress@kick (" not in text and '- steps.gtr-hpf: {"id": "gtr-hpf"' in text
    packs = list((song / "renders" / "check" / "reviews").glob("pack-*.md"))
    assert packs and all(len(p.read_bytes()) < 15000 for p in packs)
    assert review.master_errors(reviews.recipe, now) == []


def test_deleting_a_failed_reviews_snapshot_forces_a_full_review_that_still_carries_its_findings(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    reviews.review(song_snapshot(tmp_path, gate=-39), verdict="fail", findings=["snare tail cut by 6 dB"])
    for path in (tmp_path / "check" / "reviews").glob("*.json"):
        if json.loads(path.read_text())["plan"]["base"]:
            path.unlink()

    text, plan = reviews.review(song_snapshot(tmp_path, gate=-38))

    assert plan["base"] is None and "claude: snare tail cut by 6 dB" in text and "codex: snare tail cut by 6 dB" in text


def test_a_slow_reviewers_findings_after_the_next_prompt_make_that_prompt_not_count(tmp_path, reviews):
    reviews.review(song_snapshot(tmp_path))
    late = song_snapshot(tmp_path, gate=-39)
    text, asked, _ = review.plan_review(late)
    reviews.claude([user(text), said(reply(late, asked))])
    now = song_snapshot(tmp_path, gate=-38)
    next_text, next_asked, _ = review.plan_review(now)
    codex = reviews.codex(text, reply(late, asked, verdict="fail", findings=["bass masks the kick by 3 dB"]))
    reviews.record(now, reviews.claude([user(next_text), said(reply(now, next_asked))]), reviews.codex(next_text, reply(now, next_asked)))

    errors = review.master_errors(reviews.recipe, now)

    assert codex and any("its prompt was not planned from the review history" in e for e in errors)
    assert any("codex: bass masks the kick by 3 dB" in e for e in errors)
