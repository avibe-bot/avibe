"""The Bot prompt sections and the Bots and Agents roster (``docs/plans/bots.md`` "Prompt drafts")."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from core.system_prompt_injection import BotPromptInfo, bot_for_turn, build_system_prompt_blocks
from modules.im.base import MessageContext
from storage.importer import ensure_sqlite_state
from vibe import cli


def _context(session_id: str = "sesabc1234567", *, fork_of: str | None = None) -> MessageContext:
    target = {"metadata": {"fork_source_session_id": fork_of}} if fork_of else None
    return MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": session_id, **({"agent_session_target": target} if target else {})},
    )


_ROSTER = [
    {"name": "main", "backend": "vibey", "description": "The user's main Bot and first point of contact.",
     "bot": {"handle": "main", "display_name": "", "builtin": True}},
    {"name": "cso", "backend": "vibey", "description": "Reviews security risks.",
     "bot": {"handle": "cso", "display_name": "首席安全官"}},
    {"name": "vibey", "backend": "vibey", "description": "Vibey, the official agent of Avibe."},
    {"name": "reviewer", "backend": "codex", "description": "Reviews code."},
]

_MAIN = BotPromptInfo(
    handle="main", display_name="", role="The user's main Bot and first point of contact.",
    session_id="ses_main", builtin=True,
)
_CSO = BotPromptInfo(
    handle="cso", display_name="首席安全官", role="Reviews security risks", session_id="ses_cso",
)


def _blocks(**kwargs):
    return {block.module_id: block.text for block in build_system_prompt_blocks(context=_context(), **kwargs)}


def test_the_roster_lists_each_bot_once_as_a_bot():
    roster = _blocks(enabled_agents=_ROSTER)["harness-agents-prompt"]
    bots, agents = roster.split("| Agent Name | Backend | Agent Description |")
    assert "| main | Vibey | The user's main Bot and first point of contact. |" in bots
    assert bots.index("| main |") < bots.index("| cso | 首席安全官 | Reviews security risks. |")
    assert "| reviewer | codex |" in agents and "| vibey | vibey |" in agents
    assert "| main |" not in agents and "| cso |" not in agents
    assert "`@<bot:name>` points at a Bot" in roster
    assert "`@<name>` without a type, found in older messages, points at an Agent." in roster


def test_bot_sections_appear_only_for_a_bot_and_the_main_section_only_for_the_main_bot():
    worker = _blocks(enabled_agents=_ROSTER)
    assert not {"bot-identity-prompt", "bot-memory-prompt", "bot-main-prompt", "bot-harness-prompt"} & set(worker)
    assert "session-title-prompt" in worker

    main = _blocks(enabled_agents=_ROSTER, bot=_MAIN)
    assert "You are Vibey, the Bot `@<bot:main>`. Your role: The user's main Bot and first point of contact." in (
        main["bot-identity-prompt"]
    )
    assert "Session `ses_main`" in main["bot-identity-prompt"]
    # The permanent Session is named once; the directory is the environment block's cwd, not a prompt value.
    assert "".join(text for module, text in main.items() if module.startswith("bot-")).count("ses_main") == 1
    assert "## You are the user's main Bot" in main["bot-main-prompt"]
    assert [module for module in main if module.startswith("bot-")] == [
        "bot-identity-prompt", "bot-memory-prompt", "bot-main-prompt", "bot-harness-prompt",
    ]
    # A Bot's Session is titled by its display name.
    assert "session-title-prompt" not in main

    cso = _blocks(enabled_agents=_ROSTER, bot=_CSO)
    assert "You are 首席安全官, the Bot `@<bot:cso>`. Your role: Reviews security risks. Your one" in (
        cso["bot-identity-prompt"]
    )
    assert "bot-main-prompt" not in cso and "bot-harness-prompt" in cso


def test_a_bots_sections_are_byte_stable_across_its_turns_and_its_forks():
    def bot_text(context):
        return [
            block.text
            for block in build_system_prompt_blocks(context=context, enabled_agents=_ROSTER, bot=_CSO)
            if block.module_id.startswith("bot-")
        ]

    first = bot_text(_context("ses_cso"))
    assert first == bot_text(_context("ses_cso"))
    assert first == bot_text(_context("sesfork123456", fork_of="ses_cso"))
    # They come before anything that names the Session, so a fork shares that prefix.
    order = [block.module_id for block in build_system_prompt_blocks(context=_context(), bot=_CSO)]
    assert order.index("bot-memory-prompt") < order.index("session-start-prompt")


def _argv(example: str) -> list[str]:
    # A placeholder is a value; `...` after a flag is its value, and a trailing one stands for the rest.
    tokens = shlex.split(re.sub(r"<[^>]+>", "x", example))
    return [
        "x" if token == "..." else token
        for index, token in enumerate(tokens)
        if token != "..." or tokens[index - 1].startswith("--")
    ]


def test_prompt_cli_examples_parse_against_the_real_cli(capsys):
    """Every `vibe` command the roster, mention, and Bot sections teach is a live caller: an example parses, a bare
    command name is a real command, a flag "Handing out work" names on its own is a `vibe agent run` flag, and a
    `--bot` example carries no flag `--bot` refuses."""
    blocks = _blocks(enabled_agents=_ROSTER, bot=_MAIN)
    text = blocks["harness-agents-prompt"] + "".join(t for module, t in blocks.items() if module.startswith("bot-"))
    fragments = re.findall(r"`(--[a-z][^`]*)`", blocks["bot-harness-prompt"])
    examples = re.findall(r"`(vibe [^`]+)`", text) + [f"vibe agent run {f}" for f in fragments if " " in f]
    assert any("--bot" in example for example in examples)
    refused = {attr for attr, _flag in cli._BOT_RUN_CONFLICTS}
    parser = cli.build_parser()
    for example in examples:
        argv = _argv(example)[1:]
        if not any(token.startswith("-") or token == "x" for token in argv):
            with pytest.raises(SystemExit) as named:
                parser.parse_args([*argv, "--help"])
            assert named.value.code == 0, example
            continue
        if argv[:2] == ["agent", "run"] and "--message" not in argv:
            argv += ["--message", "x"]
        args = parser.parse_args(argv)
        if getattr(args, "bot", None):
            assert not [attr for attr in refused if getattr(args, attr, None) not in (None, False, "")], example
    capsys.readouterr()
    with pytest.raises(SystemExit):
        parser.parse_args(["agent", "run", "--help"])
    run_flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", capsys.readouterr().out))
    assert {"--queue", "--visible", "--model"} <= run_flags
    for fragment in fragments:
        assert set(re.findall(r"--[a-z][a-z-]*", fragment)) <= run_flags, fragment


def test_bot_harness_prompt_matches_plan_draft_and_teaches_read_receipts():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "core/prompts/bot-harness.md").read_text()
    plan = (root / "docs/plans/bots.md").read_text()
    section = plan.split("### Handing out work (every Bot, after the main Bot's section)\n", 1)[1]
    draft = section.split("```markdown\n", 1)[1].split("\n```", 1)[0] + "\n"
    assert prompt == draft

    built = _blocks(enabled_agents=_ROSTER, bot=_CSO)["bot-harness-prompt"]
    assert "read receipt with its last reply" in built
    assert "handles your message without writing back" in built
    assert "following up is yours when you were waiting on it" in built
    assert "goes idle without answering" not in built


def test_bots_get_no_messaging_guidance_in_their_system_prompt():
    """bots.md D4 (2026-11-03): where a result goes is said on each input, not in the system prompt."""
    bot = _blocks(enabled_agents=_ROSTER, bot=_CSO)
    identity = bot["bot-identity-prompt"]
    assert "begin with a `[Harness: ...]` line naming the sender" in identity
    roster = _blocks(enabled_agents=_ROSTER)["harness-agents-prompt"]
    bot_rules = roster[roster.index("Bot rules:"): roster.index("Agent rules:")]
    for text in (identity, bot_rules):
        assert "callback" not in text and "report" not in text and "silent" not in text
    # Handing out work says a dispatched Session reports back, never to ask for a callback (owner, 2026-11-08).
    assert "callback" not in bot["bot-harness-prompt"]


@pytest.fixture()
def bots(monkeypatch, tmp_path):
    from core.bots import BotService
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = VibeAgentStore()
    store.ensure_builtin_default_agents([])
    with BotService(store) as service:
        yield service
    store.close()


def test_a_turn_runs_as_a_bot_only_in_the_bots_directory(bots, tmp_path):
    main = bots.ensure_main_bot()
    vibey = bots.store.get_builtin_default_agent_for_backend("vibey")
    info = bot_for_turn(bots.store, agent_id=main.agent.id, cwd=str(tmp_path / "bots" / "main"))
    assert info == BotPromptInfo(
        handle="main", display_name="", role=main.role, session_id="ses_main", builtin=True,
    )
    # A fresh --agent Session of the Bot's Agent elsewhere, or another Agent in the directory, is a worker.
    assert bot_for_turn(bots.store, agent_id=main.agent.id, cwd=str(tmp_path)) is None
    assert bot_for_turn(bots.store, agent_id=vibey.id, cwd=str(tmp_path / "bots" / "main")) is None
    bots.update_bot("main", display_name="歪比")
    assert bot_for_turn(bots.store, agent_id=main.agent.id, cwd=str(tmp_path / "bots" / "main")).display_name == "歪比"
