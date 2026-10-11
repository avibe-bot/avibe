"""A12 compaction quality: an offline, LLM-in-the-loop evaluation (``docs/plans/avibe-agent-core.md`` section 6).

It drives a real Vibey Session through the instance's own ``vibe agent run``, so every model call goes through that
instance's Model Hub and every checkpoint through the shipped C-9 code. A scripted long session states the user's
ground rules, has the agent change two files for stated reasons, run a check that fails twice, and agree on a next
step, while large log reads push the context over the threshold. Once the Session holds at least two checkpoints, a
probe Turn asks, from memory, for the ground rules, the files changed and why, the last error verbatim, and the next
step. The answer is scored against the facts the script planted.

Not a test and not collected by pytest (no ``test_`` prefix); never run in CI. It makes real model calls and writes
real Sessions, so it runs only inside a regression instance (``VIBE_DEPLOYMENT_ENV=regression``), for example through
``python3 scripts/incus_regression.py shell --target worktree --slug <slug>``:

    cd /opt/avibe/source && /opt/avibe/venv/bin/python tests/evals/a12_compaction_quality.py --model <vibey-model>

``--model`` names a Vibey model whose Model Hub definition has a window of at most 64,000 tokens, so two checkpoints
happen within a bounded run. Results go to stdout as JSON; record them in ``tests/evals/a12_baselines.md`` per prompt
version. See ``tests/evals/README.md``.
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# Run as a file from the source checkout: the product's modules are imported from it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: The largest window the fixture is sized for: two checkpoints within the scripted Turns plus a few extra reads.
MAX_WINDOW = 64_000
#: Each log read is this share of the window, under the read tool's own cap.
LOG_SHARE = 0.35
LOG_TOKEN_CAP = 10_000
#: Extra log reads allowed when the scripted Turns compacted fewer than twice.
EXTRA_READS = 3
TURN_TIMEOUT_S = 900

RULES = (
    "We are hardening the token-bucket rate limiter in this project. Ground rules for this whole session: never "
    "modify vendor/legacy_api.py; do not rename any public function; record every change you make as one line in "
    "CHANGES.md. First read src/rate_limit.py and src/config.py, then reply with one line."
)
TURNS = (
    RULES,
    "Read logs/day1.log and logs/day2.log completely, then tell me in one line which warning is most common.",
    "Change BURST in src/rate_limit.py from 10 to 5, because the upstream API now rejects bursts above 5. Add the "
    "CHANGES.md line. Then run python3 check.py and show me its output.",
    "Read logs/day3.log completely and summarize it in one line.",
    "Set REFILL_RATE in src/config.py to 1, because a refill rate of 0 means the bucket never refills. Add the "
    "CHANGES.md line, run python3 check.py again, and show me its output.",
    "Read logs/day4.log completely and summarize it in one line.",
    "The next step will be to add a regression test in tests/test_burst.py that covers a burst of exactly 5. Do not "
    "write it yet; just confirm the plan in one line.",
)
EXTRA_TURN = "Read logs/day{index}.log completely and summarize it in one line."
PROBE = (
    "Without reading any file or running any command, answer from memory in four short sections: (1) every ground "
    "rule I set for this session; (2) each file you changed, and why; (3) the last error check.py printed, "
    "verbatim; (4) the next step."
)
LAST_ERROR = "AssertionError: burst window overlaps (bucket=5, window=0.5s)"


@dataclass(frozen=True)
class Fact:
    """One planted fact: every group must match, each by any of its phrases (case-insensitive)."""

    name: str
    groups: tuple[tuple[str, ...], ...]

    def found(self, answer: str) -> bool:
        lowered = answer.lower()
        return all(any(phrase.lower() in lowered for phrase in group) for group in self.groups)


#: What A12 asks the agent to still state: the user's constraints, the files changed and why, the last error, and
#: the next step.
FACTS = (
    Fact("rule: vendor/legacy_api.py untouched", (("vendor/legacy_api.py",),)),
    Fact("rule: no public function renamed", (("rename",), ("public",))),
    Fact("rule: every change noted in CHANGES.md", (("changes.md",),)),
    Fact("changed: src/rate_limit.py, BURST to 5", (("rate_limit.py",), ("burst",), ("5",))),
    Fact("why: upstream rejects bursts above 5", (("upstream",),)),
    Fact("changed: src/config.py, REFILL_RATE to 1", (("config.py",), ("refill",))),
    # With "refill" required by the fact above, a reason states that 0 stopped refills or that 1 enables them.
    Fact(
        "why: a zero refill rate never refills",
        (("never refill", "never refills", "0 means", "zero means", "of 0", "enable"),),
    ),
    Fact("last error, verbatim", (("burst window overlaps",),)),
    Fact("next step: tests/test_burst.py", (("tests/test_burst.py", "test_burst.py"),)),
)

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
        "vendor/legacy_api.py": "# Vendored client for the legacy upstream API. Do not edit.\n\ndef call(path):\n    return path\n",
        "CHANGES.md": "# Changes\n",
        "check.py": CHECK_PY,
    }
    for index in range(1, 5 + EXTRA_READS):
        files[f"logs/day{index}.log"] = _log(index, log_tokens)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def vibe(command: Sequence[str], *args: str) -> dict:
    result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=TURN_TIMEOUT_S + 60)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"vibe {' '.join(args[:2])} printed no JSON: {result.stderr.strip()[:500]}") from error
    if not payload.get("ok", False):
        raise SystemExit(f"vibe {' '.join(args[:2])} failed: {json.dumps(payload)[:800]}")
    return payload


def model_window(model: str) -> tuple[int, Optional[int]]:
    from config.v2_config import V2Config

    supply = V2Config.load().model_hub.agents.get("vibey")
    definition = next((item for item in (supply.models if supply else ()) if item.id == model), None)
    if definition is None or not definition.context_window:
        raise SystemExit(f"Vibey has no model {model!r} with a context window in its Model Hub definition")
    return definition.context_window, definition.max_output_tokens


class Rows:
    """Read-only views of the instance's state database."""

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

    def compactions(self, session_id: str) -> list[dict]:
        rows = self._query(
            "select content_json from agent_events where session_id = ? and event_type = 'context_compaction' "
            "order by created_at",
            session_id,
        )
        return [json.loads(row["content_json"]) for row in rows]

    def checkpoint_turns(self, session_id: str) -> list[dict]:
        rows = self._query(
            "select content_json from agent_events where session_id = ? and event_type = 'fork_turn' order by created_at",
            session_id,
        )
        return [json.loads(row["content_json"]) for row in rows]

    def last_result(self, session_id: str) -> str:
        rows = self._query(
            "select content_text from messages where session_id = ? and type = 'result' order by created_at desc "
            "limit 1",
            session_id,
        )
        return rows[0]["content_text"] if rows else ""


def run(args: argparse.Namespace) -> dict:
    window, max_output = model_window(args.model)
    if window > MAX_WINDOW:
        raise SystemExit(f"{args.model} has a {window}-token window; A12 needs one of at most {MAX_WINDOW}")
    command = tuple(args.vibe.split())
    stamp = time.strftime("%Y%m%d%H%M%S")
    agent = f"a12-eval-{stamp}"
    root = Path(args.project_dir or tempfile.mkdtemp(prefix=f"a12-{stamp}-"))
    write_fixture(root, window)
    vibe(command, "agent", "create", agent, "--backend", "vibey", "--model", args.model,
         "--description", "A12 compaction-quality evaluation (temporary)")
    rows, session_id, transcript = Rows(), None, []
    try:
        turns = list(TURNS)
        extra = 0
        while turns:
            message = turns.pop(0)
            target = ("--session-id", session_id) if session_id else ("--agent", agent, "--create-session", "--cwd", str(root))
            payload = vibe(command, "agent", "run", *target, "--sync", "--no-callback",
                           "--wait-timeout", str(TURN_TIMEOUT_S), "--message", message)
            session_id = session_id or payload["session_id"]
            transcript.append({"message": message, "reply": rows.last_result(session_id)})
            if not turns and len(rows.compactions(session_id)) < 2 and extra < EXTRA_READS:
                extra += 1
                turns.append(EXTRA_TURN.format(index=4 + extra))
        compactions = rows.compactions(session_id)
        vibe(command, "agent", "run", "--session-id", session_id, "--sync", "--no-callback",
             "--wait-timeout", str(TURN_TIMEOUT_S), "--message", PROBE)
        answer = rows.last_result(session_id)
    finally:
        if not args.keep:
            vibe(command, "agent", "remove", agent)
    scores = {fact.name: fact.found(answer) for fact in FACTS}
    from core.agent_core.harness.context import PROMPT_VERSION

    turns_audit = rows.checkpoint_turns(session_id)
    return {
        "prompt_version": PROMPT_VERSION,
        "model": args.model,
        "window": window,
        "max_output_tokens": max_output,
        "session_id": session_id,
        "project_dir": str(root),
        "compactions": [
            {
                "mode": item.get("mode"),
                "reason": item.get("reason"),
                "tokens_before": item.get("tokens_before"),
                "tokens_after_estimate": item.get("tokens_after_estimate"),
                "model_written": bool(item.get("checkpoint")),
            }
            for item in compactions
        ],
        "checkpoint_turns": [
            {"outcome": item.get("outcome"), "repair": item.get("repair"), "error": item.get("error")}
            for item in turns_audit
        ],
        "score": f"{sum(scores.values())}/{len(scores)}",
        "facts": scores,
        "answer": answer,
        "turns": transcript,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--model", required=True, help="a Vibey model with a Model Hub window of at most 64,000 tokens")
    parser.add_argument("--project-dir", help="where to write the fixture (default: a new temporary directory)")
    parser.add_argument("--vibe", default="vibe", help="the vibe command of the instance under evaluation")
    parser.add_argument("--keep", action="store_true", help="keep the temporary Agent")
    args = parser.parse_args(argv)
    if os.environ.get("VIBE_DEPLOYMENT_ENV") != "regression":
        print("A12 makes real model calls and writes Sessions: run it inside a regression instance.", file=sys.stderr)
        return 2
    result = run(args)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
