"""How a hosted tool call is shown: the Web card's data and the IM line (``vibey-hosted-tools.md`` §6.4).

One projection from the canonical C-1 records, so the card, the IM line and any
later surface render the same facts for a native and a proxied call alike:

- a native call is a ``HostedToolBlock`` in an ``AssistantMessage``; the
  response-scoped ``hosted_summary`` (cost, cited posts) belongs to the whole
  response, so it is shown once, on the response's last hosted call;
- a proxied call is a ``ToolCallBlock`` whose ``ToolResultMessage`` carries
  ``hosted``; its record and executor are its own.

Nothing here prices, routes or replays anything: it reads what the producers
recorded. ``HostedActivity.to_payload`` is the wire shape a ``toolcall`` row
carries as ``content.hosted`` (version 1), read by the Web activity group.

A search chain that reached a search-capable candidate records ``attempts``
(``web-search-services.md`` §4.3). This module alone decides, from those facts
and each Source's current state, who ran the call, what it cost, the switch
note, and each entry's reason and repair; the IM line and the Web card only put
the decided keys into words.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Callable, Literal, Mapping, Optional

from config.v2_config import KEYLESS_EXA_SOURCE_ID
from core.agent_core.hosted_continuations import pending_hosted
from core.agent_core.messages import (
    HOSTED_ERROR_CODES,
    AssistantMessage,
    HostedAction,
    HostedAttempt,
    HostedCost,
    HostedImage,
    HostedResult,
    HostedSource,
    HostedToolBlock,
    ProxyExecution,
    ServiceExecutorOrigin,
    ToolCallBlock,
    WebFetchRecord,
)
from core.citations import safe_url
from core.message_output import plain_untrusted_text
from vibe.i18n import t as i18n_t

HOSTED_ACTIVITY_VERSION = 1

# The longest query or prompt an IM line quotes; the Web card shows it whole.
LINE_SUBJECT_MAX_CHARS = 60
_BUILTIN_LABELS = {KEYLESS_EXA_SOURCE_ID: "keylessExa", "src_localfetch": "fetch.local", "src_jinakeyless": "fetch.jina"}

SourceNames = Callable[[str], Optional[str]]
#: A Source as Model Hub holds it now: ``protocol``, ``vendor``, ``needs_action`` (its ``needs_action`` or ``error``
#: detail key, absent while it is healthy) and, for a search-capable Source, its effective ``enabled``. ``None`` is a
#: Source removed since. A caller that could not read Model Hub passes no ``SourceState`` at all.
SourceState = Callable[[str], Optional[Mapping[str, Any]]]

_NEEDS_ACTION = "models.source.needs_action."
# The Source health each search-service failure sets (§4.2), so a failed entry is matched against the Source's
# current health even when the producer recorded none beside the code.
_CODE_HEALTH = {
    "service_key_invalid": f"{_NEEDS_ACTION}credential_revoked",
    "service_balance": f"{_NEEDS_ACTION}balance_exhausted",
    "service_limit": f"{_NEEDS_ACTION}spend_limit_reached",
}
_HEALTH_CODE = {health: code for code, health in _CODE_HEALTH.items()}
# Failures with a reason of their own; any other, a model executor's included, says it failed.
_FAILURE_REASONS = (
    "service_key_invalid",
    "service_balance",
    "service_limit",
    "service_unavailable",
    "service_rate_limited",
    "service_refused",
    "service_unreadable",
    "service_busy",
    "fetch_target_unreadable",
    "fetch_url_blocked",
    "fetch_dns_unverified",
    "fetch_challenge",
    "fetch_js_shell",
    "fetch_timeout",
    "fetch_unsupported_content",
    "fetch_refused",
    "fetch_unavailable",
)
# Each existing Source health detail's repair; any other detail (the generic error) points to the Source.
_REPAIRS = {
    f"{_NEEDS_ACTION}credential_revoked": "repair.key",
    f"{_NEEDS_ACTION}oauth_expired": "repair.reauthorize",
    f"{_NEEDS_ACTION}balance_exhausted": "repair.balance",
    f"{_NEEDS_ACTION}spend_limit_reached": "repair.limit",
    f"{_NEEDS_ACTION}plan_excludes_product": "repair.plan",
    f"{_NEEDS_ACTION}account_banned": "repair.account",
}
# A tool-only search Source leaves a balance or limit state only through "重新启用", so its repair ends with it; a
# model Source recovers on its own paths (§4.2). Tavily's catalog row names both of its limits' repairs (432, 433).
_SEARCH_REPAIRS = {
    f"{_NEEDS_ACTION}balance_exhausted": "repair.balanceReenable",
    f"{_NEEDS_ACTION}spend_limit_reached": "repair.limitReenable",
}
_VENDOR_SEARCH_REPAIRS = {("tavily", f"{_NEEDS_ACTION}spend_limit_reached"): "repair.tavilyLimit"}


def _no_names(_source_id: str) -> Optional[str]:
    return None


@dataclass(frozen=True)
class HostedAttemptView(HostedAttempt):
    """One recorded chain entry as the surfaces show it now.

    The recorded facts are kept as written, except ``label``, which is the Source's current name (the recorded one
    for a Source removed since), and ``superseded``, which is also set when the Source's current health no longer
    matches the failure. ``reason_key`` says what happened to the Source in this call and ``remedy_key`` what it
    needs now, if anything, both relative to each surface's hosted-tool copy (``hostedTools.`` for IM,
    ``chat.agentActivity.hosted.`` for the Web). A completed entry has no reason.
    """

    reason_key: Optional[str] = None
    remedy_key: Optional[str] = None


@dataclass(frozen=True)
class HostedSwitch:
    """A completed search that moved past Sources that failed or were blocked: their names, why (one Source's own
    reason, or "failed" for several), and the Source that answered."""

    sources: tuple[str, ...]
    reason_key: str
    to: str


@dataclass(frozen=True)
class HostedActivity:
    """One hosted call as the user sees it."""

    call_id: str
    tool: str
    # How it ran: natively by the model's provider, by a search executor, or by an image endpoint directly.
    mode: Literal["native", "proxy", "direct"]
    status: str
    # The reason as text: the provider's own, or Avibe's, which always carries ``error_code``, the display key this
    # module and the Web card render in the viewer's language (#2589).
    error: Optional[str]
    # What the call was about: the query, prompt, page or thread it named.
    subject: Optional[str]
    # The provider-neutral action type for a native call; a proxied call has none.
    action: Optional[str]
    extra_queries: tuple[str, ...]
    # The vendor that ran it (the response's for native, the executor's for proxy).
    provider: Optional[str]
    source_id: Optional[str]
    source_name: Optional[str]
    model: Optional[str]
    sources: tuple[HostedSource, ...]
    images: tuple[HostedImage, ...]
    size: Optional[str]
    quality: Optional[str]
    # The prompt the image model actually rendered, when the provider reported it.
    revised_prompt: Optional[str]
    cost: Optional[HostedCost]
    # How many calls ``cost`` covers: a native response's figure is for all its calls.
    cost_calls: int
    duration_ms: Optional[int] = None
    # ``cost`` is a lower bound: an attempt reported no price, so the call cost at least this.
    cost_at_least: bool = False
    # The stable code of a reason Avibe worded itself (``HOSTED_ERROR_CODES``), by
    # which each surface says it in its reader's language; None for a provider's own.
    error_code: Optional[str] = None
    # The committed response row a native call came in. Its card is ordered just
    # before that row, as the call happened before the text that reads its result.
    response_id: Optional[str] = None
    # The Source that refused to run this search natively, so it ran by proxy instead (``hosted.fallback_from``),
    # with its current name.
    fallback_source_id: Optional[str] = None
    fallback_source_name: Optional[str] = None
    # Every candidate a search chain reached (§4.3), as shown now; None for the released shape, which has none.
    attempts: Optional[tuple[HostedAttemptView, ...]] = None
    # Whether a search service or a model executor ran it; None when the record does not say (released records).
    executor_kind: Optional[Literal["service", "model", "local"]] = None
    fetch: Optional[WebFetchRecord] = None
    switch: Optional[HostedSwitch] = None
    # Presentation-only explanation for a proxy hop selected after repeated native interruptions.
    route_switch: Optional[Literal["native_interrupted"]] = None
    # Policy refusal: the service ids already sent this URL; empty means no forwarding.
    policy_sources: Optional[tuple[str, ...]] = None
    # Display-only category of the owning model response's failure, never provider text.
    interruption: Optional[Literal["overloaded", "disconnected", "upstream"]] = None

    @property
    def completed(self) -> bool:
        """Whether the call finished: the one gate every surface reads.

        Only a completed call claims how it ran (its mode, who ran it, how long it
        took) and what it produced (counts, sources, images). A cut-off or failed
        call shows its status instead. A known cost shows whatever the status:
        a charge stands whatever happened after it. The Web card reads the same
        rule from ``hostedCompleted`` (``ui/src/lib/hostedActivity.ts``).
        """

        return self.status == "completed"

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": HOSTED_ACTIVITY_VERSION,
            "call_id": self.call_id,
            "tool": self.tool,
            "mode": self.mode,
            "status": self.status,
            "sources": [_source_payload(source) for source in self.sources],
            "images": [_image_payload(image) for image in self.images],
        }
        optional = {
            "error": self.error,
            "error_code": self.error_code,
            "subject": self.subject,
            "action": self.action,
            "provider": self.provider,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "model": self.model,
            "size": self.size,
            "quality": self.quality,
            "revised_prompt": self.revised_prompt,
            "duration_ms": self.duration_ms,
            "response_id": self.response_id,
            "executor_kind": self.executor_kind,
            "interruption": self.interruption,
            "route_switch": self.route_switch,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.policy_sources is not None:
            payload["policy_sources"] = list(self.policy_sources)
        if self.fetch is not None:
            from core.agent_core.web_fetch import fetch_record_to_dict
            payload["fetch"] = fetch_record_to_dict(self.fetch)
        if self.extra_queries:
            payload["extra_queries"] = list(self.extra_queries)
        if self.attempts is not None:
            payload["attempts"] = [_attempt_payload(attempt) for attempt in self.attempts]
        if self.switch is not None:
            payload["switch"] = {
                "sources": list(self.switch.sources),
                "reason_key": self.switch.reason_key,
                "to": self.switch.to,
            }
        if self.fallback_source_id is not None:
            fallback: dict[str, Any] = {"source_id": self.fallback_source_id}
            if self.fallback_source_name is not None:
                fallback["source_name"] = self.fallback_source_name
            payload["fallback_from"] = fallback
        if self.cost is not None:
            cost: dict[str, Any] = {"basis": self.cost.basis, "calls": self.cost_calls}
            if self.cost.usd is not None:
                cost["usd"] = self.cost.usd
            if self.cost.estimate:
                cost["estimate"] = True
            if self.cost_at_least:
                cost["at_least"] = True
            payload["cost"] = cost
        return payload


def _attempt_payload(attempt: HostedAttemptView) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_id": attempt.source_id,
        "label": attempt.label,
        "kind": attempt.kind,
        "outcome": attempt.outcome,
    }
    for name in ("error_code", "skipped", "retry_at", "needs_action", "superseded", "reason_key", "remedy_key"):
        value = getattr(attempt, name)
        if value is not None:
            out[name] = value
    if attempt.cost is not None:
        out["cost"] = {"basis": attempt.cost.basis}
        if attempt.cost.usd is not None:
            out["cost"]["usd"] = attempt.cost.usd
        if attempt.cost.estimate:
            out["cost"]["estimate"] = True
    return out


def _source_payload(source: HostedSource) -> dict[str, Any]:
    """A source as the card links it: only a URL a browser should open, held to the rule citation links are
    (``safe_url``). Any other string a provider gave stays readable as the source's title, never a link."""
    url = safe_url(source.url)
    out: dict[str, Any] = {"url": url} if url else {}
    for name in ("title", "snippet", "page_age"):
        value = getattr(source, name)
        if value:
            out[name] = value
    if not url and not source.title:
        out["title"] = source.url
    return out


def _image_payload(image: HostedImage) -> dict[str, Any]:
    out: dict[str, Any] = {"path": image.path, "mime": image.mime}
    if image.width is not None:
        out["width"] = image.width
    if image.height is not None:
        out["height"] = image.height
    if image.bytes is not None:
        out["bytes"] = image.bytes
    return out


def _subject(action: HostedAction) -> tuple[Optional[str], tuple[str, ...]]:
    if action.type == "search" and action.queries:
        return action.queries[0], tuple(action.queries[1:])
    # ``other.name`` names a provider item type, not the subject of the action.
    for name in ("query", "prompt", "pattern", "url", "ref"):
        value = getattr(action, name)
        if value:
            return value, ()
    return None, ()


def response_interruption(message: AssistantMessage) -> Optional[Literal["overloaded", "disconnected", "upstream"]]:
    """A deliberately small, sanitized display vocabulary, not a retry decision.

    The model error belongs to pending calls only. Completed calls and Anthropic
    deferred exchanges keep their existing outcome. No provider diagnostic leaves
    this boundary, even when it contains URLs, request ids or credentials.
    """
    if message.stop_reason != "error":
        return None
    error = (message.error_message or "").lower()
    if "overloaded" in error or "overloaded_error" in error:
        return "overloaded"
    if "stream disconnected" in error or "stream ended" in error or "connection reset" in error:
        return "disconnected"
    return "upstream"


def native_activities(
    message: AssistantMessage,
    *,
    source_names: SourceNames = _no_names,
    response_id: Optional[str] = None,
) -> list[HostedActivity]:
    """Every native hosted call in a response, in content order.

    The response's summary is shown once: its cost on the last hosted call, and
    its cited posts on the last X search that has no sources of its own (xAI
    reports X-search results only as citations on the answer).
    """

    # A deferred call has not run. It produces no card on either surface; its later
    # result fragment is an ordinary hosted block at that response's own position.
    deferred = pending_hosted(message)
    blocks = [block for block in message.content if isinstance(block, HostedToolBlock) and block.id not in deferred]
    if not blocks:
        return []
    summary = message.hosted_summary
    cited_owner = next(
        (
            index
            for index in range(len(blocks) - 1, -1, -1)
            if blocks[index].tool == "x_search" and not blocks[index].record.sources
        ),
        None,
    )
    activities = []
    for index, block in enumerate(blocks):
        subject, extra = _subject(block.action)
        sources = block.record.sources
        if index == cited_owner and summary is not None:
            sources = summary.cited_sources
        last = index == len(blocks) - 1
        activities.append(
            HostedActivity(
                call_id=block.id,
                tool=block.tool,
                mode="native",
                status=block.record.status,
                error=block.record.error,
                error_code=block.record.error_code,
                subject=subject,
                action=block.action.type,
                extra_queries=extra,
                provider=message.origin.provider,
                source_id=block.native.source_id,
                source_name=source_names(block.native.source_id),
                model=message.origin.model,
                sources=tuple(sources),
                images=tuple(block.record.images or ()),
                size=block.action.size,
                quality=block.action.quality,
                # A native generate action's prompt is the provider's revised prompt.
                revised_prompt=block.action.prompt if block.action.type == "generate" else None,
                cost=summary.cost if last and summary is not None else None,
                cost_calls=len(blocks),
                response_id=response_id,
                interruption=response_interruption(message) if block.record.status == "pending" else None,
            )
        )
    return activities


def _repair(detail: str, current: Mapping[str, Any]) -> str:
    """What a Source in health ``detail`` needs: the detail's existing repair, ending with "重新启用" for a tool-only
    search Source in a balance or limit state."""

    if current.get("protocol") == "search":
        repair = _VENDOR_SEARCH_REPAIRS.get((current.get("vendor"), detail)) or _SEARCH_REPAIRS.get(detail)
        if repair:
            return repair
    return _REPAIRS.get(detail, "repair.error")


def _view(attempt: HostedAttempt, source_names: SourceNames, source_state: Optional[SourceState], tool: str) -> HostedAttemptView:
    """One entry as shown now. Current state governs every repair, never the record: a Source repaired, re-enabled
    or removed since shows none, and without a Model Hub read nothing is offered at all. Only a failed entry can be
    superseded: explicitly, by a removal, or by a current health other than the one its failure set (§4.3)."""

    current = source_state(attempt.source_id) if source_state is not None else None
    superseded = bool(attempt.superseded)
    reason: Optional[str] = None
    remedy: Optional[str] = None
    if attempt.outcome == "failed":
        health = attempt.needs_action or _CODE_HEALTH.get(attempt.error_code or "")
        if source_state is not None and not superseded:
            superseded = current is None or (health is not None and current.get("needs_action") != health)
        if superseded:
            reason = "reason.superseded"
        else:
            reason = f"reason.{attempt.error_code}" if attempt.error_code in _FAILURE_REASONS else "reason.failed"
            if health is not None and current is not None:
                remedy = _repair(health, current)
    elif attempt.skipped == "blocked":
        # The skip stays as recorded; its repair is whatever the Source needs now, if anything.
        recorded = _HEALTH_CODE.get(attempt.needs_action or "")
        reason = f"reason.{recorded}" if recorded else "skip.blocked"
        if current is not None and current.get("needs_action"):
            remedy = _repair(current["needs_action"], current)
    elif attempt.skipped is not None:
        reason = f"skip.{attempt.skipped}"
        if attempt.skipped == "disabled" and current is not None and current.get("enabled") is False:
            remedy = "repair.disabled"
    if (attempt.source_id in {KEYLESS_EXA_SOURCE_ID, "src_jinakeyless"} and not superseded
            and current is not None and current.get("enabled") is not False
            and (attempt.error_code == "service_rate_limited" or attempt.skipped == "cooling")):
        remedy = "repair.retrySearch"
    if tool == "web_fetch":
        reason = {"skip.disabled": "skip.fetch_disabled", "reason.service_refused": "reason.fetch_refused"}.get(reason, reason)
        if remedy == "repair.retrySearch":
            remedy = "repair.retryFetch"
        if remedy == "repair.disabled":
            remedy = "repair.fetch_disabled"
    return HostedAttemptView(
        **{field.name: getattr(attempt, field.name) for field in fields(HostedAttempt)}
        | {"label": source_names(attempt.source_id) or attempt.label, "superseded": superseded or None},
        reason_key=reason,
        remedy_key=remedy,
    )


def _last_tried(attempts: tuple[HostedAttemptView, ...]) -> Optional[int]:
    """Where the entry that ran the call is: the last one tried."""

    return next((index for index in range(len(attempts) - 1, -1, -1) if attempts[index].outcome != "skipped"), None)


def _switch(status: str, attempts: tuple[HostedAttemptView, ...], last: Optional[int]) -> Optional[HostedSwitch]:
    """The Sources a completed chain moved past before its answer: the failed ones and the blocked or disabled
    skips. A Source cooling down, refusing a long query or gone since the turn began was not a failure of its own."""

    if status != "completed" or last is None or attempts[last].outcome != "completed":
        return None
    winner = attempts[last]
    before = [
        attempt
        for attempt in attempts[:last]
        if attempt.outcome == "failed" or attempt.skipped in ("blocked", "disabled")
    ]
    if not before:
        return None
    reason = before[0].reason_key if len(before) == 1 and before[0].reason_key else "reason.failed"
    return HostedSwitch(sources=tuple(attempt.label for attempt in before), reason_key=reason, to=winner.label)


def _attempts_cost(attempts: tuple[HostedAttemptView, ...]) -> tuple[Optional[HostedCost], bool]:
    """What a chain cost, from its tried entries alone (§4.3), and whether that is only a lower bound.

    A skip sent nothing. An exact $0 is never an estimate, so the total's basis comes from its non-zero figures: it
    is approximate when any of them is a list price or an estimate, and an all-zero total is an exact "$0". A tried
    entry without a cost makes a known total a lower bound; with no known figure the cost is unknown.
    """

    tried = [attempt for attempt in attempts if attempt.outcome != "skipped"]
    known = [attempt.cost for attempt in tried if attempt.cost is not None]
    if not known:
        return None, False
    unpriced = len(known) < len(tried)
    priced = [cost for cost in known if cost.basis != "included"]
    if not priced:
        return (HostedCost(basis="included"), False) if not unpriced else (None, False)
    nonzero = [cost for cost in priced if cost.usd]
    reported = bool(nonzero) and all(cost.basis == "reported" for cost in nonzero)
    estimate = reported and any(cost.estimate for cost in nonzero)
    usd = round(sum(cost.usd or 0.0 for cost in priced), 6)
    if usd == 0 and unpriced:
        return None, False
    return HostedCost(basis="reported" if reported else "list", usd=usd, estimate=True if estimate else None), unpriced


def proxy_activity(
    call: ToolCallBlock,
    result: HostedResult,
    *,
    source_names: SourceNames = _no_names,
    source_state: Optional[SourceState] = None,
    duration_ms: Optional[int] = None,
) -> HostedActivity:
    """A hosted call that ran outside the model: the model's own call, and the record its search executor
    (``proxy``) or image endpoint (``direct``) returned.

    With ``attempts`` (§4.3) the chain alone says who ran it and what it cost: the last tried entry, and the sum of
    the tried entries. ``execution`` then adds only the model of that same entry, which the entry does not store;
    a chain that tried nothing names no executor, whatever ``execution`` says. Without them, the released reading
    holds, and a ``search_api`` origin still marks a search service.
    """

    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    subject = next(
        (value for name in ("query", "prompt") if isinstance(value := arguments.get(name), str) and value),
        None,
    )
    fetch = result.record if isinstance(result.record, WebFetchRecord) else None
    if fetch is not None:
        from urllib.parse import urlsplit
        try:
            subject = urlsplit(fetch.requested_url).hostname or fetch.requested_url
        except ValueError:
            subject = fetch.requested_url
    size = arguments.get("size") if isinstance(arguments.get("size"), str) else None
    execution = result.execution
    attempts = None
    last = switch = None
    executor = execution
    if result.record.attempts is None:
        cost, at_least = _chain_cost(execution)
    else:
        attempts = tuple(_view(attempt, source_names, source_state, call.name) for attempt in result.record.attempts)
        index = _last_tried(attempts)
        last = attempts[index] if index is not None else None
        if last is None or execution is None or execution.executor_source_id != last.source_id:
            executor = None
        cost, at_least = _attempts_cost(attempts)
        switch = _switch(result.record.status, attempts, index)
    origin = executor.executor_origin if executor is not None else None
    service = isinstance(origin, ServiceExecutorOrigin)
    kind = last.kind if last is not None else (
        "local" if service and origin.api == "local_fetch" else "service" if service else None
    )
    # What the executor reports it did (an image's rendered prompt and resolved size and quality), read as a native
    # call's block action is.
    ran = result.action if result.action is not None and result.action.type == "generate" else None
    if origin is not None and not service and kind != "service":
        model = origin.model
    else:
        # The model a refused image call named: it never moves to another (§7.4). A search service has no model:
        # its catalog service id is not shown (§4.3).
        model = ran.model if ran is not None and attempts is None and origin is None else None
    if last is not None:
        source_id, source_name = last.source_id, last.label
    elif executor is not None:
        source_id, source_name = executor.executor_source_id, source_names(executor.executor_source_id)
    else:
        source_id = source_name = None
    return HostedActivity(
        call_id=call.id,
        tool=call.name,
        # An image call is direct even when it was refused before it ran, with no execution to say so.
        mode=execution.mode if execution is not None else ("direct" if call.name == "image_generate" else "proxy"),
        status=result.record.status,
        error=result.record.error,
        error_code=result.record.error_code,
        subject=subject,
        action=None,
        extra_queries=(),
        provider=origin.provider if origin is not None else None,
        source_id=source_id,
        source_name=source_name,
        model=model,
        sources=() if fetch is not None else tuple(result.record.sources),
        images=() if fetch is not None else tuple(result.record.images or ()),
        size=(ran.size if ran is not None and ran.size else size),
        quality=ran.quality if ran is not None else None,
        revised_prompt=ran.prompt if ran is not None else None,
        cost=cost,
        cost_calls=1,
        duration_ms=duration_ms,
        fallback_source_id=result.fallback_from.source_id if result.fallback_from is not None else None,
        fallback_source_name=(
            source_names(result.fallback_from.source_id) if result.fallback_from is not None else None
        ),
        route_switch=result.route_switch,
        cost_at_least=at_least,
        attempts=attempts,
        executor_kind=kind,
        fetch=fetch,
        switch=switch,
        policy_sources=(tuple(dict.fromkeys(a.source_id for a in fetch.attempts if a.kind == "service" and a.outcome != "skipped"))
                        if fetch is not None and fetch.error_code in {"fetch_url_blocked", "fetch_dns_unverified"} else None),
    )


def _chain_cost(execution: Optional[ProxyExecution]) -> tuple[Optional[HostedCost], bool]:
    """What a proxied call cost over every executor attempt it billed, and whether that is only a lower bound (§6).

    ``execution.cost`` is the last attempt's own, and ``failed_attempts`` the earlier billed ones. Every attempt
    included is ``included``; otherwise the known figures are summed, ``reported`` only when each is. An attempt
    without a cost makes the sum a lower bound, never a complete figure; with no priced attempt to sum, the cost is
    unknown rather than "at least $0".
    """

    if execution is None:
        return None, False
    costs = [*(attempt.cost for attempt in execution.failed_attempts), execution.cost]
    known = [cost for cost in costs if cost is not None]
    unpriced = len(known) < len(costs)
    priced = [cost for cost in known if cost.basis != "included"]
    if not priced:
        return (HostedCost(basis="included"), False) if known and not unpriced else (None, False)
    total = round(sum(cost.usd or 0.0 for cost in priced), 6)
    if total == 0 and unpriced:
        return None, False
    basis = "reported" if all(cost.basis == "reported" for cost in priced) else "list"
    return HostedCost(basis=basis, usd=total), unpriced


def _runner_label(activity: HostedActivity, lang: str, escape: Callable[[str], str]) -> Optional[str]:
    """Who ran a call, as the IM line names it: its vendor's catalog label, else the Source's own name.

    A Source added as a compatible endpoint has the vendor id ``custom``, which names no vendor, so its name is the
    only thing that tells the reader where the call ran. A search service is named by its Source first, as the user
    named it (§4.3). A Source name is the user's text, so it is made inert. A vendor id with neither stays as it is,
    as for a row stored without a Source.
    """

    from vibe.model_hub_runtime.api_key_vendors import catalog_api_key_vendor_label

    if activity.source_id in _BUILTIN_LABELS:
        return i18n_t(f"hostedTools.{_BUILTIN_LABELS[activity.source_id]}", lang)
    label = catalog_api_key_vendor_label(activity.provider) if activity.provider else None
    if label and activity.executor_kind != "service":
        return label
    if activity.source_name:
        return _untrusted(activity.source_name, escape)
    return label or activity.provider


def _usd(value: float) -> str:
    if value >= 0.1:
        return f"{value:.2f}"
    # Small per-call figures keep their first significant digits: $0.006, not $0.01.
    return f"{value:.3f}".rstrip("0").rstrip(".") if value >= 0.01 else f"{value:.4f}".rstrip("0").rstrip(".")


def _clip(text: str, limit: int = LINE_SUBJECT_MAX_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _untrusted(text: str, escape: Callable[[str], str]) -> str:
    """Text Avibe did not write (a model's query, a provider's error, a user's Source or model name), as the IM line
    carries it: its mentions and masked links made inert for every platform, then escaped for the destination's
    markup. A platform escape alone leaves Discord's ``@everyone`` and ``[label](url)`` live."""

    return escape(plain_untrusted_text(text))


def _ran(activity: HostedActivity, lang: str, escape: Callable[[str], str]) -> list[Optional[str]]:
    """How a completed call ran and what it produced, as the IM line names them.

    A direct image call names the image model and the image's size rather than a vendor: the model is what the call
    picked, and the image is the result. A proxied search names its executor's model beside the vendor, and a search
    that moved past failing Sources says so in place of how it ran.
    """

    if activity.mode == "direct":
        first = activity.images[0] if activity.images else None
        size = f"{first.width}×{first.height}" if first is not None and first.width and first.height else None
        more = _count(activity, lang) if len(activity.images) > 1 else None
        return [_untrusted(activity.model, escape) if activity.model else None, size, more]
    vendor = _runner_label(activity, lang, escape)
    if vendor and activity.mode == "proxy" and activity.route_switch == "native_interrupted":
        return [
            i18n_t("hostedTools.mode.nativeInterrupted", lang, provider=vendor),
            _switch_text(activity, lang, escape) if activity.switch is not None else None,
            _count(activity, lang),
        ]
    if vendor and activity.mode == "proxy" and activity.fallback_source_id is not None:
        # Why it ran by proxy: the turn's own Source refused the native tool (§8.3, mockup 06).
        refused_by = _untrusted(activity.fallback_source_name or activity.fallback_source_id, escape)
        mode = i18n_t("hostedTools.mode.fallback", lang, source=refused_by, provider=vendor)
    elif activity.switch is not None:
        # Why it ran where it did: the Sources before it failed or were blocked (§6.5).
        mode = _switch_text(activity, lang, escape)
    elif not vendor:
        mode = None
    elif activity.executor_kind in {"service", "local"}:
        # Preserve the released search label; fetch names every executor alike.
        mode = (i18n_t("hostedTools.mode.service", lang, provider=vendor)
                if activity.source_id == KEYLESS_EXA_SOURCE_ID and activity.fetch is None else vendor)
    elif activity.mode == "proxy" and activity.model:
        mode = i18n_t("hostedTools.mode.proxyModel", lang, provider=vendor, model=_untrusted(activity.model, escape))
    else:
        mode = i18n_t(f"hostedTools.mode.{activity.mode}", lang, provider=vendor)
    return [mode, _count(activity, lang)]


def _count(activity: HostedActivity, lang: str) -> Optional[str]:
    if activity.fetch is not None:
        count = len(activity.fetch.content)
        size = i18n_t("hostedTools.fetch.characters", lang, count=count)
        return f"{size} · {i18n_t('hostedTools.fetch.truncated', lang)}" if activity.fetch.truncated else size
    if activity.tool == "image_generate":
        count, unit = len(activity.images), "image"
    elif activity.tool == "x_search":
        count, unit = len(activity.sources), "post"
    else:
        count, unit = len(activity.sources), "source"
    if count == 0:
        return None
    key = f"hostedTools.count.{unit}One" if count == 1 else f"hostedTools.count.{unit}s"
    return i18n_t(key, lang, count=count)


def _cost(activity: HostedActivity, lang: str) -> Optional[str]:
    cost = activity.cost
    if cost is None:
        return None
    if (activity.source_id in _BUILTIN_LABELS
            and cost.usd == 0 and not activity.cost_at_least):
        text = i18n_t("hostedTools.cost.free", lang)
    elif cost.basis == "included":
        text = i18n_t("hostedTools.cost.included", lang)
    elif cost.usd is None:
        return None
    else:
        # A zero is exact; any other figure is approximate when it is a list price or the vendor's own estimate.
        approximate = cost.usd != 0 and (cost.basis == "list" or bool(cost.estimate))
        text = i18n_t(f"hostedTools.cost.{'list' if approximate else 'reported'}", lang, usd=_usd(cost.usd))
    if activity.cost_at_least:
        text = i18n_t("hostedTools.cost.atLeast", lang, cost=text)
    if activity.cost_calls > 1:
        text = i18n_t("hostedTools.cost.calls", lang, cost=text, calls=activity.cost_calls)
    return text


def _error_text(activity: HostedActivity, lang: str) -> str:
    """Why the call did not complete: in the reader's language when Avibe worded it, else as the provider said it.

    Only a known code is localized. One from a newer producer reads as its raw
    ``error``, as a provider's own reason does.
    """

    if activity.error_code in HOSTED_ERROR_CODES:
        namespace = "fetchErrorCode" if activity.tool == "web_fetch" and activity.error_code.startswith("service_") else "errorCode"
        return i18n_t(f"hostedTools.{namespace}.{activity.error_code}", lang)
    return activity.error or ""


def _switch_text(activity: HostedActivity, lang: str, escape: Callable[[str], str]) -> str:
    switch = activity.switch
    assert switch is not None
    # Switch labels are the projection's recorded names. Localize only an
    # unambiguous built-in label; never rename an ordinary user Source.
    def name_label(name: str) -> str:
        ids = {a.source_id for a in activity.attempts or () if a.label == name}
        return _source_label(next(iter(ids)), name, lang, escape) if len(ids) == 1 else _untrusted(name, escape)

    names = i18n_t("hostedTools.listSeparator", lang).join(name_label(name) for name in switch.sources)
    provider = _source_label(activity.source_id, switch.to, lang, escape)
    if switch.reason_key == "reason.service_rate_limited":
        return i18n_t("hostedTools.rateLimitSwitch", lang, sources=names, provider=provider)
    failed = f"{names} {i18n_t(f'hostedTools.{switch.reason_key}', lang)}"
    return i18n_t("hostedTools.switch", lang, failed=failed, provider=provider)


def _source_label(source_id: Optional[str], label: str, lang: str, escape: Callable[[str], str]) -> str:
    key = _BUILTIN_LABELS.get(source_id)
    return i18n_t(f"hostedTools.{key}", lang) if key else _untrusted(label, escape)


def _attempt_items(activity: HostedActivity, lang: str, escape: Callable[[str], str]) -> Optional[str]:
    """Every Source a call that did not complete reached, whichever supplied its reason: each with what it needs
    now, else what happened to it (§4.3)."""

    if activity.completed or not activity.attempts:
        return None
    items = [
        f"{_source_label(attempt.source_id, attempt.label, lang, escape)} {i18n_t(f'hostedTools.{key}', lang)}"
        for attempt in activity.attempts
        if (key := attempt.remedy_key or attempt.reason_key)
    ]
    return i18n_t("hostedTools.itemSeparator", lang).join(items) or None


def hosted_line(activity: HostedActivity, lang: str, *, escape: Callable[[str], str] = lambda text: text) -> str:
    """The one-line form IM shows per hosted call.

    ``🔎 Web search "query" · native (xAI) · 6 sources · $0.01``, or for an image
    ``🖼 Image generation "prompt" · gpt-image-2 · 1536×1024 · $0.02``. A call that did
    not complete says why instead of how it ran or what it found, names every
    Source its search chain reached with what each needs now, and keeps a
    known cost (``HostedActivity.completed``). ``escape`` is the destination
    formatter's literal-text operation (``escape_special_chars``), so a quoted
    query such as ``*domain*`` reads as written rather than as markup.
    """

    emoji = "🖼" if activity.tool == "image_generate" else "🌐" if activity.tool == "web_fetch" else "🔎"
    parts: list[Optional[str]] = [f"{emoji} {i18n_t(f'hostedTools.tool.{activity.tool}', lang)}"]
    if activity.subject:
        subject = _untrusted(_clip(activity.subject), escape)
        parts[0] += " " + (subject if activity.fetch is not None else i18n_t("hostedTools.quoted", lang, subject=subject))
    if activity.completed:
        parts.extend(_ran(activity, lang, escape))
    elif activity.interruption is not None:
        parts.append(i18n_t(
            "hostedTools.interruption.message", lang,
            reason=i18n_t(f"hostedTools.interruption.{activity.interruption}", lang),
        ))
    elif activity.policy_sources is not None:
        names = {a.source_id: a.label for a in activity.attempts or ()}
        providers = i18n_t("hostedTools.listSeparator", lang).join(
            _source_label(sid, names.get(sid, sid), lang, escape) for sid in activity.policy_sources
        )
        parts.append(i18n_t("hostedTools.fetch.policy", lang))
        parts.append(i18n_t("hostedTools.fetch.forwarded" if providers else "hostedTools.fetch.notForwarded", lang, providers=providers))
    else:
        parts.append(
            i18n_t(f"hostedTools.status.{activity.status}", lang, error=_untrusted(_error_text(activity, lang), escape))
        )
        parts.append(_attempt_items(activity, lang, escape))
    parts.append(_cost(activity, lang))
    return " · ".join(part for part in parts if part)
