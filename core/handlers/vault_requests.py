"""Warm Vault fetch transport; avault remains the only custody/egress boundary.

Fetch never waits or retries after an external handoff. Approval waiting is a
separate, effect-free request so input/output files stay owned by the caller.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import math
import threading
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from core.control_requests import ControlRequest, failure, result

HELP = "vibe vault fetch --help"


def _arguments(request: ControlRequest) -> Namespace:
    defaults = {
        "auth": "", "url": "", "method": "GET", "header": None,
        "body_b64": None, "input_error": None, "session_id": None,
        "caller_pid": None, "approval_wait": None, "no_approval_wait": False,
    }
    if request.operation == "vault.fetch.await":
        defaults = {"request_id": "", "timeout": 0.0, "session_id": None}
    if request.arguments.keys() - defaults.keys():
        raise ValueError("Unknown Vault fetch arguments")
    values = {**defaults, **request.arguments}
    for key in ("auth", "url", "method", "body_b64", "session_id", "request_id"):
        if key in values and values[key] is not None and not isinstance(values[key], str):
            raise ValueError("Invalid Vault fetch arguments")
    for key in ("approval_wait", "timeout"):
        value = values.get(key)
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            raise ValueError("Invalid Vault approval wait")
    if request.operation == "vault.fetch":
        headers = values["header"]
        error = values["input_error"]
        if (
            (headers is not None and (not isinstance(headers, list) or any(not isinstance(h, str) for h in headers)))
            or type(values["no_approval_wait"]) is not bool
            or type(values["caller_pid"]) is not int or values["caller_pid"] <= 0
            or (error is not None and (
                not isinstance(error, dict) or error.get("ok") is not False or error.get("kind") != "error"
                or any(not isinstance(error.get(k), str) for k in ("code", "error"))
            ))
        ):
            raise ValueError("Invalid Vault fetch arguments")
        try:
            values["body"] = base64.b64decode(values["body_b64"], validate=True) if values["body_b64"] is not None else None
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid Vault fetch body") from exc
    return Namespace(**values)


def _fetch(args: Namespace, engine) -> dict[str, Any]:
    """The former CLI policy/grant/egress path, without process-owned I/O."""
    from storage import vault_service
    from vibe import api, cli

    if args.input_error is not None:
        return result(args.input_error, execution="not_executed")
    name = args.auth
    grant = one_shot_grant = None
    handoff_started = False
    requester = {"source": "cli", "pid": args.caller_pid}
    delivery = {}
    if args.session_id:
        requester["session_id"] = args.session_id
    try:
        headers = cli._parse_headers(args.header)
        method = cli._vault_fetch_method(args.method)
        host = cli._vault_fetch_host(args.url)
        with engine.connect() as conn:
            record_meta = vault_service.get_secret_record_meta(conn, name)
        name = str(record_meta["record_name"])
        request = cli._build_vault_fetch_request(
            engine, name=name, url=args.url, host=host, method=method,
            headers=headers, body=args.body, help_command=HELP,
        )
        delivery = {"mode": "fetch", "host": host, "method": method}
        if args.session_id:
            delivery["session_id"] = args.session_id
        approval_wait = cli._vault_approval_wait_seconds(args, help_command=HELP)
        grant, one_shot_grant, sealed = cli._resolve_single_vault_delivery(
            engine, name, requester=requester, delivery=delivery, purpose="fetch",
        )
        identities = cli._capture_vault_delivery_identities(engine, [name], [one_shot_grant] if one_shot_grant else [])
        handoff_started = True
        if grant is not None:
            response = api.avault_agent_deliver_fetch(
                grant_id=grant["id"], name=name, sealed=sealed, request=request,
                context={"session_id": grant.get("session_id"), "purpose": "fetch"},
            )
        else:
            response = api.avault_deliver_fetch(name, sealed, request)
        cli._consume_after_possible_use([one_shot_grant] if one_shot_grant else [], reason="vault-fetch-one-shot", engine=engine)
        status = int(response.get("status") or 0)
        body = response.get("body") or ""
        try:
            with engine.begin() as conn:
                vault_service.record_proxy_use(
                    conn, identities[0], requester={"source": "cli", "pid": args.caller_pid},
                    delivery={"host": host, "method": method, "status": status},
                )
        except Exception:
            # Egress already happened; bookkeeping is never grounds for replay.
            pass
        return result({"schema_version": 1, "ok": 200 <= status <= 299,
                       "kind": "vault_fetch", "status": status,
                       "headers": response.get("headers") or {}, "body": body})
    except Exception as exc:
        grants = [one_shot_grant] if one_shot_grant else []
        execution = "execution_unknown" if handoff_started else "not_executed"
        if isinstance(exc, api.AvaultError):
            cli._finish_one_shot_after_avault_error(engine, grants, exc, reason="vault-fetch-one-shot")
            if isinstance(exc, api.AvaultPreHandoffError):
                execution = "not_executed"
            if isinstance(grant, dict) and cli._agent_missing_grant(exc) and grant.get("id"):
                cli._expire_agent_grant_after_missing(
                    engine, grant["id"], [name], requester=requester, delivery=delivery, purpose="fetch",
                )
                error = cli.TaskCliError("protected grant expired; approve the request again", code="approval_required", help_command=HELP)
            else:
                error = cli._avault_cli_error(exc, f"request failed: {exc}", code="request_failed", help_command=HELP)
        else:
            if handoff_started:
                cli._consume_after_possible_use(grants, reason="vault-fetch-one-shot", engine=engine)
            else:
                cli._release_one_shot_reservations(engine, grants)
            codes = (
                (vault_service.SecretNotFoundError, "secret_not_found"),
                (vault_service.KeypairNotValueDeliverableError, "keypair_not_value_deliverable"),
                (vault_service.InvalidFieldNameError, "invalid_field_name"),
                (vault_service.FieldsCorruptError, "fields_corrupt"),
                (vault_service.FieldNotFoundError, "field_not_found"),
                (vault_service.FieldsNotSupportedError, "fields_not_supported"),
                (vault_service.UnsupportedProtectionError, "protected_tier_unavailable"),
            )
            error = exc
            for error_type, code in codes:
                if isinstance(exc, error_type):
                    text = f"secret '{args.auth}' not found" if code == "secret_not_found" else str(exc)
                    error = cli.TaskCliError(text, code=code, help_command=HELP)
                    break
        payload = cli._task_error_payload(error, help_command=HELP)
        response = result(payload, execution=execution)
        # Only the explicit pre-handoff approval result permits continuation.
        if (
            not handoff_started and isinstance(exc, cli.TaskCliError)
            and exc.code == "approval_required" and (exc.details or {}).get("request_id")
            and approval_wait > 0
        ):
            response["approval"] = {"auth": name, "request_id": exc.details["request_id"], "timeout": approval_wait}
        if execution == "execution_unknown":
            payload["execution"] = execution
            payload.setdefault("hint", "The request may have executed. Do not retry blindly.")
        return response


async def _wait_for_approval(args: Namespace, engine, step, *, request: ControlRequest, cancelled) -> dict[str, Any]:
    """Existing waiter state/OS lock; cancellation never strands an owned lock."""
    from storage import vault_service
    from vibe import api, cli

    caller_env = request.caller_env
    request_id = args.request_id
    waiter_id = vault_service.new_request_waiter_id()
    lock = None
    terminal = True

    def arm():
        nonlocal lock, terminal
        from storage.lock import MigrationFileLock, _acquire_file_lock

        if cancelled.is_set():
            raise asyncio.CancelledError
        if request.expired:
            raise cli.TaskCliError("Control request expired before execution", code="deadline_expired", help_command=HELP)
        # This new RPC accepts a request ID rather than receiving one directly
        # from this call's resolver. Apply the existing request read checks first.
        with engine.connect() as conn:
            approval_request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
            if approval_request.get("request_type") != "access" or approval_request.get("delivery", {}).get("mode") != "fetch":
                raise cli.TaskCliError("Not a Vault fetch approval request", code="invalid_request", help_command=HELP)
        # Ordinary MigrationFileLock.acquire is thread-reentrant and MUST be
        # released on its acquiring thread. A descriptor-adopted lock has no
        # RLock affinity and keeps exactly the same OS lock/probe/path contract.
        lock = MigrationFileLock(vault_service.request_waiter_lock_path(engine.url.database, waiter_id), timeout_seconds=0)
        lock.adopt_locked_handle(_acquire_file_lock(lock.lock_path, time.monotonic()))
        with engine.begin() as conn:
            vault_service.arm_request_waiter(
                conn, request_id, waiter_id=waiter_id,
                deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=args.timeout)).isoformat(),
                liveness=vault_service.WAITER_LIVENESS_LOCK,
            )
        terminal = False

    def poll():
        with engine.begin() as conn:
            request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
            value = api._vault_request_result(conn, request) if request.get("status") == "approved" else None
            if request.get("status") in {"approved", "denied", "expired", "failed", "fulfilled"}:
                if cli._vault_access_delivery_ready(request, value):
                    return request
        return None

    def finish(*, timeout=False):
        nonlocal terminal
        from storage.agent_session_rows import reserve_write_lock

        with engine.begin() as conn:
            if not timeout:
                # A disconnect observed while queued or waiting for SQLite wins
                # before ownership completion, not after its already-landed commit.
                reserve_write_lock(conn)
                if cancelled.is_set():
                    raise asyncio.CancelledError
            operation = vault_service.timeout_request_waiter if timeout else vault_service.complete_request_waiter
            operation(conn, request_id, waiter_id=waiter_id)
        terminal = True

    try:
        await step(arm)
        deadline = time.monotonic() + args.timeout
        while True:
            approval_request = await step(poll)
            if approval_request is not None:
                await step(finish)
                error = cli._vault_terminal_request_error(approval_request, help_command=HELP)
                if error:
                    raise error
                return result({"schema_version": 1, "ok": True, "kind": "vault_fetch_approved"}, execution="not_executed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    await step(lambda: finish(timeout=True))
                except vault_service.ContinuationClaimedError:
                    terminal = True
                except Exception:
                    cli.logger.debug("failed to mark vault request waiter timed out", exc_info=True)
                raise cli.TaskCliError(
                    f"Vault approval request '{request_id}' is still waiting for the user",
                    code="approval_wait_timeout", hint=cli._approval_wait_timeout_hint(args, request_id, caller_env=caller_env),
                    help_command=HELP, details={"request_id": request_id, "timeout_seconds": args.timeout,
                                              "callback_expected": cli._approval_wait_callback_expected(args, caller_env=caller_env)},
                )
            await asyncio.sleep(min(2.0, remaining))
    except vault_service.ContinuationClaimedError:
        terminal = True
        raise cli._vault_approval_handed_off_error(args, request_id, help_command=HELP, caller_env=caller_env)
    finally:
        if lock is not None:
            # Cancellation leaves the row active and the lock free. The existing
            # callback sweeper recognizes the dead waiter; no fetch follows it.
            await step(lambda: vault_service.release_request_waiter_lock(lock, discard=terminal))


async def handle_vault_control_request(
    request: ControlRequest, *, run_sync: Callable[[Callable[[], Any]], Awaitable[Any]],
) -> dict[str, Any]:
    from core.caller_context import caller_context_from_env
    from core.runtime_work import land_step
    from storage.db import get_cached_sqlite_engine
    from vibe import cli
    from vibe.authorization import invocation_authority

    try:
        args = await asyncio.to_thread(_arguments, request)
    except ValueError as exc:
        return failure("invalid_control_request", str(exc))
    authority = None
    cancellation = threading.Event()

    async def step(operation):
        def invoke():
            with invocation_authority(authority):
                return operation()
        landed, cancelled = await land_step(run_sync(invoke), on_cancel=cancellation.set)
        value = landed.result()
        if cancelled:
            raise asyncio.CancelledError
        return value

    def admit():
        nonlocal authority
        if request.expired:
            return failure("deadline_expired", "Control request expired before execution")
        authority = cli._cli_invocation_authority(request.caller_env)
        with invocation_authority(authority):
            cli._require_command_path_admission(("vault", "fetch"))
        context = caller_context_from_env(request.caller_env)
        args.session_id = (args.session_id or "").strip() or cli._default_session_id_from_caller(context)
        if request.expired:
            return failure("deadline_expired", "Control request expired before execution")
        return None

    dispatched = False

    def fetch():
        nonlocal dispatched
        if request.expired:
            return failure("deadline_expired", "Control request expired before execution")
        engine = get_cached_sqlite_engine()
        dispatched = True
        return _fetch(args, engine)

    try:
        denied = await step(admit)
        if denied is not None:
            return denied
        if request.operation == "vault.fetch":
            # One worker owns all reservation/egress/finalization steps even if
            # the requesting socket disappears. No cancellation can replay it.
            return await step(fetch)
        engine = await step(get_cached_sqlite_engine)
        return await _wait_for_approval(args, engine, step, request=request, cancelled=cancellation)
    except Exception as exc:
        if dispatched:
            return failure("execution_unknown", "Vault fetch failed without a confirmed result; do not replay it", execution="execution_unknown")
        return result(cli._task_error_payload(exc, help_command=HELP), execution="not_executed")
