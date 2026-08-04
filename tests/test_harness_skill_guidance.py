from __future__ import annotations

import re
import shlex
from pathlib import Path

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


def test_avibe_skills_teach_current_harness_defaults() -> None:
    for path in ("skills/use-avibe/SKILL.md", "skills/use-vibe-remote/SKILL.md"):
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


def test_injected_harness_prompt_examples_parse_against_the_real_cli() -> None:
    """CLI examples in the injected prompt are live callers, so they must parse.

    The agent reads this prompt and runs what it finds. An example that argparse
    rejects — a renamed flag, a dropped one, a form that never existed — teaches
    the agent a call that fails at runtime. `tests/test_agent_tool_policy.py`
    guards the same rule for the native-scheduler denial strings; this guards the
    Harness prompt itself, which is where scheduled-command guidance lives.
    """

    examples = _embedded_cli_examples(_read("core/system_prompt_injection.py"))
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
        "skills/use-vibe-remote/SKILL.md",
        "skills/background-watch-hook/SKILL.md",
    ):
        body = _read(path)
        for text in disallowed:
            assert text not in body, f"{path} still contains {text!r}"


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
