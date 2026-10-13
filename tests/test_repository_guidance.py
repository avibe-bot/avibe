"""Repository guidance contracts that can run without application dependencies.

The docs-only lane executes this module with pytest --noconftest, so changing
AGENTS.md cannot bypass its existing guidance checks. Keep runtime/CLI examples
in test_harness_skill_guidance.py, whose input files always select full CI.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pr_delivery_loop_delegates_waiters_to_background_watch_skill() -> None:
    body = (ROOT / ".agents/skills/pr-delivery-loop/SKILL.md").read_text()
    flat = " ".join(body.split())
    agents = (ROOT / "AGENTS.md").read_text()

    assert "`background-watch-hook` owns managed waits, waiter scripts, cursor mechanics" in flat
    assert "Load that skill before a managed wait." in flat
    assert "do not vendor or reimplement it in a repository" in flat
    assert ".agents/skills/pr-delivery-loop/scripts/" not in body
    assert not (ROOT / ".agents/skills/pr-delivery-loop/scripts").exists()
    assert not (ROOT / ".agents/skills/pr-delivery-loop/tests").exists()

    assert "the `pr-delivery-loop` skill for every implementation task" in agents
    assert "use the `background-watch-hook` skill" in agents
    assert "one durable `--forever` combined PR/CI Watch" in agents
    assert "one durable combined PR + CI Watch per owner/concern" in flat
    assert "`wait_pr.py`, `--forever`, required `--workflow` names" in flat
    assert "no fixed `--sha` during a normal delivery loop" in flat
    assert "Set `--timeout 0` on both supervisor and waiter; do not impose a lifetime cap." in flat
    assert "disable the Watch's per-cycle timeout" in agents
    assert "Never reseed, rotate, or replace the cursor between rounds." in flat
    assert "never use `--forever`" not in body
    # Gate on captured head-bound evidence, not the reaction's persistent level.
    assert "A Codex `+1` **on the PR body** is a pass only when the original durable Watch" in flat
    assert "captured it as new after the current head epoch began" in flat
    assert "the prior-head review was already terminal, and the head is unchanged" in flat
    assert "If the timeline cannot bind it, trigger review on the unchanged head" in flat
    assert "require a SHA-bearing verdict" in flat
    assert "`Codex Review: Didn't find any major issues` and name the current reviewed SHA" in flat
    assert "`Reviewed commit` alone is not a pass; findings reviews use it too." in flat
    # The bot quotes `@codex review` in its own boilerplate, so a body-text search
    # for the trigger matches the verdicts it is supposed to be distinguished from.
    assert "Save the trigger comment ID returned by your own write and read it back." in flat
    assert "Never select the last comment or search bodies for the trigger text." in flat
    # Only the *passing* auto-review is reaction-only. Broadening that to every
    # auto-review tells an agent to ignore a findings review and keep waiting on
    # a reaction that will never describe it.
    assert "Act on auto-review findings as inline reviews." in flat
    assert "A clean auto-review may pass only through the PR-body reaction" in flat
    assert "trigger review when a SHA-bearing pass comment is needed" in flat
    assert "rather than waiting for one to appear automatically" in flat
    assert (ROOT / "skills/background-watch-hook/scripts/wait_pr.py").is_file()
    assert (ROOT / "skills/background-watch-hook/scripts/wait_action.py").is_file()
