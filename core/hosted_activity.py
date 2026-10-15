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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from core.agent_core.messages import (
    HOSTED_ERROR_CODES,
    AssistantMessage,
    HostedAction,
    HostedCost,
    HostedImage,
    HostedResult,
    HostedSource,
    HostedToolBlock,
    ProxyExecution,
    ToolCallBlock,
)
from vibe.i18n import t as i18n_t

HOSTED_ACTIVITY_VERSION = 1

# The longest query or prompt an IM line quotes; the Web card shows it whole.
LINE_SUBJECT_MAX_CHARS = 60

SourceNames = Callable[[str], Optional[str]]


def _no_names(_source_id: str) -> Optional[str]:
    return None


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
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.extra_queries:
            payload["extra_queries"] = list(self.extra_queries)
        if self.cost is not None:
            cost: dict[str, Any] = {"basis": self.cost.basis, "calls": self.cost_calls}
            if self.cost.usd is not None:
                cost["usd"] = self.cost.usd
            if self.cost_at_least:
                cost["at_least"] = True
            payload["cost"] = cost
        return payload


def _source_payload(source: HostedSource) -> dict[str, Any]:
    out: dict[str, Any] = {"url": source.url}
    for name in ("title", "snippet", "page_age"):
        value = getattr(source, name)
        if value:
            out[name] = value
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
    for name in ("query", "prompt", "pattern", "url", "ref", "name"):
        value = getattr(action, name)
        if value:
            return value, ()
    return None, ()


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

    blocks = [block for block in message.content if isinstance(block, HostedToolBlock)]
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
            )
        )
    return activities


def proxy_activity(
    call: ToolCallBlock,
    result: HostedResult,
    *,
    source_names: SourceNames = _no_names,
    duration_ms: Optional[int] = None,
) -> HostedActivity:
    """A hosted call that ran outside the model: the model's own call, and the record its search executor
    (``proxy``) or image endpoint (``direct``) returned."""

    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    subject = next(
        (value for name in ("query", "prompt") if isinstance(value := arguments.get(name), str) and value),
        None,
    )
    size = arguments.get("size") if isinstance(arguments.get("size"), str) else None
    execution = result.execution
    cost, at_least = _chain_cost(execution)
    # What the executor reports it did (an image's rendered prompt and resolved size and quality), read as a native
    # call's block action is.
    ran = result.action if result.action is not None and result.action.type == "generate" else None
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
        provider=execution.executor_origin.provider if execution is not None else None,
        source_id=execution.executor_source_id if execution is not None else None,
        source_name=source_names(execution.executor_source_id) if execution is not None else None,
        model=(
            execution.executor_origin.model
            if execution is not None
            # The model a refused image call named: it never moves to another (§7.4).
            else ran.model if ran is not None else None
        ),
        sources=tuple(result.record.sources),
        images=tuple(result.record.images or ()),
        size=(ran.size if ran is not None and ran.size else size),
        quality=ran.quality if ran is not None else None,
        revised_prompt=ran.prompt if ran is not None else None,
        cost=cost,
        cost_calls=1,
        duration_ms=duration_ms,
        cost_at_least=at_least,
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
    basis = "reported" if all(cost.basis == "reported" for cost in priced) else "list"
    return HostedCost(basis=basis, usd=round(sum(cost.usd or 0.0 for cost in priced), 6)), unpriced


def _vendor_label(provider: Optional[str]) -> Optional[str]:
    if not provider:
        return None
    from vibe.model_hub_runtime.api_key_vendors import catalog_api_key_vendor_label

    return catalog_api_key_vendor_label(provider) or provider


def _usd(value: float) -> str:
    if value >= 0.1:
        return f"{value:.2f}"
    # Small per-call figures keep their first significant digits: $0.006, not $0.01.
    return f"{value:.3f}".rstrip("0").rstrip(".") if value >= 0.001 else f"{value:.4f}".rstrip("0").rstrip(".")


def _clip(text: str, limit: int = LINE_SUBJECT_MAX_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _ran(activity: HostedActivity, lang: str) -> list[Optional[str]]:
    """How a completed call ran and what it produced, as the IM line names them.

    A direct image call names the image model and the image's size rather than a vendor: the model is what the call
    picked, and the image is the result. A proxied search names its executor's model beside the vendor.
    """

    if activity.mode == "direct":
        first = activity.images[0] if activity.images else None
        size = f"{first.width}×{first.height}" if first is not None and first.width and first.height else None
        more = _count(activity, lang) if len(activity.images) > 1 else None
        return [activity.model, size, more]
    vendor = _vendor_label(activity.provider)
    if not vendor:
        mode = None
    elif activity.mode == "proxy" and activity.model:
        mode = i18n_t("hostedTools.mode.proxyModel", lang, provider=vendor, model=activity.model)
    else:
        mode = i18n_t(f"hostedTools.mode.{activity.mode}", lang, provider=vendor)
    return [mode, _count(activity, lang)]


def _count(activity: HostedActivity, lang: str) -> Optional[str]:
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
    if cost.basis == "included":
        text = i18n_t("hostedTools.cost.included", lang)
    elif cost.usd is None:
        return None
    else:
        key = "hostedTools.cost.list" if cost.basis == "list" else "hostedTools.cost.reported"
        text = i18n_t(key, lang, usd=_usd(cost.usd))
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
        return i18n_t(f"hostedTools.errorCode.{activity.error_code}", lang)
    return activity.error or ""


def hosted_line(activity: HostedActivity, lang: str, *, escape: Callable[[str], str] = lambda text: text) -> str:
    """The one-line form IM shows per hosted call.

    ``🔎 Web search "query" · native (xAI) · 6 sources · $0.01``, or for an image
    ``🖼 Image generation "prompt" · gpt-image-2 · 1536×1024 · $0.02``. A call that did
    not complete says why instead of how it ran or what it found, and keeps a
    known cost (``HostedActivity.completed``). ``escape`` makes the quoted
    subject safe for the destination's markup.
    """

    emoji = "🖼" if activity.tool == "image_generate" else "🔎"
    parts: list[Optional[str]] = [f"{emoji} {i18n_t(f'hostedTools.tool.{activity.tool}', lang)}"]
    if activity.subject:
        parts[0] += " " + i18n_t("hostedTools.quoted", lang, subject=escape(_clip(activity.subject)))
    if activity.completed:
        parts.extend(_ran(activity, lang))
    else:
        parts.append(i18n_t(f"hostedTools.status.{activity.status}", lang, error=escape(_error_text(activity, lang))))
    parts.append(_cost(activity, lang))
    return " · ".join(part for part in parts if part)
