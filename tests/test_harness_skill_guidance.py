from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_avibe_skills_teach_current_harness_defaults() -> None:
    for path in ("skills/use-avibe/SKILL.md", "skills/use-vibe-remote/SKILL.md"):
        body = _read(path)

        assert "Runs are async by default" in body
        assert "pass `--sync` only when" in body
        assert "Omit the target when the work should continue here." in body
        assert "vibe agent run --agent '<agent-name>' --message '...'" in body
        assert "from an Avibe Agent shell" in body
        assert "vibe task add --cron '<expr>' --message '...'" in body
        assert "vibe watch add --message '...' -- <cmd>" in body
        assert "vibe agent run --agent '<agent-name>' --same-scope --message '...'" in body
        assert "Avibe uses the command's current working directory" in body
        assert "Forks keep the source Session cwd by default" in body
        assert "follows the caller or source Session cwd" not in body


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
