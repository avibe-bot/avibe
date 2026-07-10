from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import DiscordConfig
from modules.im.discord import DiscordBot, _prioritize_claude_model_choices


def test_prioritize_pulls_aliases_ahead_of_catalog_tail():
    # claude_models() appends the bare aliases after the full catalog.
    models = ["claude-fable-5", "claude-opus-4-8", "opus", "sonnet", "haiku"]
    result = _prioritize_claude_model_choices(models, None)

    assert result[:3] == ["opus", "sonnet", "haiku"]
    # No entries gained or lost — only reordered.
    assert sorted(result) == sorted(models)


def test_prioritize_puts_current_selection_first():
    models = ["claude-fable-5", "claude-opus-4-8", "opus", "sonnet", "haiku"]
    result = _prioritize_claude_model_choices(models, "claude-opus-4-8")

    assert result[0] == "claude-opus-4-8"
    assert result[1:4] == ["opus", "sonnet", "haiku"]
    assert sorted(result) == sorted(models)


def test_prioritize_ignores_missing_or_default_selection():
    models = ["claude-fable-5", "opus"]

    # __default__ / None / unknown ids are not real catalog entries and must not
    # be injected into the option list.
    assert _prioritize_claude_model_choices(models, "__default__") == ["opus", "claude-fable-5"]
    assert _prioritize_claude_model_choices(models, None) == ["opus", "claude-fable-5"]


def test_prioritized_aliases_survive_discord_25_cap():
    # Regression for the P2: 24 catalog ids plus the 3 bare aliases appended last.
    # The Claude modal prepends one default option then slices model_options to 25,
    # so only the first 24 model entries survive. Prioritization must keep the
    # always-valid aliases inside that window.
    catalog = [f"claude-catalog-{i}" for i in range(24)]
    models = catalog + ["opus", "sonnet", "haiku"]

    result = _prioritize_claude_model_choices(models, None)

    assert {"opus", "sonnet", "haiku"} <= set(result[:24])


def test_codex_routing_uses_shared_catalog_reasoning_options():
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    channel = SimpleNamespace(send=AsyncMock())
    current_routing = SimpleNamespace(
        model="gpt-5.6-terra",
        reasoning_effort=None,
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent=None,
        claude_model=None,
        claude_reasoning_effort=None,
        codex_agent=None,
        codex_model=None,
        codex_reasoning_effort=None,
    )

    with patch.object(bot, "_fetch_channel", new=AsyncMock(return_value=channel)):
        asyncio.run(
            bot.open_routing_modal(
                trigger_id=None,
                channel_id="C123",
                registered_backends=["codex"],
                current_backend="codex",
                current_routing=current_routing,
                opencode_agents=[],
                opencode_models={},
                opencode_default_config={},
                claude_agents=[],
                claude_models=[],
                codex_agents=[],
                codex_models=["gpt-5.6-terra"],
                backend_reasoning_options={
                    "codex": {
                        "gpt-5.6-terra": [
                            {"value": "__default__", "label": "(Default)"},
                            {"value": "ultra", "label": "Ultra"},
                        ]
                    }
                },
            )
        )

    view = channel.send.await_args.kwargs["view"]
    option_sets = [
        [option.value for option in child.options]
        for child in view.children
        if getattr(child, "options", None)
    ]
    assert ["__default__", "ultra"] in option_sets
