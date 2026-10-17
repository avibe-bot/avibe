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
    # An agent merges on instruction, or on its own judgment only inside these bounds.
    flat_agents = " ".join(agents.split())
    assert "merge on the owner's or orchestrator's instruction, or on your own judgment only when" in flat_agents
    assert "touches no release or tag, database migration, or security boundary" in flat_agents
    assert "you may merge on your own judgment only when all of these hold" in flat
    assert "touches no release or tag, database migration, or security boundary" in flat

    # A Codex pass names the current head's SHA: the summary comment's review state, or a
    # pass comment naming it. A PR-body `+1` names no commit, so it is never a pass alone.
    # `Reviewed commit` is on findings too.
    assert "A pass must name the current head's SHA" in flat
    assert '`"status":"completed"` and `"has_any_findings":false`' in flat
    assert "`Codex Review: Didn't find any major issues` and naming the current SHA" in flat
    assert "never as a pass on its own" in flat
    assert "If no SHA-bearing conclusion exists for the current head, trigger another review." in flat
    assert "also contains `Reviewed commit`; that alone is not a pass" in flat
