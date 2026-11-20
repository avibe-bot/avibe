"""Controller-owned card use, with a durable fence and user-only reconciliation."""

from __future__ import annotations

import asyncio
import re

from .card_fences import FenceUnavailable, KINDS
from .reset_cards import VENDORS


async def _account(service, source):
    try:
        key = await service.adapter.reset_card_account_key(source.source_id, source.vendor, source.credential_ref)
    except Exception:
        return None
    return key if isinstance(key, str) and re.fullmatch("[a-f0-9]{64}", key) else None


def _decision(kind, reply, inventory):
    result = dict(kind=kind, outcome="unconfirmed", reason=None, cleared=[])
    if not isinstance(reply, dict) or inventory.get("state") != "ok":
        return result
    before, statement = reply.get("before"), reply.get("statement")
    if type(before) is not int or before <= 0:
        return result
    after = next((card["count"] for card in inventory["cards"] if card["kind"] == kind), 0)
    if statement == "ack" and after < before:
        allowed = ("five_hour", "seven_day", "seven_day_overage_included")
        result.update(outcome="used", cleared=[v for v in allowed if v in reply.get("cleared", [])])
    elif statement in ("not_limited", "cooldown", "refused", "unavailable") and after == before:
        result.update(outcome="not_used", reason=statement)
    return result


async def use(service, source_id, payload):
    from .service import ModelHubError

    if (
        not isinstance(payload, dict)
        or set(payload) != {"kind"}
        or not isinstance(payload["kind"], str)
        or payload["kind"] not in KINDS
    ):
        raise ModelHubError("invalid_parameter", status=400)
    # Remember only claims which overlapped an unresolved press. Two aliases of
    # the same account can finish their identity GETs in the opposite order.
    epoch = service._card_epoch
    kind = payload["kind"]
    if source_id in service._card_operations:
        raise ModelHubError("reset_card_locked", status=409)

    async def run():
        sources = await asyncio.to_thread(service._quota_sources)
        source = next((s for s in sources if s.source_id == source_id and s.vendor in VENDORS), None)
        key = await _account(service, source) if source else None
        if not key:
            raise ModelHubError("reset_card_unavailable", status=409)
        if service._card_epochs.get(key, 0) > epoch:
            raise ModelHubError("reset_card_locked", status=409)
        service._card_epoch += 1
        service._card_epochs[key] = service._card_epoch
        try:
            request_id = await asyncio.to_thread(service.card_fences.claim, key, kind, service.now())
        except FenceUnavailable:
            raise ModelHubError("reset_card_locked", status=409) from None
        service._card_pending.pop(source_id, None)
        finalized = False
        try:
            try:
                reply = await service.adapter.use_reset_card(
                    source_id, source.vendor, source.credential_ref, kind, request_id, key
                )
            except Exception:
                reply = {}
            if isinstance(reply, dict) and reply.get("statement") == "no_card":
                await asyncio.to_thread(service.card_fences.finish, key, request_id, unknown=False)
                finalized = True
                raise ModelHubError("reset_card_unavailable", status=409)
            fresh = await service.quota.reread(source)
            same_account = await _account(service, source) == key
            decision = _decision(kind, reply, fresh.get("reset_cards", {}) if same_account else {})
            await asyncio.to_thread(
                service.card_fences.finish, key, request_id, unknown=decision["outcome"] == "unconfirmed"
            )
            finalized = True
            if same_account and fresh:
                await service._resume_accounts_with_quota({"sources": [fresh]})
            return {"quota": await service.quota_summary(), "use": decision}
        finally:
            if not finalized:
                try:
                    await asyncio.to_thread(service.card_fences.finish, key, request_id, unknown=True)
                except FenceUnavailable:
                    pass  # Store is fail-closed.

    # Shield the whole transaction, including the claim, against a dropped browser connection.
    service._card_pending[source_id] = dict(reason="in_flight", kind=kind, since=service.now().isoformat())
    task = asyncio.create_task(run())
    service._card_operations[source_id] = task

    def finished(done):
        service._card_operations.pop(source_id, None)
        service._card_pending.pop(source_id, None)
        if not service._card_operations:
            service._card_epochs.clear()
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def refresh_unknown(service, sources):
    if not await asyncio.to_thread(service.card_fences.has_locks):
        return
    for source in sources:
        if source.vendor not in VENDORS:
            continue
        key = await _account(service, source)
        token = await asyncio.to_thread(service.card_fences.refresh_token, key) if key else None
        if token:
            # The generation must predate this particular vendor request. Cached, timer and
            # already-running reads never participate, even if their response arrives later.
            fresh = await service.quota.reread(source)
            if fresh.get("reset_cards", {}).get("state") == "ok" and await _account(service, source) == key:
                await asyncio.to_thread(service.card_fences.confirm_read, key, token)


async def overlay(service, sources, summary):
    durable = await asyncio.to_thread(service.card_fences.has_locks)
    if not durable and not service._card_pending:
        return
    refs = {src.source_id: src for src in sources}
    for row in summary["sources"]:
        if "reset_cards" not in row:
            continue
        pending = service._card_pending.get(row["source_id"])
        if pending or not durable:
            row["reset_cards"] = {**row["reset_cards"], "lock": pending}
            continue
        key = await _account(service, refs[row["source_id"]])
        kind = "all" if row["vendor"] == "anthropic" else "account"
        lock = (
            await asyncio.to_thread(service.card_fences.public, key, kind)
            if key
            else service.card_fences.unresolved_identity(kind)
        )
        row["reset_cards"] = {**row["reset_cards"], "lock": lock}
