"""A12 compaction quality: an offline, LLM-in-the-loop evaluation (``docs/plans/avibe-agent-core.md`` section 6).

It drives a real Vibey Session through the instance's own ``vibe agent run``, so every model call goes through that
instance's Model Hub and every checkpoint through the shipped C-9 code. A scripted session first plants the facts: it
states the user's ground rules, has the agent change two files for stated reasons, run a check that fails twice, and
agree on a next step. Large log reads then push the context over the threshold until at least two checkpoints have
summarized every planted fact. A probe Turn asks, from memory, for the ground rules, the files changed and why, the
last error verbatim, and the next step, as one JSON object. Each field is scored against the facts the script planted.

A run counts only if ground truth says it happened as scripted: every Agent run succeeded, the fixture on disk ends in
the scripted state, both check outputs are in the Session's tool results in order, at least two checkpoints before the
probe summarized the context past the last planted fact, the probe called no tool, and every model-written checkpoint
used the checkout's prompt version. An invalid run is reported with its reasons, not scored, and exits with status 3.

Not a test and not collected by pytest (no ``test_`` prefix); never run in CI. It makes real model calls and writes
real Sessions, so it runs only inside a regression instance (``VIBE_DEPLOYMENT_ENV=regression``), for example through
``python3 scripts/incus_regression.py shell --target worktree --slug <slug>``:

    cd /opt/avibe/source && /opt/avibe/venv/bin/python tests/evals/a12_compaction_quality.py --model <vibey-model>

``--model`` names a Vibey model whose Model Hub definition has a window of at most 32,000 tokens, so two checkpoints
happen within the fixture's reads. Results go to stdout as JSON; record them in ``tests/evals/a12_baselines.md`` per
prompt version. See ``tests/evals/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# Run as a file from the source checkout: the product's modules are imported from it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: The largest window the fixture is sized and tested for: two checkpoints past the planted facts within its reads.
#: Larger windows need more or larger reads than the fixture holds.
MAX_WINDOW = 32_000
#: Each log read is this share of the window, under the read tool's own cap.
LOG_SHARE = 0.35
LOG_TOKEN_CAP = 10_000
#: Log reads, one per Turn, allowed until two checkpoints have summarized every planted fact.
LOG_READS = 7
TURN_TIMEOUT_S = 900

RULES = (
    "We are hardening the token-bucket rate limiter in this project. Ground rules for this whole session: never "
    "modify vendor/legacy_api.py; do not rename any public function; record every change you make as one line in "
    "CHANGES.md. First read src/rate_limit.py and src/config.py, then reply with one line."
)
#: The Turns that plant every scored fact. The log reads come only after them, so each checkpoint that counts has all
#: of them to carry.
TURNS = (
    RULES,
    "Change BURST in src/rate_limit.py from 10 to 5, because the upstream API now rejects bursts above 5. Add the "
    "CHANGES.md line. Then run python3 check.py and show me its output.",
    "Set REFILL_RATE in src/config.py to 1, because a refill rate of 0 means the bucket never refills. Add the "
    "CHANGES.md line, run python3 check.py again, and show me its output.",
    "The next step will be to add a regression test in tests/test_burst.py that covers a burst of exactly 5. Do not "
    "write it yet; just confirm the plan in one line.",
)
READ_TURN = "Read logs/day{index}.log completely and summarize it in one line."
PROBE = (
    "Answer from memory only: do not read any file or run any command. Reply with one JSON object and nothing else, "
    'in this shape: {"rules": [{"action": "...", "kind": "must"}], "changes": [{"file": "...", "change": "...", '
    '"why": "..."}], "last_error": "...", "next_step": "..."}. rules: every ground rule I set for this session, one '
    'object each; action is the action the rule is about, written positively (for example "delete the cache"), and '
    'kind is "must_not" if I forbade that action or "must" if I required it. changes: each file you changed, what '
    "you changed in it, and why. last_error: the last error check.py printed, verbatim. next_step: the next step we "
    "agreed on."
)
FIRST_ERROR = "ValueError: refill_rate must be > 0 (got 0)"
LAST_ERROR = "AssertionError: burst window overlaps (bucket=5, window=0.5s)"
VENDOR = "# Vendored client for the legacy upstream API. Do not edit.\n\ndef call(path):\n    return path\n"


def _text(value: object) -> str:
    return value.lower() if isinstance(value, str) else ""


def _change(answer: dict, file: str) -> dict:
    """The answer's entry for ``file`` (its path ends with it), or an empty one."""
    changes = answer.get("changes")
    entries = [item for item in changes if isinstance(item, dict)] if isinstance(changes, list) else []
    return next((item for item in entries if _text(item.get("file")).endswith(file)), {})


def _rule(answer: dict, kind: str, subjects: Sequence[str], verbs: Sequence[str] = ("",)) -> bool:
    """Whether a rule of ``kind`` names every subject and one of the verbs in its action: polarity comes from ``kind``,
    by construction, and the verbs keep a rule about the same file but another action from counting."""
    rules = answer.get("rules")
    entries = [item for item in rules if isinstance(item, dict)] if isinstance(rules, list) else []
    return any(
        _text(item.get("kind")).strip() == kind
        and all(subject in _text(item.get("action")) for subject in subjects)
        and any(verb in _text(item.get("action")) for verb in verbs)
        for item in entries
    )


def _set_to(text: str, value: int) -> bool:
    """Whether ``text`` gives ``value`` as the setting's new value ("to 5", "= 5", "→ 5"), not merely mentions it. A
    sentence's closing period may follow it; a decimal part may not."""
    return re.search(rf"(?:\bto|=|→|->)\s*{value}(?!\.?\d)", text) is not None


def _refill_reason(answer: dict) -> bool:
    """Whether config.py's reason is about refilling and says why: a zero rate never refills, or 1 enables refilling."""
    why = _text(_change(answer, "config.py").get("why"))
    return "refill" in why and re.search(r"never|zero|enabl|(?<![\d.])0(?!\.?\d)", why) is not None


def _next_step(answer: dict) -> bool:
    """Whether the next step is still to add a test in tests/test_burst.py for a burst of exactly 5: the action and the
    boundary as well as the file, so a remembered filename with a lost or different action fails (polarity is not
    checked; see the README's known limits)."""
    step = _text(answer.get("next_step"))
    return (
        "test_burst.py" in step
        and any(verb in step for verb in ("add", "write", "creat", "implement"))
        and re.search(r"(?<![\d.])5(?!\.?\d)", step) is not None
    )


@dataclass(frozen=True)
class Fact:
    """One planted fact, judged on its own field of the probe's JSON answer."""

    name: str
    found: Callable[[dict], bool]


#: What A12 asks the agent to still state: the user's constraints, the files changed and why, the last error, and
#: the next step. A rule counts only with its polarity, a value only inside its own file's entry, a reason only in that
#: entry's ``why``.
FACTS = (
    Fact(
        "rule: vendor/legacy_api.py must not be modified",
        lambda a: _rule(a, "must_not", ("vendor/legacy_api.py",), ("modif", "edit", "change", "touch", "alter")),
    ),
    Fact("rule: public functions must not be renamed", lambda a: _rule(a, "must_not", ("rename", "public", "function"))),
    Fact(
        "rule: every change must be noted in CHANGES.md",
        lambda a: _rule(a, "must", ("changes.md",), ("record", "note", "log", "document", "add", "write")),
    ),
    Fact(
        "changed: src/rate_limit.py, BURST to 5",
        lambda a: "burst" in _text(_change(a, "rate_limit.py").get("change"))
        and _set_to(_text(_change(a, "rate_limit.py").get("change")), 5),
    ),
    Fact("why: upstream rejects bursts above 5", lambda a: "upstream" in _text(_change(a, "rate_limit.py").get("why"))),
    Fact(
        "changed: src/config.py, REFILL_RATE to 1",
        lambda a: "refill" in _text(_change(a, "config.py").get("change"))
        and _set_to(_text(_change(a, "config.py").get("change")), 1),
    ),
    Fact("why: a zero refill rate never refills", _refill_reason),
    Fact("changed: CHANGES.md", lambda a: bool(_change(a, "changes.md"))),
    Fact("last error, verbatim", lambda a: _text(a.get("last_error")).strip(" `'\"") == LAST_ERROR.lower()),
    Fact("next step: add a test in tests/test_burst.py for a burst of exactly 5", _next_step),
)


def parse_answer(text: str) -> Optional[dict]:
    """The probe's JSON object: the outermost braces of the reply, fences and narration around them ignored."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


CHECK_PY = f'''import re
import sys

config = open("src/config.py").read()
limiter = open("src/rate_limit.py").read()
refill = int(re.search(r"REFILL_RATE = (-?\\d+)", config).group(1))
burst = int(re.search(r"BURST = (\\d+)", limiter).group(1))
if refill <= 0:
    print(f"ValueError: refill_rate must be > 0 (got {{refill}})")
    sys.exit(1)
if burst != 5:
    print(f"AssertionError: burst must be 5 for the upstream API (got {{burst}})")
    sys.exit(1)
print("{LAST_ERROR}")
sys.exit(1)
'''


def _log(index: int, tokens: int) -> str:
    """Deterministic, varied log lines of about ``tokens`` tokens (UTF-8 bytes / 4)."""
    rng = random.Random(index)
    levels = ("INFO", "INFO", "INFO", "WARN", "DEBUG")
    events = (
        "token bucket refilled",
        "request admitted",
        "request throttled: bucket empty",
        "upstream latency high",
        "burst of requests from client",
        "retry scheduled after 429",
    )
    lines, size = [], 0
    second = 0
    while size < tokens * 4:
        second += rng.randint(1, 7)
        line = (
            f"2026-10-0{index} {second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d} "
            f"{rng.choice(levels)} limiter[{rng.randint(100, 999)}] {rng.choice(events)} "
            f"client=c{rng.randint(1, 40):02d} tokens={rng.randint(0, 10)}"
        )
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) + "\n"


def write_fixture(root: Path, window: int) -> None:
    log_tokens = min(LOG_TOKEN_CAP, int(window * LOG_SHARE))
    files = {
        "src/rate_limit.py": (
            '"""Token-bucket rate limiter."""\n\nfrom src.config import REFILL_RATE\n\nBURST = 10\n\n\n'
            "def allow(bucket: dict, now: float) -> bool:\n"
            '    elapsed = now - bucket["at"]\n'
            '    bucket["tokens"] = min(BURST, bucket["tokens"] + elapsed * REFILL_RATE)\n'
            '    bucket["at"] = now\n'
            '    if bucket["tokens"] < 1:\n        return False\n'
            '    bucket["tokens"] -= 1\n    return True\n'
        ),
        "src/config.py": "REFILL_RATE = 0\n",
        "vendor/legacy_api.py": VENDOR,
        "CHANGES.md": "# Changes\n",
        "check.py": CHECK_PY,
    }
    for index in range(1, 1 + LOG_READS):
        files[f"logs/day{index}.log"] = _log(index, log_tokens)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def vibe(command: Sequence[str], *args: str) -> dict:
    """One CLI call's JSON. A synchronous Agent run must have succeeded: one that failed, was canceled, or is still
    running when the wait ends (detached) would let the next Turn read the previous Turn's reply."""
    result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=TURN_TIMEOUT_S + 60)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"vibe {' '.join(args[:2])} printed no JSON: {result.stderr.strip()[:500]}") from error
    if not payload.get("ok", False):
        raise SystemExit(f"vibe {' '.join(args[:2])} failed: {json.dumps(payload)[:800]}")
    if "--sync" in args and (payload.get("run") or {}).get("status") != "succeeded":
        raise SystemExit(f"vibe agent run did not succeed: {json.dumps(payload.get('run'))[:800]}")
    return payload


def model_window(model: str) -> tuple[int, int]:
    from config.v2_config import V2Config

    supply = V2Config.load().model_hub.agents.get("vibey")
    definition = next((item for item in (supply.models if supply else ()) if item.id == model), None)
    if definition is None or not definition.context_window:
        raise SystemExit(f"Vibey has no model {model!r} with a context window in its Model Hub definition")
    from types import SimpleNamespace

    from modules.agents.vibey.context import output_budget

    # The output Vibey actually asks for on this route, which also sets the compaction threshold.
    return definition.context_window, output_budget(SimpleNamespace(**vars(definition)))


class Rows:
    """Read-only views of the instance's state database: the ground truth a run is judged on."""

    def __init__(self) -> None:
        from config import paths

        self.path = paths.get_sqlite_state_path()

    def _query(self, sql: str, *args: object) -> list[sqlite3.Row]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def _events(self, session_id: str, kind: str, after: int = 0) -> list[dict]:
        rows = self._query(
            "select content_json from agent_events where session_id = ? and event_type = ? and rowid > ? order by rowid",
            session_id,
            kind,
            after,
        )
        return [json.loads(row["content_json"]) for row in rows]

    def mark(self) -> int:
        """The newest event's position: what a later Turn writes comes after it."""
        return self._query("select coalesce(max(rowid), 0) as mark from agent_events")[0]["mark"]

    def compactions(self, session_id: str) -> list[dict]:
        return self._events(session_id, "context_compaction")

    def context_end(self, session_id: str) -> int:
        """The Session's newest context position: what the Turns so far committed is at or before it."""
        rows = self._query(
            "select max(seq) as seq from (select max(context_seq) as seq from messages where session_id = ? "
            "union all select max(context_seq) from agent_events where session_id = ?)",
            session_id,
            session_id,
        )
        return rows[0]["seq"] or 0

    def checkpoint_turns(self, session_id: str) -> list[dict]:
        return self._events(session_id, "fork_turn")

    def tool_results(self, session_id: str, after: int) -> int:
        """Tool results committed to the context after ``after``: durable rows, unlike the best-effort call traces."""
        rows = self._query(
            "select count(*) as count from agent_events where session_id = ? and event_type = 'tool_result' "
            "and context_seq is not null and rowid > ?",
            session_id,
            after,
        )
        return rows[0]["count"]

    def tool_outputs(self, session_id: str) -> list[str]:
        """Every tool result's text, in order."""
        outputs = []
        for payload in self._events(session_id, "tool_result"):
            blocks = (payload.get("message") or {}).get("content") or []
            outputs.append("\n".join(block.get("text") or "" for block in blocks if isinstance(block, dict)))
        return outputs

    def last_result(self, session_id: str) -> str:
        rows = self._query(
            "select content_text from messages where session_id = ? and type = 'result' order by created_at desc "
            "limit 1",
            session_id,
        )
        return rows[0]["content_text"] if rows else ""


def fixture_problems(root: Path) -> list[str]:
    """How the fixture on disk differs from the end state the scripted Turns ask for."""
    problems = []
    limiter = (root / "src/rate_limit.py").read_text(encoding="utf-8")
    if not re.search(r"^BURST\s*=\s*5\s*$", limiter, re.M):
        problems.append("src/rate_limit.py does not set BURST = 5")
    if not re.search(r"^def allow\(", limiter, re.M):
        problems.append("src/rate_limit.py no longer defines allow()")
    if not re.search(r"^REFILL_RATE\s*=\s*1\s*$", (root / "src/config.py").read_text(encoding="utf-8"), re.M):
        problems.append("src/config.py does not set REFILL_RATE = 1")
    notes = [line.lower() for line in (root / "CHANGES.md").read_text(encoding="utf-8").splitlines()[1:] if line.strip()]
    if len(notes) != 2 or not any("burst" in note for note in notes) or not any("refill" in note for note in notes):
        problems.append(f"CHANGES.md does not hold exactly the BURST and REFILL_RATE lines: {notes}")
    if (root / "vendor/legacy_api.py").read_text(encoding="utf-8") != VENDOR:
        problems.append("vendor/legacy_api.py was modified")
    return problems


def check_outputs_in_order(outputs: Sequence[str]) -> bool:
    """Whether the first check's error and then the last one appear in the Session's tool results, in that order."""
    first = next((index for index, text in enumerate(outputs) if FIRST_ERROR in text), None)
    return first is not None and any(LAST_ERROR in text for text in outputs[first + 1 :])


def run(args: argparse.Namespace) -> tuple[dict, bool]:
    window, max_output = model_window(args.model)
    if window > MAX_WINDOW:
        raise SystemExit(f"{args.model} has a {window}-token window; A12 needs one of at most {MAX_WINDOW}")
    stamp = time.strftime("%Y%m%d%H%M%S")
    if args.project_dir:
        root = Path(args.project_dir)
        if root.exists() and any(root.iterdir()):
            raise SystemExit(f"{root} is not empty; A12 writes its fixture only into a new or empty directory")
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix=f"a12-{stamp}-"))
    write_fixture(root, window)
    command = tuple(args.vibe.split())
    agent = f"a12-eval-{stamp}"
    vibe(command, "agent", "create", agent, "--backend", "vibey", "--model", args.model,
         "--description", "A12 compaction-quality evaluation (temporary)")
    rows, session_id, transcript = Rows(), None, []

    def turn(message: str) -> None:
        nonlocal session_id
        target = ("--session-id", session_id) if session_id else ("--agent", agent, "--create-session", "--cwd", str(root))
        payload = vibe(command, "agent", "run", *target, "--sync", "--no-callback",
                       "--wait-timeout", str(TURN_TIMEOUT_S), "--message", message)
        session_id = session_id or payload["session_id"]
        transcript.append({"message": message, "reply": rows.last_result(session_id)})

    def carried() -> int:
        """Checkpoints that summarized the context past the last planted fact: each had every fact to carry."""
        return sum(1 for item in rows.compactions(session_id) if (item.get("summarized_to_seq") or 0) >= facts_end)

    try:
        for message in TURNS:
            turn(message)
        facts_end = rows.context_end(session_id)
        for index in range(1, 1 + LOG_READS):
            if carried() >= 2:
                break
            turn(READ_TURN.format(index=index))
        before_probe = carried()
        problems = fixture_problems(root)
        if not check_outputs_in_order(rows.tool_outputs(session_id)):
            problems.append("the Session's tool results lack the two check outputs in order")
        if before_probe < 2:
            problems.append(
                f"{before_probe} checkpoint(s) before the probe summarized the context past the last planted fact "
                f"(position {facts_end}), not at least 2"
            )
        answer, probe_calls = "", None
        if not problems:  # an invalid run is not probed
            mark = rows.mark()
            vibe(command, "agent", "run", "--session-id", session_id, "--sync", "--no-callback",
                 "--wait-timeout", str(TURN_TIMEOUT_S), "--message", PROBE)
            answer, probe_calls = rows.last_result(session_id), rows.tool_results(session_id, mark)
            if probe_calls:
                problems.append(f"the probe ran {probe_calls} tool(s) instead of answering from memory")
    finally:
        if not args.keep:
            vibe(command, "agent", "remove", agent)

    from core.agent_core.harness.context import PROMPT_VERSION

    compactions = rows.compactions(session_id)  # after the probe: any checkpoint it made is reported too
    versions = sorted({item["summarizer"].get("prompt_version") for item in compactions if item.get("summarizer")})
    if versions != [PROMPT_VERSION]:
        problems.append(f"the runtime's checkpoints used prompt version(s) {versions}, the checkout has {PROMPT_VERSION}")
    parsed = parse_answer(answer) if not problems else None
    scores = {fact.name: parsed is not None and fact.found(parsed) for fact in FACTS} if not problems else None
    result = {
        "valid": not problems,
        "invalid_reasons": problems,
        "prompt_version": versions[0] if len(versions) == 1 else versions,
        "model": args.model,
        "window": window,
        "output_budget": max_output,
        "session_id": session_id,
        "project_dir": str(root),
        "facts_end": facts_end,
        "compactions": [
            {
                "mode": item.get("mode"),
                "summarized_to_seq": item.get("summarized_to_seq"),
                "reason": item.get("reason"),
                "tokens_before": item.get("tokens_before"),
                "tokens_after_estimate": item.get("tokens_after_estimate"),
                # A dropped row carries an earlier checkpoint without a model call: no summarizer.
                "model_written": item.get("summarizer") is not None,
            }
            for item in compactions
        ],
        "checkpoint_turns": [
            {"outcome": item.get("outcome"), "repair": item.get("repair"), "error": item.get("error")}
            for item in rows.checkpoint_turns(session_id)
        ],
        "probe_tool_calls": probe_calls,
        "answer_parsed": parsed is not None if not problems else None,
        "score": f"{sum(scores.values())}/{len(scores)}" if scores is not None else None,
        "facts": scores,
        "answer": answer,
        "turns": transcript,
    }
    return result, not problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--model", required=True, help="a Vibey model with a Model Hub window of at most 32,000 tokens")
    parser.add_argument("--project-dir", help="a new or empty directory for the fixture (default: a new temporary one)")
    beside = Path(sys.executable).with_name("vibe")  # the instance's own CLI, next to the Python running this script
    parser.add_argument(
        "--vibe", default=str(beside) if beside.exists() else "vibe", help="the vibe command of the instance under evaluation"
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary Agent")
    args = parser.parse_args(argv)
    if os.environ.get("VIBE_DEPLOYMENT_ENV") != "regression":
        print("A12 makes real model calls and writes Sessions: run it inside a regression instance.", file=sys.stderr)
        return 2
    result, valid = run(args)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if valid else 3


if __name__ == "__main__":
    sys.exit(main())
