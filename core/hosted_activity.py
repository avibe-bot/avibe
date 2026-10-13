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
    AssistantMessage,
    HostedAction,
    HostedCost,
    HostedImage,
    HostedResult,
    HostedSource,
    HostedToolBlock,
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
    mode: Literal["native", "proxy"]
    status: str
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
    # The committed response row a native call came in. Its card is ordered just
    # before that row, as the call happened before the text that reads its result.
    response_id: Optional[str] = None

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
    """A proxied hosted call: the model's own call, and the record its executor returned."""

    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    subject = next(
        (value for name in ("query", "prompt") if isinstance(value := arguments.get(name), str) and value),
        None,
    )
    size = arguments.get("size") if isinstance(arguments.get("size"), str) else None
    execution = result.execution
    return HostedActivity(
        call_id=call.id,
        tool=call.name,
        mode="proxy",
        status=result.record.status,
        error=result.record.error,
        subject=subject,
        action=None,
        extra_queries=(),
        provider=execution.executor_origin.provider if execution is not None else None,
        source_id=execution.executor_source_id if execution is not None else None,
        source_name=source_names(execution.executor_source_id) if execution is not None else None,
        model=execution.executor_origin.model if execution is not None else None,
        sources=tuple(result.record.sources),
        images=tuple(result.record.images or ()),
        size=size,
        quality=None,
        revised_prompt=None,
        cost=execution.cost if execution is not None else None,
        cost_calls=1,
        duration_ms=duration_ms,
    )


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


def _count(activity: HostedActivity, lang: str) -> Optional[str]:
    if activity.tool == "image_generation":
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
    if activity.cost_calls > 1:
        text = i18n_t("hostedTools.cost.calls", lang, cost=text, calls=activity.cost_calls)
    return text


def hosted_line(activity: HostedActivity, lang: str, *, escape: Callable[[str], str] = lambda text: text) -> str:
    """The one-line form IM shows per hosted call.

    ``🔎 Web search "query" · native (xAI) · 6 sources · $0.01``. A failed call
    says why instead of counting results it never had. ``escape`` makes the
    quoted subject safe for the destination's markup.
    """

    emoji = "🖼" if activity.tool == "image_generation" else "🔎"
    parts = [f"{emoji} {i18n_t(f'hostedTools.tool.{activity.tool}', lang)}"]
    if activity.subject:
        parts[0] += " " + i18n_t("hostedTools.quoted", lang, subject=escape(_clip(activity.subject)))
    if activity.status in ("failed", "incomplete"):
        parts.append(i18n_t(f"hostedTools.status.{activity.status}", lang, error=escape(activity.error or "")))
        return " · ".join(parts)
    vendor = _vendor_label(activity.provider)
    if vendor:
        parts.append(i18n_t(f"hostedTools.mode.{activity.mode}", lang, provider=vendor))
    if activity.status == "pending":
        parts.append(i18n_t("hostedTools.status.pending", lang))
        return " · ".join(parts)
    for part in (_count(activity, lang), _cost(activity, lang)):
        if part:
            parts.append(part)
    return " · ".join(parts)
