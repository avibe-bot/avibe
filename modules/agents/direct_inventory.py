"""Noninteractive native probes. Only a complete, attributable answer is publishable."""

from __future__ import annotations

import asyncio
import json
import tempfile

from core.backend_restart import finish_native_operation
from core.command_runner import run_supervised_command
from core.handlers.direct_inventory_identity import NativeScope, claude_account_matches
from core.handlers.direct_model_menu import PickerModel

_MAX_OUTPUT = 8 * 1024 * 1024


def _rows(payload, key):
    rows = payload[key]
    if not isinstance(rows, list) or len(rows) > 8192 or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid inventory")
    return rows


async def _codex(binary: str, scope: NativeScope) -> list[PickerModel]:
    # debug models is one unpaginated native document; never pass --bundled
    # or a Hub launcher/environment. Do not load a caller's project config.
    with tempfile.TemporaryDirectory() as cwd:
        answer = await run_supervised_command(
            command=[binary, "debug", "models"],
            cwd=cwd,
            timeout_seconds=10,
            label="Codex native inventory",
            discard_stderr=True,
            max_output_bytes=_MAX_OUTPUT,
        )
    if answer.exit_code or answer.timed_out or answer.stdout_truncated:
        raise ValueError("incomplete Codex inventory")
    visible = []
    for row in _rows(json.loads(answer.stdout), "models"):
        if row["visibility"] in ("hide", "hidden", "none"):
            continue
        if row["visibility"] != "list":
            raise ValueError("unknown model visibility")
        if scope.mode == "api":
            supported = row["supported_in_api"]
            if not isinstance(supported, bool):
                raise ValueError("unknown API-key eligibility")
            if not supported:
                continue
        efforts = row.get("supported_reasoning_levels")
        visible.append(
            (
                row["priority"],
                PickerModel(
                    row["slug"],
                    row.get("display_name"),
                    reasoning_efforts=None if efforts is None else tuple(e["effort"] for e in efforts),
                    default_reasoning_effort=row.get("default_reasoning_level"),
                ),
            )
        )
    return [model for _, model in sorted(visible, key=lambda item: item[0])]


async def _claude(settings, binary: str, scope: NativeScope, controller) -> list[PickerModel]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from vibe.claude_config import CLAUDE_NATIVE_MCP_DISABLED, build_claude_subprocess_env

    env = await asyncio.to_thread(build_claude_subprocess_env, settings)
    with tempfile.TemporaryDirectory() as cwd:
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cli_path=binary,
                cwd=cwd,
                env=env,
                settings='{"disableAllHooks":true}',
                setting_sources=["user"],
                extra_args={"no-session-persistence": None},
                max_buffer_size=_MAX_OUTPUT,
                **CLAUDE_NATIVE_MCP_DISABLED,
            )
        )
        try:
            await client.connect()
            info = await client.get_server_info()
            if not isinstance(info, dict) or not claude_account_matches(scope, info):
                raise ValueError("unverified native Claude account")
            return [
                PickerModel(
                    row["value"], row.get("displayName"), row.get("resolvedModel"), row.get("supportedEffortLevels")
                )
                for row in _rows(info, "models")
            ]
        finally:
            # Keep the native-credential lease until bounded disconnect/reaping
            # finishes, including cancellation during initialization or shutdown.
            await finish_native_operation(controller.agent_auth_service._disconnect_claude_probe_client(client))


async def discover(backend: str, settings, binary: str, cwd: str, scope: NativeScope, controller) -> list[PickerModel]:
    if backend == "codex":
        return await _codex(binary, scope)
    if backend == "claude":
        return await _claude(settings, binary, scope, controller)
    if backend == "opencode":
        from modules.agents.opencode.picker_inventory import discover as from_server

        return await from_server(controller, binary, cwd)
    raise ValueError("not a polled native backend")
