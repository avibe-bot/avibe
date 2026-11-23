"""Shared discovery and executable command contracts for system scheduling."""

from pathlib import Path
import shlex

from core.managed_skills import (
    BUILTIN_SKILLS_ROOT_ENV,
    load_skill,
    publish_builtin_skills,
)
from core.system_prompt_injection import build_system_prompt_injection
from modules.agents.catalog import AGENT_BACKENDS
from modules.im import MessageContext
from vibe import cli


ROOT = Path(__file__).resolve().parents[1]


def test_system_schedule_skill_is_published_and_discovered_by_every_backend(tmp_path, monkeypatch):
    """A packaged skill must reach every backend, not just a local author's catalog.

    Generic resolver coverage uses synthetic skills and misses omitting this
    built-in from publication or the common routing prompt.
    """
    catalog = publish_builtin_skills(
        source_root=ROOT / "skills",
        destination_root=tmp_path / "builtin-skills",
    )
    monkeypatch.setenv(BUILTIN_SKILLS_ROOT_ENV, str(catalog))
    project = tmp_path / "project"
    project.mkdir()
    context = MessageContext(
        user_id="fixture-user",
        channel_id="fixture-channel",
        platform="avibe",
        platform_specific={"agent_session_id": "fixture-session"},
    )

    for backend in AGENT_BACKENDS:
        prompt = build_system_prompt_injection(
            backend=backend, context=context, skills_cwd=project,
        )
        assert "- use-system-schedule:" in prompt, backend
        assert "load `use-system-schedule`" in prompt, backend
        # The policy is loaded on demand, rather than copied into every prompt.
        assert "## iPhone: one operation through completion" not in prompt

    loaded = load_skill("use-system-schedule", project)
    assert loaded is not None
    assert loaded.directory == catalog / "use-system-schedule"
    assert loaded.body.strip() == (
        ROOT / "skills/use-system-schedule/SKILL.md"
    ).read_text().split("---", 2)[2].strip()


def test_system_schedule_skill_commands_parse_and_preserve_operation_identity():
    """The shipped examples are real callers, including the cancel positional ID.

    Existing Harness example tests cover task/watch commands only; a stale
    schedule flag or a different ID in status/cancel would otherwise ship.
    """
    body = (ROOT / "skills/use-system-schedule/SKILL.md").read_text()
    examples = [
        line for line in body.splitlines() if line.startswith("vibe schedule ")
    ]
    assert len(examples) == 3
    parser = cli.build_parser()
    parsed = [parser.parse_args(shlex.split(line)[1:]) for line in examples]
    assert [item.schedule_command for item in parsed] == ["create", "status", "cancel"]
    create, status, cancel = parsed
    assert create.kind == "alarm"
    assert create.operation_id == status.operation_id == cancel.operation_id
    reminder_args = shlex.split(examples[0])[1:]
    reminder_args[reminder_args.index("--kind") + 1] = "reminder"
    assert parser.parse_args(reminder_args).kind == "reminder"
