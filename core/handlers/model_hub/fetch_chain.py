"""Controller-owned web_fetch chain and service settlement.

The chain freezes order in ``TurnHostedTools`` but rechecks identity, enablement
and shared service cooldown immediately before each send. Local execution is
isolated in ``LocalFetchEngine``; configured/keyless transports stay behind the
EngineAdapter boundary.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from config.v2_config import (
    KEYLESS_EXA_SOURCE_ID,
    is_keyless_exa_source,
    model_hub_builtin_source,
)
from core.agent_core.messages import HostedAttempt, HostedCost, WebFetchRecord
from core.web_fetch_worker.policy import FetchFailure

from .adapter import SearchResponse
from .async_owner import await_owned_task
from .capability_evidence import identity_digest, utc_text
from .fetch_services import FetchServiceResult, ZERO, map_fetch_response
from .hosted_executor import ExecutorCall, Failure, call_timeout
from .hosted_turn import ExecutorSlot, TurnHostedTools
from .retry import source_identity
from vibe.model_hub_runtime.fetch_catalog import fetch_service


class _NotAdmitted(Exception):
    pass


@dataclass(frozen=True)
class FetchAttempt:
    source: Any
    service_id: str
    kind: str
    record: dict[str, Any]
    cost: HostedCost | None
    billable: bool
    uncertain: bool
    moves_on: bool
    sent: bool

    def execution(self) -> dict[str, Any]:
        api = "local_fetch" if self.kind == "local" else "fetch_api"
        execution: dict[str, Any] = {
            "mode": "proxy",
            "executor_origin": {"provider": self.source.vendor, "api": api, "model": self.service_id},
            "executor_source_id": self.source.id,
            "executor_billing": "metered",
        }
        if self.cost is not None:
            execution["cost"] = {"basis": self.cost.basis, "usd": self.cost.usd}
            if self.cost.estimate is not None:
                execution["cost"]["estimate"] = self.cost.estimate
        return execution


class FetchChain:
    def __init__(self, owner: Any, call: ExecutorCall, turn: TurnHostedTools, say: Callable[..., str]):
        self.owner = owner
        self.call = call
        self.turn = turn
        self.say = say
        self.deadline = asyncio.get_running_loop().time() + call_timeout(call.tool)
        self.requested_url = str(call.arguments["url"])
        self.facts: list[dict[str, Any]] = []
        self.tried: list[FetchAttempt] = []
        self._gate_error: str | None = None

    @property
    def _engine(self):
        engine = getattr(self.owner, "_local_fetch_engine", None)
        if engine is None:
            from .local_fetch import LocalFetchEngine
            engine = LocalFetchEngine()
            self.owner._local_fetch_engine = engine
        return engine

    async def _classify(self, slot: ExecutorSlot, *, locked: bool = False):
        if not locked:
            async with self.owner._mutation_lock:
                return await self._classify(slot, locked=True)
        config = await asyncio.to_thread(self.owner.store.load)
        return await asyncio.to_thread(self._classify_sync, slot, config)

    def _classify_sync(self, slot: ExecutorSlot, config):
        source = model_hub_builtin_source(slot.source_id) or next(
            (item for item in config.sources if item.id == slot.source_id), None
        )
        fact = {
            "source_id": slot.source_id,
            "label": slot.label or slot.source_id,
            "kind": slot.kind,
            "outcome": "skipped",
        }
        if source is None or identity_digest(source) != slot.identity:
            fact["skipped"] = "unavailable"
            return None, None, fact
        fact["label"] = source.display_name[:64]
        service = fetch_service(source)
        if service is None or service.get("service_id") != slot.model_id:
            fact["skipped"] = "unavailable"
            return source, service, fact
        if not service.get("available", False):
            fact["skipped"] = "unavailable"
            return source, service, fact
        if not slot.enabled:
            fact["skipped"] = "disabled"
            return source, service, fact
        if source.id != KEYLESS_EXA_SOURCE_ID and source.state.status in {"needs_action", "error"}:
            fact.update(skipped="blocked", needs_action=source.state.detail_key or "models.source.error.unclassified")
            return source, service, fact
        if service.get("kind") != "local":
            until = self.owner._service_cooldowns.until(source, slot.model_id, self.owner.now())
            if until is not None:
                fact.update(skipped="cooling", retry_at=utc_text(until))
                return source, service, fact
        return source, service, None

    async def run(self) -> dict[str, Any]:
        try:
            await self._engine.check(self.requested_url, deadline=self.deadline)
        except FetchFailure as exc:
            self._gate_error = exc.code
            return self._finish(None)
        slots = self.turn.executors.get(self.call.tool, ())
        for slot in slots:
            if asyncio.get_running_loop().time() >= self.deadline:
                break
            source, service, skip = await self._classify(slot)
            if skip is not None:
                self.facts.append(skip)
                continue
            if service is None or source is None:
                self.facts.append({
                    "source_id": slot.source_id, "label": slot.label or slot.source_id,
                    "kind": slot.kind, "outcome": "skipped", "skipped": "unavailable",
                })
                continue
            if service.get("kind") == "local":
                attempt, skip = await self._local_attempt(slot, source)
            else:
                attempt, skip = await self._service_attempt(slot, source, service)
            if attempt is None:
                self.facts.append(skip or {
                    "source_id": slot.source_id, "label": slot.label or slot.source_id,
                    "kind": slot.kind, "outcome": "skipped", "skipped": "unavailable",
                })
                continue
            self.tried.append(attempt)
            self.facts.append(self._fact(attempt))
            if not attempt.moves_on:
                break
        return self._finish(self.tried[-1] if self.tried else None)

    def _fact(self, attempt: FetchAttempt) -> dict[str, Any]:
        record = attempt.record
        fact: dict[str, Any] = {
            "source_id": attempt.source.id,
            "label": attempt.source.display_name[:64],
            "kind": attempt.kind,
            "outcome": "completed" if record.get("status") == "completed" else "failed",
        }
        if record.get("error_code"):
            fact["error_code"] = record["error_code"]
        if attempt.cost is not None:
            fact["cost"] = {"basis": attempt.cost.basis, "usd": attempt.cost.usd}
        return fact

    async def _local_attempt(self, slot: ExecutorSlot, source):
        sent = False

        def on_sent():
            nonlocal sent
            sent = True

        try:
            raw = await self._engine.fetch(
                self.requested_url,
                deadline=self.deadline,
                on_sent=on_sent,
            )
        except FetchFailure as exc:
            raw = {"error_code": exc.code, "terminal": exc.terminal, "sent": sent}
        sent = bool(raw.get("sent", sent))
        if raw.get("skipped"):
            return None, {
                "source_id": slot.source_id, "label": source.display_name[:64], "kind": "local",
                "outcome": "skipped", "skipped": raw["skipped"],
                **({"retry_at": raw["retry_at"]} if raw.get("retry_at") else {}),
            }
        code = raw.get("error_code")
        if code:
            record = {
                "status": "failed", "error_code": code,
                "error": self.say(f"hostedTools.errorCode.{code}"),
                "content": self.say(f"hostedTools.errorCode.{code}"),
            }
            moves_on = not raw.get("terminal", False)
        else:
            record = {"status": "completed", **raw}
            moves_on = False
        cost = ZERO if sent else None
        if sent:
            write = self.owner.usage_writer.record_service(
                source_id=source.id, service_id=slot.model_id, tool="web_fetch",
                billable=False, uncertain=False, at=self.owner.now(),
            )
            await self.owner.usage_writer.wait_recorded(write)
        if record.get("status") == "completed":
            await self.owner.record_hosted_success(source, slot.model_id, "web_fetch", "direct")
        for key in ("error_code", "terminal", "sent", "retry_at", "cooldown_code", "cooldown_suppressed"):
            record.pop(key, None)
        return FetchAttempt(
            source, slot.model_id, "local", record, cost, False, False, moves_on, sent,
        ), None

    async def _service_attempt(self, slot: ExecutorSlot, source, service):
        if is_keyless_exa_source(source):
            remaining = max(0.0, self.deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(self.owner._keyless_exa_attempt_lock.acquire(), timeout=remaining)
            except asyncio.TimeoutError:
                return None, {
                    "source_id": slot.source_id, "label": source.display_name[:64], "kind": "service",
                    "outcome": "skipped", "skipped": "service_busy",
                }
            try:
                return await self._service_attempt_locked(slot, source, service)
            finally:
                self.owner._keyless_exa_attempt_lock.release()
        return await self._service_attempt_locked(slot, source, service)

    async def _service_attempt_locked(self, slot, source, service):
        sent = False
        response: SearchResponse | None = None
        health_generation = None
        cooldown_generation = None

        async def invoke():
            nonlocal sent, source, response, health_generation, cooldown_generation
            async with self.owner._mutation_lock:
                current, _, skip = await self._classify(slot, locked=True)
                if skip is not None:
                    raise _NotAdmitted
                source = current
                health_generation = self.owner._reserve_settlement_generation(source.id, source=source)
                cooldown_generation = self.owner._service_cooldowns.advance(source, slot.model_id)

            async def on_sent():
                nonlocal sent, source
                async with self.owner._mutation_lock:
                    current, _, skip = await self._classify(slot, locked=True)
                    if skip is not None:
                        raise _NotAdmitted
                    source = current
                    sent = True

            request = self._request_for(service)
            response = await self.owner.adapter.fetch(
                source.id, source.vendor, slot.model_id, source.credential_ref,
                request, on_sent=on_sent,
            )
            return response

        task = asyncio.create_task(invoke())
        timed_out = stopped = False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, self.deadline - asyncio.get_running_loop().time()))
        except asyncio.TimeoutError:
            timed_out = True
            task.cancel()
        except asyncio.CancelledError:
            stopped = True
            task.cancel()
        except _NotAdmitted:
            await self._release_unspent(source, slot, health_generation, cooldown_generation, response)
            return None, {"source_id": slot.source_id, "label": source.display_name[:64], "kind": "service", "outcome": "skipped", "skipped": "unavailable"}
        except Exception:
            response = SearchResponse(None, {}, None, "connection")
        finally:
            if not task.done():
                draining = asyncio.create_task(self._drain(task))
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    stopped = True
                    await await_owned_task(draining)
        if stopped and not sent:
            await self._release_unspent(source, slot, health_generation, cooldown_generation, response)
            raise asyncio.CancelledError
        response = response or SearchResponse(None, {}, None, "cancelled" if stopped else "timeout" if timed_out else "connection")
        if timed_out and response.error == "cancelled":
            response = replace(response, error="timeout")
        if stopped:
            result = await asyncio.to_thread(
                map_fetch_response, service, response, requested_url=self.requested_url,
                sent=sent, say=self.say,
            )
            result = replace(result, cooling=False)
            await self._settle_owned(source, slot, result, response, health_generation, cooldown_generation, sent)
            raise asyncio.CancelledError
        if not sent:
            await self._release_unspent(source, slot, health_generation, cooldown_generation, response)
            return None, {
                "source_id": slot.source_id, "label": source.display_name[:64], "kind": "service",
                "outcome": "skipped", "skipped": "unavailable",
            }
        result = await asyncio.to_thread(
            map_fetch_response, service, response, requested_url=self.requested_url,
            sent=sent, say=self.say,
        )
        await self._settle_owned(source, slot, result, response, health_generation, cooldown_generation, sent)
        record = dict(result.record)
        if result.retry_after:
            record["retry_after"] = result.retry_after
        moves_on = result.record.get("status") != "completed"
        return FetchAttempt(source, slot.model_id, "service", record, result.cost, result.billable, result.uncertain, moves_on, sent), None

    async def _release_unspent(self, source, slot, health_generation, cooldown_generation, response):
        if health_generation is None and cooldown_generation is None:
            return
        await self._settle_owned(
            source, slot,
            FetchServiceResult({"status": "completed"}, None, False, False, None, False),
            response or SearchResponse(None, {}, None, "invalid_endpoint"),
            health_generation, cooldown_generation, False,
        )

    async def _settle_owned(self, source, slot, result, response, health_generation, cooldown_generation, sent):
        await await_owned_task(asyncio.create_task(
            self._settle_service(source, slot, result, response, health_generation, cooldown_generation, sent)
        ))

    @staticmethod
    async def _drain(task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _request_payload(service: Mapping[str, Any], url: str) -> dict[str, Any]:
        # The request is deliberately one URL. Catalog mappings are data, not
        # transport code, so the adapter can validate the fixed endpoint.
        request_spec = service.get("request") if isinstance(service.get("request"), Mapping) else {}
        value = url
        if value is None:
            raise ValueError("fetch URL is required")
        field = str(request_spec.get("url_field") or "url")
        payload = dict(request_spec.get("fixed") or {})
        payload[field] = [value] if request_spec.get("url_array") else value
        return payload

    def _request_for(self, service: Mapping[str, Any]) -> dict[str, Any]:
        return self._request_payload(service, self.requested_url)

    async def _settle_service(self, source, slot, result: FetchServiceResult, response, health_generation, cooldown_generation, sent):
        write = self.owner.usage_writer.record_service(
            source_id=source.id,
            service_id=slot.model_id,
            tool="web_fetch",
            billable=result.billable,
            uncertain=result.uncertain,
            reported_usd=result.reported_usd,
            at=self.owner.now(),
        ) if sent else None
        async with self.owner._mutation_lock:
            previous = await asyncio.to_thread(self.owner.store.load)
            current = model_hub_builtin_source(source.id) or next((item for item in previous.sources if item.id == source.id), None)
            identity_current = current is not None and source_identity(current) == source_identity(source)
            if identity_current and cooldown_generation is not None:
                self.owner._service_cooldowns.settle(current, slot.model_id, cooldown_generation, _search_compatible(result), response, self.owner.now())
            if identity_current and health_generation is not None and result.blocker:
                config = await asyncio.to_thread(self.owner._clone_config, previous)
                updated = self.owner._source(config, source.id)
                from config.v2_config import ModelHubSourceStateConfig
                updated.state = ModelHubSourceStateConfig(status="needs_action", detail_key=result.blocker)
                await self.owner._save_runtime_config(previous, config)
        if write is not None:
            await self.owner.usage_writer.wait_recorded(write)
        if result.record.get("status") == "completed":
            await self.owner.record_hosted_success(source, slot.model_id, "web_fetch", "service")

    def _finish(self, last: FetchAttempt | None) -> dict[str, Any]:
        if self._gate_error:
            code = self._gate_error
            raw = {"status": "failed", "error_code": code,
                   "error": self.say(f"hostedTools.errorCode.{code}"),
                   "content": self.say(f"hostedTools.errorCode.{code}")}
            execution = None
        elif last is None:
            if any(f.get("skipped") in {"blocked", "disabled"} for f in self.facts):
                code = "fetch_sources_blocked"
            else:
                code = "fetch_unavailable"
            error = self.say(f"hostedTools.errorCode.{code}")
            raw, execution = {"status": "failed", "error_code": code, "error": error, "content": error}, None
        else:
            raw, execution = last.record, last.execution()
            if last.moves_on:
                raw = dict(raw)
                tried = ", ".join(dict.fromkeys(item.source.display_name for item in self.tried))
                raw["error"] = f"{raw.get('error', '')} {self.say('modelHub.hostedTools.triedSources', sources=tried)}".strip()[:1000]
        record = self._record(raw)
        if self.facts:
            record["attempts"] = self.facts
        answer: dict[str, Any] = {"record": record}
        if execution is not None:
            earlier = [item.execution().get("cost") for item in self.tried[:-1] if item.cost is not None]
            if earlier:
                execution["failed_attempts"] = [
                    {"source_id": item.source.id, "billing": "metered", "cost": item.execution()["cost"]}
                    for item in self.tried[:-1] if item.cost is not None
                ]
            answer["execution"] = execution
        return answer

    def _record(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        content = raw.get("content") if isinstance(raw.get("content"), str) else str(raw.get("error") or "No page content was retained.")
        status = "completed" if raw.get("status") == "completed" else "failed"
        provider_truncated = bool(raw.get("provider_truncated"))
        attempts = tuple(self._attempt_objects())
        base = {
            "status": status,
            "requested_url": self.requested_url,
            "content": content,
            "size_bytes": len(content.encode("utf-8", "surrogatepass")),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "budget_method": "utf8_bytes_div4_v1",
            "final_url": raw.get("final_url"),
            "canonical_url": raw.get("canonical_url"),
            "title": raw.get("title"),
            "content_type": raw.get("content_type", "text/markdown" if status == "completed" else None),
            "source_content_type": raw.get("source_content_type"),
            "source_size_bytes": raw.get("source_size_bytes"),
            "error": raw.get("error") if status == "failed" else None,
            "error_code": raw.get("error_code") if status == "failed" else None,
            "attempts": attempts,
            "transport_degraded": raw.get("transport_degraded"),
            "transport_degraded_reason": raw.get("transport_degraded_reason"),
            "charset_fallback": raw.get("charset_fallback"),
        }
        reason = "provider_limit" if provider_truncated else None
        try:
            candidate = WebFetchRecord(**base, truncated=reason is not None, truncation_reason=reason)
            if reason is None:
                return _record_payload(candidate)
        except ValueError:
            reason = reason or "budget"
        hi = len(content)
        lo = 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate_content = content[:mid]
            try:
                candidate = WebFetchRecord(
                    **{**base, "content": candidate_content, "size_bytes": len(candidate_content.encode("utf-8", "surrogatepass")),
                       "truncated": True, "truncation_reason": reason},
                )
            except ValueError:
                hi = mid - 1
            else:
                best = candidate
                lo = mid + 1
        if best is None:
            best = WebFetchRecord(
                **{**base, "content": " ", "size_bytes": 1, "truncated": True, "truncation_reason": reason},
            )
        return _record_payload(best)

    def _attempt_objects(self):
        for fact in self.facts:
            yield HostedAttempt(
                source_id=fact["source_id"], label=fact["label"], kind=fact["kind"], outcome=fact["outcome"],
                error_code=fact.get("error_code"), skipped=fact.get("skipped"), retry_at=fact.get("retry_at"),
                needs_action=fact.get("needs_action"), cost=_cost(fact.get("cost")),
            )


def _cost(value: object) -> HostedCost | None:
    if not isinstance(value, Mapping):
        return None
    return HostedCost(basis=str(value.get("basis")), usd=value.get("usd"), estimate=value.get("estimate"))


def _record_payload(record: WebFetchRecord) -> dict[str, Any]:
    from core.agent_core.web_fetch import fetch_record_to_dict
    return fetch_record_to_dict(record)


def _search_compatible(result: FetchServiceResult):
    # ServiceCooldowns is intentionally shared with web_search; it only needs
    # the stable fields used by settle, not the search mapper's concrete class.
    from .search_services import ServiceResult
    return ServiceResult(
        result.record, result.cost, result.billable, result.uncertain,
        result.blocker is None and result.record.get("status") == "completed",
        result.blocker, result.cooling,
    )
