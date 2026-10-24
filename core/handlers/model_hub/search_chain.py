"""Controller-owned mixed search chain and producer of service attempts.

The turn freezes choices, not runtime health. Every slot is classified on the
current snapshot; a service rechecks that snapshot immediately before writing
request bytes. Settlement owns observed status even when the caller stops.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from config.v2_config import (
    KEYLESS_EXA_ACTIVE,
    KEYLESS_EXA_SOURCE_ID,
    ModelHubSourceStateConfig,
    is_keyless_exa_source,
    model_hub_keyless_exa_source,
    model_hub_search_service,
)

from .adapter import SearchResponse
from .async_owner import await_owned_task
from .capability_evidence import identity_digest, utc_text
from .hosted_executor import ExecutorCall, Failure, call_timeout, failed_record
from .hosted_turn import ExecutorSlot, TurnHostedTools
from .resolver import parse_model_hub_timestamp
from .retry import source_identity
from .search_services import ATTEMPT_SECONDS, ZERO, map_response, search_request

if TYPE_CHECKING:
    from .service import ModelHubService


class _NotAdmitted(Exception):
    pass


class SearchChain:
    def __init__(self, owner: ModelHubService, call: ExecutorCall, turn: TurnHostedTools, say: Callable[..., str]):
        self.owner, self.call, self.turn, self.say = owner, call, turn, say
        self.deadline = asyncio.get_running_loop().time() + call_timeout(call.tool)
        self.facts: list[dict[str, Any]] = []
        self.tried: list[Any] = []
        self.query_limits: list[int] = []
        self.with_facts = any(slot.kind == "service" for slot in turn.executors.get(call.tool, ()))

    async def classify(self, slot: ExecutorSlot, *, locked: bool = False):
        if not locked:
            async with self.owner._mutation_lock:
                return await self.classify(slot, locked=True)
        config = await asyncio.to_thread(self.owner.store.load)
        # Recovery owns live half-open reservations on this loop. Its annotation
        # step is memory-only; never call recovery_annotations(), which reloads
        # the config, or move the mutable coordinator to a worker.
        annotations = self.owner.recovery.annotations(config)
        return await asyncio.to_thread(self._classify, slot, config, annotations)

    def _classify(self, slot: ExecutorSlot, config, annotations):
        """First matching skip only: unavailable, disabled, blocked, cooling, query length.

        No mutation: in particular a skipped slot neither reserves a trial nor
        advances any fence. Runs on a worker over the final-admission snapshot.
        """
        owner = self.owner
        source = (
            model_hub_keyless_exa_source()
            if slot.source_id == KEYLESS_EXA_SOURCE_ID and KEYLESS_EXA_ACTIVE
            else next((s for s in config.sources if s.id == slot.source_id), None)
        )
        fact = {"source_id": slot.source_id, "label": slot.label or slot.source_id, "kind": slot.kind, "outcome": "skipped"}
        unavailable = source is None or identity_digest(source) != slot.identity
        service = None
        if not unavailable:
            fact["label"] = source.display_name[:64]
            if slot.kind == "service":
                service = model_hub_search_service(source)
                unavailable = service is None or service["service_id"] != slot.model_id or (
                    not slot.source_id == KEYLESS_EXA_SOURCE_ID and not source.credential_ref
                )
            else:
                unavailable = owner._admitted_executor(slot, self.call.tool) is None
        if unavailable:
            fact["skipped"] = "unavailable"
        elif not slot.enabled:
            fact["skipped"] = "disabled"
        elif source.state.status in {"needs_action", "error"} and not (
            slot.kind == "service" and owner.search_reenable_trial_armed(source.id)
        ):
            fact.update(skipped="blocked", needs_action=source.state.detail_key or "models.source.error.unclassified")
        else:
            now = owner.now()
            deadlines = []
            if service is not None:
                until = owner._service_cooldowns.until(source, slot.model_id, now)
                if until is not None:
                    deadlines.append(until)
            if source.state.status == "cooldown" and source.state.retry_at:
                until = parse_model_hub_timestamp(source.state.retry_at)
                if until > now:
                    deadlines.append(until)
            # Model Sources also honor the live recovery coordinator, including
            # its retired persisted cooldowns and in-flight half-open requests.
            if source.protocol != "search":
                annotation = annotations.get(source.id)
                if annotation is not None:
                    if annotation.retired_cooldown == (source.state.retry_at, source.state.detail_key):
                        deadlines = [d for d in deadlines if service is not None and d == owner._service_cooldowns.until(source, slot.model_id, now)]
                    if annotation.retry_at:
                        deadlines.append(parse_model_hub_timestamp(annotation.retry_at))
                    elif annotation.in_flight:
                        fact["skipped"] = "unavailable"
            if "skipped" not in fact:
                if deadlines:
                    fact.update(skipped="cooling", retry_at=utc_text(max(deadlines)))
                elif service is not None and (limit := service["limits"].get("query_max_chars")) is not None and len(self.call.arguments["query"]) > limit:
                    fact["skipped"] = "query_too_long"
                else:
                    return source, service, None
        return source, service, fact

    async def run(self) -> dict:
        from .service import _cost_payload

        slots = self.turn.executors.get(self.call.tool, ())
        for slot in slots:
            if asyncio.get_running_loop().time() >= self.deadline:
                break
            source, catalog, skip = await self.classify(slot)
            if skip is not None:
                self.facts.append(skip)
                if skip["skipped"] == "query_too_long":
                    self.query_limits.append(catalog["limits"]["query_max_chars"])
                continue
            if slot.kind == "service":
                attempt, skip = await self.service_attempt(slot, source, catalog)
            else:
                final_skip = None

                async def admitted():
                    nonlocal final_skip
                    final, _, final_skip = await self.classify(slot, locked=True)
                    return final if final_skip is None else None

                attempt = await self.owner._executor_attempt(
                    self.call, slot, deadline=self.deadline, say=self.say, admission=admitted,
                )
                skip = final_skip
            if attempt is None:
                if skip is None:
                    _, _, skip = await self.classify(slot)
                self.facts.append(skip or {
                    "source_id": slot.source_id, "label": slot.label or slot.source_id,
                    "kind": slot.kind, "outcome": "skipped", "skipped": "unavailable",
                })
                continue
            self.tried.append(attempt)
            fact = {
                "source_id": attempt.source.id, "label": attempt.source.display_name[:64], "kind": slot.kind,
                "outcome": "completed" if attempt.record["status"] == "completed" else "failed",
            }
            if fact["outcome"] == "failed" and attempt.record.get("error_code"):
                fact["error_code"] = attempt.record["error_code"]
            cost = attempt.cost if attempt.billable or slot.kind == "service" else ZERO
            if cost is not None:
                fact["cost"] = _cost_payload(cost)
            if attempt.superseded and fact["outcome"] == "failed":
                fact["superseded"] = True
            self.facts.append(fact)
            if not attempt.moves_on:
                break
        last = self.tried[-1] if self.tried else None
        record = last.record if last is not None else None
        if last is None or last.record["status"] == "failed":
            reason = self.terminal_reason()
            if self.tried:
                names = ", ".join(dict.fromkeys(attempt.source.display_name for attempt in self.tried))
                reason = reason.also(self.say("modelHub.hostedTools.triedSources", sources=names))
            if not slots:
                from .hosted_tools import refused_executors
                from .service import hosted_tool_spec

                current = await asyncio.to_thread(self.owner.store.load)
                refused = await asyncio.to_thread(
                    refused_executors, current, self.call.tool,
                    evidence=self.owner.hosted_evidence(), spec_for=hosted_tool_spec,
                )
                if refused:
                    names = self.say("modelHub.hostedTools.listSeparator").join(source.display_name for source in refused)
                    reason = Failure(self.say("modelHub.hostedTools.noExecutor.web_search.refused", sources=names), None)
            record = failed_record(reason, last.record.get("sources") if last is not None else None)
        if self.with_facts:
            record = {**record, "attempts": self.facts}
        answer = {"record": record}
        if last is not None:
            execution = last.execution()
            earlier = [attempt.failed_attempt() for attempt in self.tried[:-1] if attempt.billable]
            if earlier:
                execution["failed_attempts"] = earlier
            answer["execution"] = execution
        return answer

    def terminal_reason(self) -> Failure:
        if self.query_limits:
            return Failure(self.say("modelHub.hostedTools.queryLimit", limit=str(max(self.query_limits))), "search_query_too_long")
        query = next((a for a in self.tried if a.record.get("error_code") == "service_refused"), None)
        if query is not None:
            return Failure(query.record["error"], "service_refused")
        if any(f.get("skipped") == "blocked" for f in self.facts) or (self.facts and all(f.get("skipped") == "disabled" for f in self.facts)):
            return Failure(self.say("hostedTools.errorCode.search_sources_blocked"), "search_sources_blocked")
        if self.tried:
            record = self.tried[-1].record
            return Failure(record["error"], record.get("error_code"))
        if self.turn.executors.get(self.call.tool):
            return Failure(self.say("hostedTools.errorCode.service_unavailable"), "service_unavailable")
        return Failure(self.say("modelHub.hostedTools.noSearchSource"), None)

    async def service_attempt(self, slot, source, catalog):
        if is_keyless_exa_source(source):
            async with self.owner._keyless_exa_attempt_lock:
                return await self._service_attempt_locked(slot, source, catalog)
        return await self._service_attempt_locked(slot, source, catalog)

    async def _service_attempt_locked(self, slot, source, catalog):
        from .service import _ExecutorAttempt

        owner = self.owner
        request = await asyncio.to_thread(search_request, catalog, self.call.arguments["query"], slot.results)
        sent = False
        health_generation = cooldown_generation = trial_epoch = None
        response = None
        skip = None
        timed_out = False
        stopped = False
        ended_at = None

        async def invoke():
            nonlocal response, ended_at, source, skip, health_generation, cooldown_generation
            async with owner._mutation_lock:
                current, _, skip = await self.classify(slot, locked=True)
                if skip is not None:
                    return None
                source = current
                waiting_trial = source.state.status in {"needs_action", "error"}
                waiting_epoch = owner._search_trial_epochs.get(source.id, 0)
                if waiting_trial:
                    # Connecting contenders have not yet won the one request.
                    # They share the re-enable fence; a skipped contender must
                    # never invalidate the winner's eventual settlement.
                    health_generation = owner._latest_source_attempt_generation.get(source.id)
                    cooldown_generation = owner._service_cooldowns.generation(source, slot.model_id)
                else:
                    health_generation = None if is_keyless_exa_source(source) else owner._reserve_settlement_generation(source.id, source=source)
                    cooldown_generation = owner._service_cooldowns.advance(source, slot.model_id)

            async def on_sent():
                nonlocal sent, trial_epoch, source, skip, health_generation, cooldown_generation
                # Connect/TLS holds no controller lock. Recheck identity/health
                # and reserve the sole trial immediately before request bytes.
                async with owner._mutation_lock:
                    current, _, skip = await self.classify(slot, locked=True)
                    if skip is not None:
                        raise _NotAdmitted
                    source = current
                    blocked = source.state.status in {"needs_action", "error"}
                    if ((waiting_trial and owner._search_trial_epochs.get(source.id, 0) != waiting_epoch)
                            or (blocked and not waiting_trial)):
                        skip = {"source_id": slot.source_id, "label": source.display_name[:64], "kind": "service",
                                "outcome": "skipped", "skipped": "unavailable"}
                        raise _NotAdmitted
                    if blocked:
                        if not owner.reserve_search_reenable_trial(source.id):
                            raise _NotAdmitted
                        trial_epoch = waiting_epoch
                    if waiting_trial:
                        # A slower contender may now see a repaired Source, or
                        # an armed trial released by a query refusal. In either
                        # case its newly admitted request owns fresh fences.
                        health_generation = owner._reserve_settlement_generation(source.id, source=source)
                        cooldown_generation = owner._service_cooldowns.advance(source, slot.model_id)
                    sent = True

            response = await owner.adapter.search(
                source.id, source.vendor, slot.model_id, source.credential_ref, request, on_sent=on_sent,
            )
            ended_at = owner.now()
            return response

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, min(ATTEMPT_SECONDS, self.deadline - asyncio.get_running_loop().time())))
        except asyncio.TimeoutError:
            timed_out = True
            task.cancel()
        except asyncio.CancelledError:
            stopped = True
            task.cancel()
        except _NotAdmitted:
            return None, skip
        except Exception:
            response = SearchResponse(None, {}, None, "connection")
        finally:
            if not task.done():
                async def drain():
                    # Convert only the child transport's cancellation to a
                    # normal completion. Cancellation of our shield is then
                    # unambiguously the caller's Stop, including on Python 3.10.
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                draining = asyncio.create_task(drain())
                try:
                    try:
                        await asyncio.shield(draining)
                    except asyncio.CancelledError:
                        stopped = True
                        await await_owned_task(draining)
                except _NotAdmitted:
                    pass
                except Exception:
                    response = SearchResponse(None, {}, None, "connection")
        if skip is not None and not sent:
            if stopped:
                raise asyncio.CancelledError
            return None, skip
        response = response or SearchResponse(None, {}, None, "cancelled" if stopped else "timeout" if timed_out else "connection")
        if timed_out and response.error == "cancelled":
            response = replace(response, error="timeout")
        stopped = stopped or response.error == "cancelled"
        ended_at = ended_at or owner.now()

        result = None
        write = None

        async def settle_result():
            nonlocal result, write
            result = await asyncio.to_thread(map_response, catalog, response, sent=sent, results=slot.results, say=self.say)
            if stopped and response.http_status in {None, 200}:
                # Stop is not a vendor failure. Keep billing/authentication facts
                # but never infer an unreadable response or a retry deadline.
                result = replace(result, cooling=False)
            # Queue first, synchronously; stopped calls cannot lose sent usage.
            write = owner.usage_writer.record_service(
                source_id=source.id, service_id=slot.model_id, billable=result.billable,
                uncertain=result.uncertain, reported_usd=result.reported_usd, at=ended_at,
            ) if sent else None
            superseded = False
            async with owner._mutation_lock:
                previous = await asyncio.to_thread(owner.store.load)
                keyless_source = is_keyless_exa_source(source)
                current = source if keyless_source else next((s for s in previous.sources if s.id == source.id), None)
                identity_current = keyless_source or (current is not None and source_identity(current) == source_identity(source))
                health_current = not keyless_source and identity_current and owner._settlement_current(current, health_generation)
                cooldown_current = identity_current and owner._service_cooldowns.current(current, slot.model_id, cooldown_generation)
                writes_health = result.blocker is not None or result.authenticated
                writes_cooldown = result.cooling or result.record["status"] == "completed"
                superseded = (
                    (health_generation is not None and writes_health and not health_current)
                    or (cooldown_generation is not None and writes_cooldown and not cooldown_current)
                )
                if cooldown_current:
                    owner._service_cooldowns.settle(current, slot.model_id, cooldown_generation, result, response, ended_at)
                if health_current:
                    state = current.state
                    if result.blocker:
                        state = ModelHubSourceStateConfig(status="needs_action", detail_key=result.blocker)
                    elif trial_epoch is not None and response.http_status == 200:
                        state = ModelHubSourceStateConfig(status="standby")
                    if state != current.state or (result.authenticated and current.verification_pending is not None):
                        config = await asyncio.to_thread(owner._clone_config, previous)
                        updated = owner._source(config, source.id)
                        updated.state = state
                        if result.authenticated:
                            updated.verification_pending = None
                        await owner._save_runtime_config(previous, config)
            if result.record["status"] == "completed":
                await owner.record_hosted_success(source, slot.model_id, "web_search", "service")
            return _ExecutorAttempt(
                source, slot.model_id, result.record, result.cost, result.billable or result.uncertain,
                result.record["status"] != "completed", kind="service", superseded=superseded,
            )

        async def settle():
            try:
                return await settle_result()
            finally:
                # These resources outlive any local mapping/evidence/config
                # error: never strand a trial or lose an already queued charge.
                if trial_epoch is not None:
                    async with owner._mutation_lock:
                        if owner._search_trial_epochs.get(source.id, 0) == trial_epoch:
                            owner.settle_search_reenable_trial(source.id)
                            if result is not None and result.record.get("error_code") == "service_refused":
                                owner._search_reenable_trials.add(source.id)
                if write is not None:
                    await owner.usage_writer.wait_recorded(write)

        settlement = asyncio.create_task(settle())
        try:
            attempt = await asyncio.shield(settlement)
        except asyncio.CancelledError:
            await await_owned_task(settlement)
            raise
        if stopped:
            raise asyncio.CancelledError
        return attempt, None
