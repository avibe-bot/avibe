"""Warm execution adapter for the CLI's existing mutation semantics.

The private channel carries invocation inputs, not ambient process globals.
Only named operations are supported; this is not an arbitrary CLI/SQL endpoint.
"""
from __future__ import annotations

import asyncio
import math
from argparse import Namespace
from typing import Any, Awaitable, Callable

from core.control_requests import ControlRequest, failure, result

_AGENT_RUN_STRINGS = frozenset({
    "agent", "bot", "session_id", "fork_session", "scope_id", "visibility",
    "model", "reasoning_effort", "cwd", "callback_session_id", "message", "message_file",
})
_AGENT_RUN_BOOLS = frozenset({
    "queue", "fork_self", "same_scope", "callback", "no_callback", "close_after", "sync_run", "json",
})


def _agent_run_arguments(arguments: dict[str, Any]) -> Namespace:
    defaults = {**dict.fromkeys(_AGENT_RUN_STRINGS), **dict.fromkeys(_AGENT_RUN_BOOLS, False), "wait_timeout": None}
    allowed = {*defaults, "command", "agent_command"}
    if arguments.keys() - allowed:
        raise ValueError("Unknown Agent Run arguments")
    values = {**defaults, **arguments}
    if (
        any(values[key] is not None and not isinstance(values[key], str) for key in _AGENT_RUN_STRINGS)
        or any(type(values[key]) is not bool for key in _AGENT_RUN_BOOLS)
        or values.get("command", "agent") != "agent"
        or values.get("agent_command", "run") != "run"
    ):
        raise ValueError("Invalid Agent Run arguments")
    wait = values["wait_timeout"]
    if wait is not None and (type(wait) not in {int, float} or not math.isfinite(wait)):
        raise ValueError("Invalid Agent Run wait timeout")
    return Namespace(**values)


async def handle_control_request(
    controller: Any,
    payload: object,
    *,
    run_sync: Callable[[Callable[[], Any]], Awaitable[Any]] = asyncio.to_thread,
) -> dict[str, Any]:
    try:
        request = ControlRequest.from_payload(payload)
        if request.operation != "agent.run":
            return failure("unsupported_operation", "This service does not support the requested operation")
        args = _agent_run_arguments(request.arguments)
    except ValueError as exc:
        return failure("invalid_control_request", str(exc))

    # Admission, file access, configuration and SQLite all run off the loop. The
    # second deadline check is inside the worker, after any executor queue wait.
    def enqueue():
        from vibe import cli
        from vibe.authorization import invocation_authority

        if request.expired:
            return failure("deadline_expired", "Control request expired before execution"), None
        try:
            authority = cli._cli_invocation_authority(request.caller_env)
            with invocation_authority(authority):
                try:
                    cli._require_command_path_admission(("agent", "run"))
                except cli.TaskCliError as exc:
                    return result(cli._task_error_payload(exc), execution="not_executed"), None
                if request.expired:
                    return failure("deadline_expired", "Control request expired before execution"), None
                agent_store = controller.vibe_agent_store
                request_store = controller.scheduled_task_service.request_store
                response, callback_session_id = cli._enqueue_agent_run(
                    args, caller_env=request.caller_env, invocation_cwd=request.cwd,
                    agent_store=agent_store, request_store=request_store,
                )
                return result(response), (authority, request_store, callback_session_id)
        except Exception as exc:
            return result(cli._task_error_payload(exc, help_command="vibe agent run --help")), None

    if request.expired:
        return failure("deadline_expired", "Control request expired before execution")
    response, submission = await run_sync(enqueue)
    if submission is None or not args.sync_run:
        return response

    from vibe import cli
    from vibe.authorization import invocation_authority

    authority, request_store, callback_session_id = submission
    run_id = response["payload"]["run"]["id"]
    try:
        # A synchronous CLI waits on this same request, but never holds a worker
        # while sleeping, so it cannot starve the turns it is waiting to finish.
        with invocation_authority(authority):
            run_payload = await cli._wait_for_run_result(request_store, run_id, wait_timeout=args.wait_timeout)
            if callback_session_id and cli._sync_run_detached(run_payload):
                await asyncio.to_thread(request_store.mark_callback_pending, run_id)
                run_payload["callback_status"] = "pending"
        response["payload"]["run"] = run_payload
        return response
    except Exception as exc:
        return result(await asyncio.to_thread(cli._task_error_payload, exc, help_command="vibe agent run --help"))
