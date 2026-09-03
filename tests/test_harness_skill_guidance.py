from __future__ import annotations

import re
import shlex
from pathlib import Path

from core.system_prompt_injection import build_system_prompt_injection
from modules.im.base import MessageContext
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]

# A `vibe task/watch` example embedded in prompt copy, stopping at the markdown
# punctuation that ends it (backtick, table pipe, newline).
_EMBEDDED_EXAMPLE = re.compile(r"vibe (?:task|watch) (?:add|update)[^`|\n\\]*")


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _embedded_cli_examples(body: str) -> list[str]:
    """Every embedded `vibe task/watch` example that carries at least one flag.

    A bare `vibe task add` in prose names the command rather than demonstrating a
    call, so it is not a live caller and is excluded.
    """

    found = set()
    for match in _EMBEDDED_EXAMPLE.finditer(body):
        example = re.sub(r"\s+\(.*\)$", "", match.group(0)).strip()
        if "--" in example:
            found.add(example)
    return sorted(found)


def test_avibe_compatibility_skill_teaches_current_harness_defaults() -> None:
    for path in ("skills/use-avibe/SKILL.md",):
        body = _read(path)

        assert "Runs are async by default" in body
        assert "pass `--sync` only when" in body
        assert "Omit the target when the work should continue here." in body
        assert "vibe agent run --agent '<agent-name>' --message '...'" in body
        assert "from an Avibe Agent shell" in body
        assert "vibe task add --cron '<expr>' --message '...'" in body
        # A scheduled command task is not a saved message: no Agent turn fires.
        assert "vibe task add --cron '<expr>' --shell '<command>'" in body
        assert (
            "vibe task add --cron '<expr>' --shell '<command>' "
            "--on-failure agent --message '<what to do>'"
        ) in body
        assert "vibe watch add --message '...' -- <cmd>" in body
        assert "vibe agent run --agent '<agent-name>' --same-scope --message '...'" in body
        assert "Avibe uses the command's current working directory" in body
        assert "Forks keep the source Session cwd by default" in body
        assert "follows the caller or source Session cwd" not in body


def test_harness_guidance_examples_parse_against_the_real_cli() -> None:
    """CLI examples in the prompt and its routed Skill must parse.

    The agent reads this prompt and runs what it finds. An example that argparse
    rejects — a renamed flag, a dropped one, a form that never existed — teaches
    the agent a call that fails at runtime. `tests/test_agent_tool_policy.py`
    guards the same rule for the native-scheduler denial strings. Harness
    operational detail now lives in ``use-avibe-harness`` while the always-on
    prompt retains only routing and live safety boundaries.
    """

    prompt = build_system_prompt_injection(
        context=MessageContext(
            user_id="user",
            channel_id="channel",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-test"},
        ),
        current_agent_backend="codex",
    )
    examples = _embedded_cli_examples(
        prompt + "\n" + _read("skills/use-avibe-harness/SKILL.md")
    )
    assert examples, "no embedded vibe task/watch examples found — did the regex drift?"

    parser = cli.build_parser()
    for example in examples:
        argv = shlex.split(example)[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse rejected a live caller
            raise AssertionError(
                f"injected prompt teaches an unparseable command: {example!r}"
            ) from exc

    # The scheduled-command form specifically: it is the whole point of the
    # command-task guidance, and the easiest example to leave stale.
    assert any(
        "--shell" in example and example.startswith("vibe task add")
        for example in examples
    ), "the injected prompt no longer shows a scheduled command task example"


def test_avibe_skills_do_not_reintroduce_legacy_harness_guidance() -> None:
    disallowed = (
        "--deliver-key",
        "`--prefix`",
        "vibe hook send",
        "--prompt`",
        "one-shot async run",
        "vibe agent run --async",
        "Delivery controls",
        "Legacy compatibility",
        "`vibe agent run` takes `--async`",
        "current Agent Session ID",
    )

    for path in (
        "skills/use-avibe/SKILL.md",
        "skills/background-watch-hook/SKILL.md",
    ):
        body = _read(path)
        for text in disallowed:
            assert text not in body, f"{path} still contains {text!r}"


def test_use_avibe_skill_keeps_its_broad_scope_without_a_session_lifecycle_protocol() -> None:
    for path in ("skills/use-avibe/SKILL.md",):
        body = _read(path)

        assert "configure, repair, explain, or operate a local Avibe installation" in body
        assert "managed background watch with `vibe watch`" in body
        assert "scheduled task with `vibe task`" in body
        assert "check or apply Avibe updates" in body
        assert "inspect logs, run doctor, check service status" in body

        for text in (
            "every user turn",
            "each user turn",
            "new user turn",
            "context compaction",
            "active Agent Session context",
        ):
            assert text not in body


def test_use_avibe_harness_owns_the_extracted_harness_protocol() -> None:
    body = _read("skills/use-avibe-harness/SKILL.md")

    assert "Avibe Harness turns user intent into durable Agent work" in body
    assert "Avibe Harness is the first-choice automation layer" in body
    assert "### Mental model" in body
    assert "### Inspecting Harness state" in body
    assert "### Choosing the right Harness shape" in body
    assert "Watch waiter contract" in body
    assert "That existing-Session send is a P1 delivery by default" in body
    assert "### Agents" in body
    assert "### Mentions in user messages" in body


def test_background_watch_skill_defaults_to_current_session() -> None:
    body = _read("skills/background-watch-hook/SKILL.md")

    assert '  --message "<what the next Agent Run should do>"' in body
    assert "Inside an Avibe-injected Agent shell, omitting the target continues this conversation." in body
    assert "Use `--session-id <id>` only when" in body
    assert "Use `--create-session --same-scope` when follow-ups should run in one visible sibling Session" in body
    assert "use `--create-session-per-run --same-scope`" in body
    assert "use `--create-session-per-run --scope-id <scopes.id>`" in body
    assert "Avibe uses the command's current working directory" in body
    assert "each follow-up should run in a visible sibling Session" not in body
    assert '  --session-id "sesk8m4q2p7x"' not in body
    assert "`--prefix`" not in body


def test_background_watch_skill_uses_bundled_waiters_and_persists_managed_pr_state() -> None:
    body = _read("skills/background-watch-hook/SKILL.md")

    assert (
        'BACKGROUND_WATCH_HOOK_DIR="<directory containing the loaded SKILL.md>"'
        in body
    )
    for environment_root in (
        "CODEX_HOME",
        "AGENTS_HOME",
        "CLAUDE_CONFIG_DIR",
        "OPENCODE_HOME",
        "XDG_CONFIG_HOME",
        "BACKGROUND_WATCH_HOOK_SKILL_FILE",
    ):
        assert environment_root not in body
    assert not (ROOT / "skills/background-watch-hook/scripts/sync_skill.py").exists()
    assert body.count('--state-file "$STATE_FILE"') >= 7
    assert body.count("--seed-state") >= 3
    assert "The one-shot PR and Actions waiters retry" in body
    assert "review-thread status" in body
    assert "independent wake signal" in body
    assert "single wake/no-wake" in body
    assert "caught up or reseeded" in body
    assert "Omit `--sha`" in body
    assert "Use `--forever` and one" in body
    assert "never reseed or replace its state between rounds" in body
    preferred_section = body.split("### Preferred PR + CI watch", 1)[1]
    preferred_forever_command = preferred_section.split('vibe watch add \\\n', 1)[
        1
    ].split("```", 1)[0]
    supervisor, waiter = preferred_forever_command.split("  -- \\\n", 1)
    assert "--timeout 0" in supervisor
    assert "--timeout 0" in waiter
    generic_forever_command = body.split('  --name "Monitor PR 151 reviews"', 1)[1].split(
        "```", 1
    )[0]
    supervisor, waiter = generic_forever_command.split("  -- \\\n", 1)
    assert "--timeout 0" in supervisor
    assert "--timeout 0" in waiter
    assert "both sides of the `--` command separator" in body
    assert "six quiet hours" in body
    assert '--workflow lint' in body


def test_pr_delivery_loop_delegates_waiters_to_background_watch_skill() -> None:
    body = _read(".agents/skills/pr-delivery-loop/SKILL.md")
    agents = _read("AGENTS.md")

    assert "## Dependency boundary" in body
    assert "Use the `background-watch-hook` skill for every managed wait." in body
    assert "Do not copy watcher implementations" in body
    assert ".agents/skills/pr-delivery-loop/scripts/" not in body
    assert not (ROOT / ".agents/skills/pr-delivery-loop/scripts").exists()
    assert not (ROOT / ".agents/skills/pr-delivery-loop/tests").exists()

    assert "the `pr-delivery-loop` skill for every implementation task" in agents
    assert "use the `background-watch-hook` skill" in agents
    assert "one durable `--forever` combined PR/CI Watch" in agents
    assert "one durable `--forever` combined PR watch" in body
    assert "omit `--sha`" in body
    assert "per-cycle `--timeout 0`" in body
    assert "disable the Watch's per-cycle timeout" in agents
    assert "Never reseed" in body
    assert "never use `--forever`" not in body
    # The PR-body reaction is a level, not an edge, and the sha-bearing pass
    # comment is emitted only in answer to an explicit trigger. Drop either and
    # the loop deadlocks waiting on something nothing will send. Matched against
    # the unwrapped text: the rule is the sentence, not where the line breaks.
    flat = " ".join(body.split())
    assert "one state slot, not an append-only log" in flat
    assert "Waiting for that comment instead of triggering waits forever." in flat
    assert "produced by an explicit trigger and by nothing else" in flat
    # The bot quotes `@codex review` in its own boilerplate, so a body-text search
    # for the trigger matches the verdicts it is supposed to be distinguished from.
    assert "never find one by matching `@codex review` in comment bodies" in flat
    # Only the *passing* auto-review is reaction-only. Broadening that to every
    # auto-review tells an agent to ignore a findings review and keep waiting on
    # a reaction that will never describe it.
    assert "An auto-review that finds something submits a review with inline threads" in flat
    assert "Only the passing case is asymmetric" in flat
    assert (ROOT / "skills/background-watch-hook/scripts/wait_pr.py").is_file()
    assert (ROOT / "skills/background-watch-hook/scripts/wait_action.py").is_file()
