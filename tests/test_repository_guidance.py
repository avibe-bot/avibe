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

    # `background-watch-hook` owns the Watch's mechanics; this skill keeps only the principle.
    assert "The `background-watch-hook` skill owns Watch mechanics" in flat
    assert "Load it before a managed wait." in flat
    assert ".agents/skills/pr-delivery-loop/scripts/" not in body
    assert not (ROOT / ".agents/skills/pr-delivery-loop/scripts").exists()
    assert not (ROOT / ".agents/skills/pr-delivery-loop/tests").exists()
    assert (ROOT / "skills/background-watch-hook/scripts/wait_pr.py").is_file()
    assert (ROOT / "skills/background-watch-hook/scripts/wait_action.py").is_file()
    # One Watch per PR and owner lives through delivery, and its cursor is never reset.
    assert "one persistent Watch per PR and owner" in flat
    assert "Do not rebuild it or reset its cursor between rounds" in flat

    assert "the `pr-delivery-loop` skill for every implementation task" in agents
    assert "use the `background-watch-hook` skill" in agents
    assert "one durable `--forever` combined PR/CI Watch" in agents
    assert "disable the Watch's per-cycle timeout" in agents
    # The skill owns the procedure but cannot weaken the root baseline, merge authority included.
    assert "cannot weaken the baseline below" in " ".join(agents.split())
    assert "never merge without explicit owner instruction" in " ".join(agents.split())

    # A Codex pass binds to the current head: a comment naming its SHA, or a PR-body
    # `+1` the Watch saw arrive after it was pushed. `Reviewed commit` is on findings too.
    assert "`Codex Review: Didn't find any major issues` and naming the current SHA" in flat
    assert "a `+1` reaction on the PR body that the Watch definitely observed as new after this head was pushed" in flat
    assert "require a SHA-bearing conclusion" in flat
    assert "also contains `Reviewed commit`; that alone is not a pass" in flat
