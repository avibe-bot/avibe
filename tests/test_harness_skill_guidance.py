from __future__ import annotations

import re
import shlex
from pathlib import Path

from core.managed_skills import parse_skill_file, render_skill_content
from core.system_prompt_injection import build_system_prompt_injection
from modules.im.base import MessageContext
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]

# A `vibe task/watch` example embedded in prompt copy, stopping at the markdown
# punctuation that ends it (backtick, table pipe, newline).
_EMBEDDED_EXAMPLE = re.compile(r"vibe (?:task|watch) (?:add|update)[^`|\n\\]*")


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _use_avibe_references() -> list[Path]:
    directory = ROOT / "skills/use-avibe"
    targets = re.findall(r"\]\((references/[^)]+\.md)\)", (directory / "SKILL.md").read_text())
    assert targets, "the Skill entry must route to its operational references"
    return [directory / target for target in targets]


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


def test_use_avibe_routes_harness_work_to_the_harness_skill() -> None:
    """Harness rules have one source: `use-avibe` points to `use-avibe-harness`.

    A second copy in `use-avibe` drifted from the first, so the operations Skill
    routes Harness work to the Harness Skill instead of restating its rules.
    """

    entry = _read("skills/use-avibe/SKILL.md")
    row = next(line for line in entry.splitlines() if "manage watches or Session queues" in line)
    assert "Load the `use-avibe-harness` Skill" in row
    for path in _use_avibe_references():
        body = path.read_text()
        for rule in ("Watch waiter contract", "vibe session send-now", "vibe session queue remove"):
            assert rule not in body, f"{path.relative_to(ROOT)} restates Harness rule {rule!r}"


def test_harness_guidance_examples_parse_against_the_real_cli() -> None:
    """CLI examples in the prompt and its routed Skill must parse.

    The agent reads this prompt and runs what it finds. An example that argparse
    rejects — a renamed flag, a dropped one, a form that never existed — teaches
    the agent a call that fails at runtime. `tests/test_agent_tool_policy.py`
    guards the same rule for the native-scheduler denial strings. Harness
    operational detail now lives in ``use-avibe-harness`` while the always-on
    prompt retains only stable routing.
    """

    prompt = build_system_prompt_injection(
        context=MessageContext(
            user_id="user",
            channel_id="channel",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-test"},
        ),
    )
    examples = _embedded_cli_examples(
        prompt + "\n" + _read("skills/use-avibe-harness/SKILL.md")
        + "\n" + "\n".join(path.read_text() for path in _use_avibe_references())
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
    # Spellings the CLI no longer accepts; a Skill that still teaches one teaches a failing call.
    disallowed = (
        "--deliver-key",
        "--post-to",
        "--session-key",
        "--send-now",
        "--visibility foreground",
        "--visibility background",
        "--prefix",
        "vibe hook",
        "--prompt`",
        "one-shot async run",
        "--async",
        "Delivery controls",
        "Legacy compatibility",
        "current Agent Session ID",
    )

    for path in (
        "skills/use-avibe/SKILL.md",
        "skills/use-avibe-harness/SKILL.md",
        "skills/choose-model/SKILL.md",
        "skills/background-watch-hook/SKILL.md",
        *(path.relative_to(ROOT).as_posix() for path in _use_avibe_references()),
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
            assert text not in body + "\n".join(path.read_text() for path in _use_avibe_references())


def test_use_avibe_harness_owns_the_extracted_harness_protocol() -> None:
    body = _read("skills/use-avibe-harness/SKILL.md")

    assert "Avibe Harness turns user intent into durable Agent work" in body
    assert "Avibe Harness is the first-choice automation layer" in body
    assert "Backend-native work is process-local" in body
    assert "anything that may outlive the turn through Harness" in body
    assert "Never detach with `nohup` or a trailing `&`" in body
    assert "live backend-native tool policy" not in body
    assert "### Mental model" in body
    assert "### Choosing the shape" in body
    assert "Watch waiter contract" in body
    assert "`--queue` waits for the running Turn instead" in body
    assert "`vibe agent run --bot <bot> --message ...`" in body
    # The resident roster (core/prompts/harness-agents.md) owns the Bot and Agent
    # rules and typed mentions; a copy here went stale once, without `--bot`.
    assert "### Agents" not in body
    assert "### Mentions" not in body
    # Loaded on demand, so it stays near the reviewed ~1.3k-token budget.
    assert len(body) <= 6000, len(body)


def test_background_watch_skill_defaults_to_current_session() -> None:
    body = _read("skills/background-watch-hook/SKILL.md")

    assert '--message "PR #151 has an event.' in body
    assert "Inside an Avibe-injected Agent shell, omitting the target continues this conversation." in body
    assert "Use `--session-id <id>` only when" in body
    assert "Use `--create-session --same-scope` for one visible sibling Session" in body
    assert "`--create-session-per-run --same-scope` for a separate visible Session per event" in body
    assert "Use `--scope-id <scopes.id>` instead of `--same-scope`" in body
    assert "Avibe uses the command's current working directory" in body
    assert "each follow-up should run in a visible sibling Session" not in body
    assert '  --session-id "sesk8m4q2p7x"' not in body
    assert "`--prefix`" not in body


def test_background_watch_skill_uses_bundled_waiters_and_persists_managed_pr_state() -> None:
    entry = _read("skills/background-watch-hook/SKILL.md")
    reference = _read("skills/background-watch-hook/references/waiters.md")
    body = entry + "\n" + reference
    skill = parse_skill_file(ROOT / "skills/background-watch-hook/SKILL.md", priority=(1, 0, 1))
    assert skill is not None
    rendered = render_skill_content(skill)
    assert reference not in rendered
    assert "Read it only for" in entry and "not for routine PR monitoring" in entry

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
    preferred_section = entry.split("Preferred PR + CI watch", 1)[1]
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


def test_pr_delivery_loop_keeps_optional_references_out_of_routine_loading() -> None:
    directory = ROOT / ".agents/skills/pr-delivery-loop"
    body = (directory / "SKILL.md").read_text()
    flat = " ".join(body.split())
    named = re.findall(r"references/[\w.-]+\.md", body)

    # A reference is read only when its step calls for it, never in the routine delivery
    # loop: the rationale when a rule seems wrong or a failure is unfamiliar, and local
    # regression after the user picks a regression update.
    assert named == ["references/rationale.md", "references/local-regression.md"]
    assert all((directory / name).is_file() for name in named)
    assert "routine delivery does not need it" in flat
    assert "After the user chooses one, read `references/local-regression.md` before acting." in flat
    assert "synchronize the entire maintained package, including `references/`, byte-for-byte" in flat

    skill = parse_skill_file(directory / "SKILL.md", priority=(1, 0, 1))
    assert skill is not None
    rendered = render_skill_content(skill)
    assert "## Keep one Watch on the PR" in rendered
    for reference in (directory / "references").glob("*.md"):
        assert reference.read_text() not in rendered


def test_agent_prompt_audit_examples_parse_against_the_real_cli() -> None:
    """The audit Skill teaches live `vibe` calls.

    Each backticked example that carries a flag or placeholder is parsed with
    its `<placeholder>` and `...` slots filled, so a renamed command or flag
    fails here instead of in an audit run.
    """

    body = _read("skills/agent-prompt-audit/SKILL.md")
    examples = sorted(
        {example for example in re.findall(r"`(vibe [^`]+)`", body) if "--" in example or "<" in example}
    )
    assert examples, "no embedded vibe examples found — did the regex drift?"

    parser = cli.build_parser()
    for example in examples:
        argv = shlex.split(re.sub(r"<[^>]+>|\.\.\.", "x", example))[1:]
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            raise AssertionError(f"agent-prompt-audit teaches an unparseable command: {example!r}") from exc


def test_guidance_teaches_receipts_and_no_reply_not_successful_callbacks() -> None:
    """bots.md D4 (2026-11-09): explicit reports and receipts replace successful callbacks."""
    stale = (
        "return through callbacks",
        "callbacks return to this conversation by default",
        "to this conversation by default",
        "is sent back to this conversation",
        "only with `--callback`",
        "callback policy",
        "callback 策略",
    )
    paths = [
        ROOT / "core/prompts/harness-agents.md",
        ROOT / "skills/use-avibe-harness/SKILL.md",
        ROOT / "skills/agent-prompt-audit/SKILL.md",
        *_use_avibe_references(),
        *(ROOT / "docs" / name for name in ("CLI.md", "CLI_ZH.md", "COMMANDS.md", "COMMANDS_ZH.md")),
    ]
    for path in paths:
        body = " ".join(path.read_text().split()).lower()
        for text in stale:
            assert text.lower() not in body, f"{path.relative_to(ROOT)} still says {text!r}"
    roster = _read("core/prompts/harness-agents.md")
    assert "--callback" not in roster and "--no-callback" not in roster
    for path in ("core/prompts/harness-agents.md", "skills/use-avibe-harness/SKILL.md"):
        body = _read(path)
        assert "read receipt" in body and "--no-reply" in body
