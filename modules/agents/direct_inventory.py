"""Native, non-interactive inventory probes. Only a complete answer is publishable."""

from __future__ import annotations

import asyncio
import json
import tempfile

from core.command_runner import run_supervised_command
from core.handlers.direct_model_menu import PickerModel

_MAX_OUTPUT = 8 * 1024 * 1024


def _rows(payload, key):
    rows = payload[key]
    if not isinstance(rows, list) or len(rows) > 8192 or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid inventory")
    return rows


async def _codex(binary: str) -> list[PickerModel]:
    # `debug models` (without --bundled) is one complete document, not the
    # paginated RPC. Run outside any project's config and never use the Hub
    # turn launcher's -c model_catalog_json override or projected CODEX_HOME.
    from vibe.upgrade import get_safe_cwd

    answer = await run_supervised_command(
        command=[binary, "debug", "models"],
        cwd=get_safe_cwd(),
        timeout_seconds=10,
        label="Codex native inventory",
        discard_stderr=True,
        max_output_bytes=_MAX_OUTPUT,
    )
    if answer.exit_code or answer.timed_out or answer.stdout_truncated:
        raise ValueError("incomplete Codex inventory")
    result = []
    for row in _rows(json.loads(answer.stdout), "models"):
        if row["visibility"] in ("hide", "hidden", "none"):
            continue
        if row["visibility"] != "list":
            raise ValueError("unknown model visibility")
        efforts = row.get("supported_reasoning_levels")
        result.append(
            PickerModel(
                row["slug"],
                row.get("display_name"),
                reasoning_efforts=None if efforts is None else tuple(e["effort"] for e in efforts),
                default_reasoning_effort=row.get("default_reasoning_level"),
            )
        )
    return result


async def _claude(settings, binary: str) -> list[PickerModel]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from vibe.claude_config import CLAUDE_NATIVE_MCP_DISABLED, build_claude_subprocess_env

    env = await asyncio.to_thread(build_claude_subprocess_env, settings)
    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(
            cli_path=binary,
            cwd=cwd,
            env=env,
            settings='{"disableAllHooks":true}',
            setting_sources=["user"],
            extra_args={"no-session-persistence": None},
            max_buffer_size=_MAX_OUTPUT,
            **CLAUDE_NATIVE_MCP_DISABLED,
        )
        # Initialize control only. No query, user turn, authentication or TUI.
        async with ClaudeSDKClient(options=options) as client:
            info = await client.get_server_info()
    return [
        PickerModel(row["value"], row.get("displayName"), row.get("resolvedModel"), row.get("supportedEffortLevels"))
        for row in _rows(info, "models")
    ]


async def _opencode(controller, cwd: str) -> list[PickerModel]:
    agent = controller.agent_service.agents["opencode"]
    async with agent.current_server() as server:
        providers = await server.direct_picker_providers(cwd)
    result = []
    for provider in providers:
        for model_id, info in provider["models"].items():
            variants = info.get("variants")
            if variants is not None and not isinstance(variants, dict):
                raise ValueError("invalid OpenCode variants")
            result.append(
                PickerModel(
                    f"{provider['id']}/{model_id}",
                    info.get("name"),
                    reasoning_efforts=None if variants is None else tuple(variants),
                )
            )
    return result


async def discover(backend: str, settings, binary: str, cwd: str, controller) -> list[PickerModel]:
    if backend == "codex":
        return await _codex(binary)
    if backend == "claude":
        return await _claude(settings, binary)
    if backend == "opencode":
        return await _opencode(controller, cwd)
    if backend == "antigravity":
        from modules.agents.antigravity.models import read_antigravity_live_models

        rows = await asyncio.to_thread(read_antigravity_live_models, settings)
        if not rows:
            raise ValueError("no authenticated ACP snapshot")
        return [PickerModel(**row) for row in rows]
    raise ValueError("unknown backend")
