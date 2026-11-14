"""Warm execution adapter for the CLI's existing mutation semantics.

The private channel carries invocation inputs, not ambient process globals.
Only named operations are supported; this is not an arbitrary CLI/SQL endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import math
import threading
from argparse import Namespace
from typing import Any, Awaitable, Callable
from uuid import uuid4

from core.control_requests import OPERATIONS, ControlRequest, failure, result

logger = logging.getLogger(__name__)

_AGENT_RUN_STRINGS = frozenset({
    "agent", "bot", "session_id", "fork_session", "scope_id", "project", "visibility",
    "model", "reasoning_effort", "speed", "cwd", "callback_session_id", "message", "message_file",
})
_AGENT_RUN_BOOLS = frozenset({
    "queue", "fork_self", "same_scope", "callback", "no_callback", "no_reply", "close_after", "sync_run", "json",
})

#: The ``help_command`` each command's own error printer adds to an error that names none.
_HELP_COMMANDS = {
    "task.add": "vibe task add --help",
    "task.update": "vibe task update --help",
    "watch.add": "vibe watch add --help",
    "watch.update": "vibe watch update --help",
    "session.update": "vibe session update --help",
    "session.send-now": "vibe session send-now --help",
    "session.queue.remove": "vibe session queue remove --help",
}
_NON_FINITE = {"inf", "-inf", "nan"}
#: How long a CLI route check waits for Model Hub's menu before the request fails.
_HUB_MENU_READ_SECONDS = 15.0


def _agent_run_arguments(arguments: dict[str, Any]) -> Namespace:
    defaults = {**dict.fromkeys(_AGENT_RUN_STRINGS), **dict.fromkeys(_AGENT_RUN_BOOLS, False), "wait_timeout": None}
    allowed = {*defaults, "command", "agent_command", "message_input_error", "invocation_home"}
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
    if values["message_file"] is not None:
        raise ValueError("Message files must be read by the invoking client")
    input_error = values.get("message_input_error")
    if input_error is not None and (
        not isinstance(input_error, dict)
        or any(not isinstance(input_error.get(key), str) for key in ("code", "error"))
        or any(input_error.get(key) is not None and not isinstance(input_error[key], str)
               for key in ("hint", "example", "help_command"))
        or (input_error.get("details") is not None and not isinstance(input_error["details"], dict))
    ):
        raise ValueError("Invalid message input error")
    wait = values["wait_timeout"]
    if wait is not None and (type(wait) not in {int, float} or not math.isfinite(wait)):
        raise ValueError("Invalid Agent Run wait timeout")
    if values.get("invocation_home") is not None and not isinstance(values["invocation_home"], str):
        raise ValueError("Invalid invocation home")
    return Namespace(**values)


@functools.lru_cache(maxsize=None)
def _command_schema(path: tuple[str, ...]) -> tuple[dict[str, str], dict[str, list], dict[str, Any]]:
    """``(command words, actions by dest, defaults)`` of one command's own parser leaf."""
    from vibe import cli

    parser = cli.build_parser()
    words: dict[str, str] = {}
    for word in path:
        subparsers = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
        words[subparsers.dest] = word
        parser = subparsers.choices[word]
    actions: dict[str, list] = {}
    defaults: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction) or action.dest == argparse.SUPPRESS:
            continue
        actions.setdefault(action.dest, []).append(action)
        defaults.setdefault(action.dest, action.default)
    for dest, value in parser._defaults.items():
        # ``set_defaults`` landing slots: the lifted ``-- <command ...>`` tails.
        actions.setdefault(dest, [])
        defaults.setdefault(dest, value)
    return words, actions, defaults


def _is_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _checked_value(dest: str, actions: list, value: Any) -> Any:
    """The parser's value for ``dest``, or ``ValueError`` when the parser could not produce it."""
    if not actions:
        if value is None or _is_strings(value):
            return value
    elif all(isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)) for action in actions):
        if type(value) is bool:
            return value
    elif all(isinstance(action, argparse._StoreConstAction) for action in actions):
        if value is None or any(value == action.const for action in actions):
            return value
    elif len(actions) == 1:
        action = actions[0]
        if isinstance(action, argparse._AppendAction) and action.type is int:
            if value is None or (isinstance(value, list) and all(type(item) is int for item in value)):
                return value
        elif isinstance(action, argparse._StoreAction) and action.nargs == argparse.REMAINDER:
            if value is None or _is_strings(value):
                return value
        elif isinstance(action, argparse._StoreAction) and action.nargs is None:
            if value is None and action.option_strings and action.default is None:
                return value
            if action.type is None and isinstance(value, str):
                if not action.choices or value in action.choices:
                    return value
            if action.type is float:
                if isinstance(value, str) and value in _NON_FINITE:
                    return float(value)
                if type(value) in {int, float} and math.isfinite(value):
                    return value
            if action.type is int and type(value) is int:
                return value
    raise ValueError(f"Invalid argument: {dest}")


def _command_arguments(operation: str, path: tuple[str, ...], arguments: dict[str, Any]) -> Namespace:
    words, actions, defaults = _command_schema(path)
    extras = {"message_file_input"} if "message_file" in actions else set()
    if operation == "task.add":
        extras.add("invocation_timezone")
    if "cwd" in actions:
        extras.add("invocation_home")
    unknown = arguments.keys() - {*words, *actions, *extras}
    if unknown:
        raise ValueError(f"Unknown arguments: {', '.join(sorted(unknown))}")
    if any(arguments.get(dest, word) != word for dest, word in words.items()):
        raise ValueError("Arguments name a different command")
    values = {**words, **defaults, **{dest: arguments[dest] for dest in actions if dest in arguments}}
    # Every final value, a default included: an omitted required id is no id.
    for dest in actions:
        values[dest] = _checked_value(dest, actions[dest], values[dest])
    if "message_file_input" in extras:
        # Preparation is caller-owned and precedes service checks. A command may
        # reject before consuming it; the service must never open the caller path.
        materialized = arguments.get("message_file_input")
        needed = values["message"] is None and bool(values["message_file"])
        if needed != (materialized is not None) or (
            materialized is not None and (
                not isinstance(materialized, dict)
                or len(materialized) != 1
                or not isinstance(next(iter(materialized.values())), str)
                or next(iter(materialized)) not in {"text", "os_error", "error"}
            )
        ):
            raise ValueError("Message files must be read by the invoking client")
        values["message_file_input"] = materialized
    if "invocation_timezone" in extras:
        zone = arguments.get("invocation_timezone")
        if (not values["timezone"]) != isinstance(zone, str) or (zone is not None and not isinstance(zone, str)):
            raise ValueError("The invocation timezone must come from the invoking client")
        values["invocation_timezone"] = zone
    if "invocation_home" in extras:
        home = arguments.get("invocation_home")
        if home is not None and not isinstance(home, str):
            raise ValueError("Invalid invocation home")
        values["invocation_home"] = home
    return Namespace(**values)


def _bound_hub_agent_reader(controller, loop: asyncio.AbstractEventLoop):
    stores = _warm_stores(controller)
    stores.bind_loop(loop)
    return stores.hub_agent_reader


class WarmCliStores:
    """What a service-owned CLI command reads and writes through, never rebuilt per request.

    Definition, execution and Agent stores are the controller's own. A mutation
    publishes the in-process ``definitions.updated`` hint and the runtime-work
    lanes reconcile from these same mirrors whatever any reader's change probe
    reported, so nothing waits for a second mirror to notice. Each request
    refreshes the mirrors first, so a commit made by another process (the Web UI)
    is seen exactly as a fresh CLI process saw it. The definition projection
    reader is this channel's one addition: no controller store serves the
    public projection the CLI prints.
    """

    def __init__(self, controller: Any):
        self._controller = controller
        self._lock = threading.Lock()
        self._reader: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def task_store(self):
        store = self._controller.scheduled_task_service.store
        store.maybe_reload()
        return store

    @property
    def watch_store(self):
        store = self._controller.watch_service.store
        store.maybe_reload()
        return store

    @property
    def watch_runtime_store(self):
        return self._controller.watch_service.runtime_store

    @property
    def request_store(self):
        return self._controller.scheduled_task_service.request_store

    @property
    def agent_store(self):
        return self._controller.vibe_agent_store

    @property
    def definition_reader(self):
        from vibe import cli

        with self._lock:
            if self._reader is None:
                self._reader = cli._definition_reader()
            return self._reader

    @property
    def session_engine(self):
        from storage.db import get_cached_sqlite_engine
        from vibe import cli

        cli._ensure_cli_sqlite_state()
        return get_cached_sqlite_engine()

    @property
    def session_turns(self):
        return getattr(self._controller, "session_turns", None)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """The controller loop, which owns Model Hub's recovery state; set by each request on that loop."""
        self._loop = loop

    @property
    def hub_agent_reader(self):
        """Model Hub's Agent payload, the route menu the Web picker reads; None without a Model Hub service.

        The command's synchronous work runs off the loop, but the payload reads recovery state the loop owns,
        so the read is ``read_agent_sources`` on the loop, which offloads only the payload construction.
        """
        service = getattr(self._controller, "model_hub_service", None)
        loop = self._loop
        if service is None or loop is None:
            return None

        def read(backend: str):
            future = asyncio.run_coroutine_threadsafe(service.read_agent_sources(backend), loop)
            return future.result(timeout=_HUB_MENU_READ_SECONDS)

        return read

    def close(self) -> None:
        with self._lock:
            reader, self._reader = self._reader, None
        if reader is not None:
            reader.close()


_STORES_LOCK = threading.Lock()


def _warm_stores(controller: Any) -> WarmCliStores:
    stores = getattr(controller, "cli_control_stores", None)
    if stores is None:
        with _STORES_LOCK:
            stores = getattr(controller, "cli_control_stores", None)
            if stores is None:
                stores = controller.cli_control_stores = WarmCliStores(controller)
    return stores


async def handle_control_request(
    controller: Any,
    payload: object,
    *,
    run_sync: Callable[[Callable[[], Any]], Awaitable[Any]] = asyncio.to_thread,
) -> dict[str, Any]:
    try:
        request = ControlRequest.from_payload(payload)
        if request.operation == "vault.generate":
            from core.handlers.vault_generation import handle_generate_control_request

            return await handle_generate_control_request(request, run_sync=run_sync)
        if request.operation in {"vault.fetch", "vault.fetch.await"}:
            from core.handlers.vault_requests import handle_vault_control_request

            return await handle_vault_control_request(request, run_sync=run_sync)
        path = OPERATIONS.get(request.operation)
        if path is None:
            return failure("unsupported_operation", "This service does not support the requested operation")
        args = _agent_run_arguments(request.arguments) if request.operation == "agent.run" else None
    except ValueError as exc:
        return failure("invalid_control_request", str(exc))
    if request.expired:
        return failure("deadline_expired", "Control request expired before execution")
    if request.operation == "agent.run":
        return await _agent_run(controller, request, args, run_sync)
    return await _command(controller, request, path, run_sync)


async def _command(controller, request: ControlRequest, path, run_sync) -> dict[str, Any]:
    help_command = _HELP_COMMANDS.get(request.operation)
    loop = asyncio.get_running_loop()

    # Argument decoding (the parser, its import), admission, store work and its
    # first effects all run off the loop. The second deadline check is inside the
    # worker, after any executor queue wait. An operation that still has to wait
    # on other work hands back a continuation rather than holding this worker.
    def start():
        if request.expired:
            return failure("deadline_expired", "Control request expired before execution"), None
        try:
            args = _command_arguments(request.operation, path, request.arguments)
        except ValueError as exc:
            return failure("invalid_control_request", str(exc)), None
        from vibe import cli
        from vibe.authorization import invocation_authority

        try:
            authority = cli._cli_invocation_authority(request.caller_env)
            with invocation_authority(authority):
                try:
                    cli._require_command_path_admission(path)
                except cli.TaskCliError as exc:
                    return result(cli._task_error_payload(exc), execution="not_executed"), None
                if request.expired:
                    return failure("deadline_expired", "Control request expired before execution"), None
                stores = _warm_stores(controller)
                stores.bind_loop(loop)
                invocation = cli._ServiceInvocation(
                    caller_env=request.caller_env,
                    cwd=request.cwd,
                    stores=stores,
                    home=getattr(args, "invocation_home", None),
                )
                outcome = cli._SERVICE_COMMANDS[request.operation](args, invocation)
                if callable(outcome):
                    return None, (authority, outcome)
                return result(outcome), None
        except Exception as exc:
            return result(_error_payload(cli, request, exc, help_command)), None

    response, pending = await run_sync(start)
    if pending is None:
        return response
    from vibe import cli
    from vibe.authorization import invocation_authority

    authority, finish = pending
    with invocation_authority(authority):
        try:
            return result(await finish())
        except Exception as exc:
            return result(await asyncio.to_thread(_error_payload, cli, request, exc, help_command))


def _error_payload(cli, request: ControlRequest, exc: Exception, help_command) -> dict[str, Any]:
    """The command's own error payload; an unexpected failure also keeps its traceback.

    The direct command let some of these escape as a traceback. The caller now
    gets the generic structured error, and the service log keeps the traceback,
    naming only the operation: arguments can carry message text.
    """
    payload = cli._task_error_payload(exc, help_command=help_command)
    if payload.get("code") == "task_command_failed":
        logger.error("CLI control operation %s failed unexpectedly", request.operation, exc_info=exc)
    return payload


async def _agent_run(controller, request: ControlRequest, args, run_sync) -> dict[str, Any]:
    if not args.sync_run:
        return await _agent_run_request(controller, request, args, run_sync, sync_run_id=None)
    service = controller.scheduled_task_service
    sync_run_id = uuid4().hex[:12]
    service.sync_waiting_run_ids = frozenset((*getattr(service, "sync_waiting_run_ids", ()), sync_run_id))
    try:
        return await _agent_run_request(controller, request, args, run_sync, sync_run_id=sync_run_id)
    finally:
        service.sync_waiting_run_ids = service.sync_waiting_run_ids - {sync_run_id}


async def _agent_run_request(controller, request: ControlRequest, args, run_sync, *, sync_run_id: str | None) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
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
                    agent_store=agent_store, request_store=request_store, run_id=sync_run_id,
                    hub_agent_reader=_bound_hub_agent_reader(controller, loop),
                )
                return result(response), (authority, request_store, callback_session_id)
        except Exception as exc:
            return result(cli._task_error_payload(exc, help_command="vibe agent run --help")), None

    from core.runtime_work import land_step

    cancelled = False
    if args.sync_run:
        # The client can disappear during enqueue. Land that admission before
        # handing off its callback, rather than orphaning a thread's committed Run.
        landed, cancelled = await land_step(run_sync(enqueue))
        response, submission = landed.result()
    else:
        response, submission = await run_sync(enqueue)
    if submission is None or not args.sync_run:
        if cancelled:
            raise asyncio.CancelledError
        return response

    from vibe import cli
    from vibe.authorization import invocation_authority

    authority, request_store, callback_session_id = submission
    run_id = response["payload"]["run"]["id"]
    terminal_read = False

    def on_terminal_read():
        nonlocal terminal_read
        terminal_read = True

    try:
        # A synchronous CLI waits on this same request, but never holds a worker
        # while sleeping, so it cannot starve the turns it is waiting to finish.
        with invocation_authority(authority):
            if cancelled:
                raise asyncio.CancelledError
            run_payload = await cli._wait_for_run_result(
                request_store, run_id, wait_timeout=args.wait_timeout, on_terminal_read=on_terminal_read,
            )
            if callback_session_id and cli._sync_run_detached(run_payload):
                await asyncio.to_thread(request_store.mark_callback_pending, run_id)
                run_payload["callback_status"] = "pending"
        response["payload"]["run"] = run_payload
        return response
    except asyncio.CancelledError:
        # A disconnected/cancelled waiter has detached just like a timeout. If
        # it already read the terminal result, its receipt thread can still mark
        # the pair answered: deliver that requested sync return even on success.
        # Otherwise keep opt-in/failure-only routing. Cleanup must land despite
        # another cancellation while the handoff write is in flight.
        if callback_session_id:
            landed, _ = await land_step(asyncio.to_thread(
                request_store.mark_callback_pending, run_id, deliver_result=terminal_read,
            ))
            try:
                landed.result()
            except Exception:
                logger.warning("Failed to hand off cancelled sync Run %s", run_id, exc_info=True)
        raise
    except Exception as exc:
        return result(await asyncio.to_thread(cli._task_error_payload, exc, help_command="vibe agent run --help"))
