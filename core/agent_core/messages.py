"""C-1 canonical message model (``agent-core-contracts/message.schema.json``).

Vendor-neutral messages exchanged by the provider adapters, the loop, and the
transcript store. ``message_to_dict`` / ``message_from_dict`` produce and read
the persisted form stored in ``messages.content_json.model`` and in tool-result
rows, so the field names here are a shipped surface.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Union

logger = logging.getLogger(__name__)

ProtocolName = Literal["anthropic", "openai_chat", "openai_responses", "google"]
PROTOCOLS: tuple[str, ...] = ("anthropic", "openai_chat", "openai_responses", "google")

StopReason = Literal["stop", "length", "tool_use", "refusal", "safety", "aborted", "error"]
STOP_REASONS: tuple[str, ...] = ("stop", "length", "tool_use", "refusal", "safety", "aborted", "error")

IMAGE_MIME_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/gif", "image/webp")

_LARGE_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^src_[a-z0-9]{8,}$")
_COUNTER_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The tools a provider runs natively, each a ``HostedToolBlock`` in assistant content (``vibey-hosted-tools.md`` §4.2).
HostedToolName = Literal["web_search", "x_search"]
HOSTED_TOOLS: tuple[str, ...] = ("web_search", "x_search")
#: Tools whose function results carry hosted metadata, including dormant fetch history.
HOSTED_RESULT_TOOLS: tuple[str, ...] = (*HOSTED_TOOLS, "image_generate", "web_fetch")
EXECUTION_MODES: tuple[str, ...] = ("proxy", "direct")
HostedStatus = Literal["completed", "failed", "incomplete", "pending"]
HOSTED_STATUSES: tuple[str, ...] = ("completed", "failed", "incomplete", "pending")
HostedAttemptKind = Literal["service", "model", "local"]
HostedAttemptOutcome = Literal["completed", "failed", "skipped"]
HOSTED_ATTEMPT_KINDS: tuple[str, ...] = ("service", "model", "local")
HOSTED_ATTEMPT_OUTCOMES: tuple[str, ...] = ("completed", "failed", "skipped")
HOSTED_SKIP_REASONS: tuple[str, ...] = ("blocked", "disabled", "cooling", "query_too_long", "unavailable", "unsupported_type", "service_busy")
HOSTED_ERROR_MAX_CHARS = 400
#: The largest hosted count a summary holds: the largest integer every JSON reader holds exactly. A provider's count
#: past it is no count, and no price or display has to survive one.
MAX_HOSTED_COUNT = 2**53 - 1
#: The stable code of each hosted failure Avibe words itself (``HostedRecord.error_code``), by which a display localizes
#: it; a provider's own reason has none. A record loads with any snake_case code, so one a newer build wrote still reads.
#: ``image_not_streamed`` has no producer since native images went (v2); it stays for the records already written.
HOSTED_ERROR_CODES: tuple[str, ...] = (
    "provider_no_reason",
    "provider_no_image",
    "image_not_streamed",
    "image_not_saved",
    "image_no_store",
    "executor_no_image",
    "gateway_unreachable",
    "gateway_refused",
    "gateway_no_result",
    "image_param_unsupported",
    "image_model_unavailable",
    "image_argument_invalid",
    "image_reference_unreadable",
    # The gateway's search executor and image endpoint (vibey-hosted-tools.md §6, §7.4).
    "provider_refused",
    "provider_unavailable",
    "provider_timed_out",
    "provider_unreachable",
    "provider_unreadable",
    "provider_too_large",
    "provider_image_unusable",
    "search_not_run",
    "search_failed",
    "response_ended_early",
    "service_key_invalid",
    "service_balance",
    "service_limit",
    "service_unavailable",
    "service_rate_limited",
    "service_refused",
    "service_unreadable",
    "search_query_too_long",
    "search_sources_blocked",
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
    "fetch_sources_blocked",
)
COST_BASES: tuple[str, ...] = ("reported", "list", "included")
SOURCE_BILLINGS: tuple[str, ...] = ("monthly", "metered")


def _require(value: Any, kind: type, what: str, *, optional: bool = False) -> None:
    """Exact-type check shared by construction and reading: what one accepts, the other accepts."""
    if value is None and optional:
        return
    # bool is an int subclass in Python; a boolean is never accepted as a number.
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ValueError(f"{what} must be {kind.__name__}")


@dataclass(frozen=True)
class LargeRef:
    """Reserved reference to content stored outside the row; v1 never writes it."""

    ref: str
    bytes: int

    def __post_init__(self) -> None:
        _require(self.ref, str, "ref")
        _require(self.bytes, int, "bytes")
        if not _LARGE_REF_RE.fullmatch(self.ref) or self.bytes < 0:
            raise ValueError(f"invalid large-content reference: {self.ref!r}")


def _require_text(value: Any, what: str, *, optional: bool = False) -> None:
    _require(value, str, what, optional=optional)
    if value is not None and not value:
        raise ValueError(f"{what} must not be empty")


@dataclass(frozen=True)
class Citation:
    """A source the provider attached to assistant text; presentation only, never replayed.

    ``start`` and ``end`` come together, as a half-open code-point range into the block's text.
    """

    url: str
    title: Optional[str] = None
    cited_text: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text(self.url, "citation url")
        _require(self.title, str, "citation title", optional=True)
        _require(self.cited_text, str, "cited_text", optional=True)
        _require(self.start, int, "citation start", optional=True)
        _require(self.end, int, "citation end", optional=True)
        if (self.start is None) != (self.end is None):
            raise ValueError("a citation range needs both start and end")
        if self.start is not None and not 0 <= self.start <= self.end:
            raise ValueError("a citation range must satisfy 0 <= start <= end")


@dataclass(frozen=True)
class TextBlock:
    text: Optional[str] = None
    ref: Optional[LargeRef] = None
    citations: Optional[tuple[Citation, ...]] = None
    """Assistant text only, and only with inline ``text``; absent when the provider attached none."""

    def __post_init__(self) -> None:
        _require(self.text, str, "text", optional=True)
        _require(self.ref, LargeRef, "ref", optional=True)
        if (self.text is None) == (self.ref is None):
            raise ValueError("a text block holds exactly one of text or ref")
        if self.citations is None:
            return
        if not isinstance(self.citations, tuple) or not self.citations:
            raise ValueError("citations, when present, are a non-empty tuple")
        if not all(isinstance(citation, Citation) for citation in self.citations):
            raise ValueError("citations must be Citation values")
        if self.text is None:
            raise ValueError("citations index into inline text")
        if any(c.end is not None and c.end > len(self.text) for c in self.citations):
            raise ValueError("a citation range must lie within the text")


@dataclass(frozen=True)
class ImageBlock:
    mime_type: str
    media_token: str
    name: Optional[str] = None

    def __post_init__(self) -> None:
        _require(self.mime_type, str, "mime_type")
        _require(self.media_token, str, "media_token")
        _require(self.name, str, "name", optional=True)
        if self.mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {self.mime_type}")
        if not self.media_token:
            raise ValueError("an image block needs a media token")
        if self.name is not None and not self.name:
            raise ValueError("an image name, when present, must not be empty")


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: Optional[str] = None
    redacted: bool = False
    """True for reasoning the provider returned only in opaque form: ``signature`` holds that payload, ``text`` is empty."""

    def __post_init__(self) -> None:
        _require(self.text, str, "thinking text")
        _require(self.signature, str, "signature", optional=True)
        _require(self.redacted, bool, "redacted")
        if self.signature is not None and not self.signature:
            raise ValueError("a signature, when present, must not be empty")
        if self.redacted and (self.signature is None or self.text):
            raise ValueError("redacted thinking keeps its payload in signature and has empty text")


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    native_id: Optional[str] = None
    signature: Optional[str] = None
    """Opaque provider payload on the call itself (Gemini ``thoughtSignature``); same rules as a thinking signature."""

    def __post_init__(self) -> None:
        _require(self.id, str, "tool call id")
        _require(self.name, str, "tool name")
        _require(self.arguments, dict, "arguments")
        require_json_value(self.arguments, "arguments")
        # Detach from the caller's object so later mutation of the input cannot reach this block.
        object.__setattr__(self, "arguments", copy.deepcopy(self.arguments))
        _require(self.native_id, str, "native_id", optional=True)
        _require(self.signature, str, "signature", optional=True)
        if not self.id or not self.name:
            raise ValueError("a tool call needs an id and a name")
        if self.native_id is not None and not self.native_id:
            raise ValueError("a native id, when present, must not be empty")
        if self.signature is not None and not self.signature:
            raise ValueError("a signature, when present, must not be empty")


def require_json_value(value: Any, what: str) -> None:
    """Refuse anything that would not survive a JSON round trip unchanged.

    Allowed: dict with str keys, list, str, int, finite float, bool, None. A tuple,
    bytes, a non-str key, or NaN would be coerced or rejected on persistence.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{what} must be a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            require_json_value(item, f"{what}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{what} keys must be strings")
            require_json_value(item, f"{what}.{key}")
        return
    raise ValueError(f"{what} must be JSON (got {type(value).__name__})")


def _require_blocks(content: Any, allowed: tuple[type, ...], what: str) -> None:
    if not isinstance(content, tuple) or not all(isinstance(block, allowed) for block in content):
        raise ValueError(f"{what} content must be a tuple of {', '.join(k.__name__ for k in allowed)}")


def _refuse_citations(content: tuple[Any, ...], what: str) -> None:
    if any(isinstance(block, TextBlock) and block.citations is not None for block in content):
        raise ValueError(f"{what} text carries no citations")


# --- hosted tools (``vibey-hosted-tools.md`` §4.2) ----------------------------
#
# A native hosted call is one ``HostedToolBlock`` in assistant content. A proxied
# call is an ordinary ``ToolCallBlock`` whose ``ToolResultMessage`` carries
# ``hosted``. Neither ever holds image bytes: a generated image is a file, and
# the record holds its path.

# Per action type: (owning tool or None for any hosted tool, required fields, optional fields).
_HOSTED_ACTIONS: dict[str, tuple[Optional[str], dict[str, type], dict[str, type]]] = {
    "search": ("web_search", {"queries": list}, {}),
    "open_page": ("web_search", {"url": str}, {}),
    "find_in_page": ("web_search", {"url": str, "pattern": str}, {}),
    "x_search": ("x_search", {"variant": str, "query": str}, {"limit": int, "mode": str}),
    "x_user_search": ("x_search", {"query": str}, {}),
    "x_thread_fetch": ("x_search", {"ref": str}, {}),
    "generate": ("image_generate", {}, {"prompt": str, "size": str, "quality": str, "model": str}),
    "other": (None, {"name": str}, {}),
}
_X_SEARCH_VARIANTS = ("keyword", "semantic")
_X_SEARCH_MODES = ("Latest", "Top")


@dataclass(frozen=True)
class HostedAction:
    """What a hosted call did, provider-neutral. Only the fields of its ``type`` are set."""

    type: str
    queries: Optional[tuple[str, ...]] = None
    url: Optional[str] = None
    pattern: Optional[str] = None
    variant: Optional[str] = None
    query: Optional[str] = None
    limit: Optional[int] = None
    mode: Optional[str] = None
    ref: Optional[str] = None
    prompt: Optional[str] = None
    size: Optional[str] = None
    quality: Optional[str] = None
    name: Optional[str] = None
    #: ``generate`` only: the image model the call ran on.
    model: Optional[str] = None

    def __post_init__(self) -> None:
        _require(self.type, str, "hosted action type")
        if self.type not in _HOSTED_ACTIONS:
            raise ValueError(f"unknown hosted action type: {self.type}")
        _, required, optional = _HOSTED_ACTIONS[self.type]
        for name in _HOSTED_ACTION_FIELDS:
            value = getattr(self, name)
            if name in required and value is None:
                raise ValueError(f"a {self.type} action needs {name}")
            if name not in required and name not in optional and value is not None:
                raise ValueError(f"a {self.type} action has no {name}")
        if self.queries is not None:
            if not isinstance(self.queries, tuple) or not self.queries:
                raise ValueError("search queries are a non-empty tuple")
            for query in self.queries:
                _require_text(query, "search query")
        for name in ("url", "pattern", "query", "ref", "prompt", "size", "quality", "name", "model"):
            _require_text(getattr(self, name), f"action {name}", optional=True)
        _require(self.variant, str, "action variant", optional=True)
        if self.variant is not None and self.variant not in _X_SEARCH_VARIANTS:
            raise ValueError(f"unknown x_search variant: {self.variant}")
        _require(self.mode, str, "action mode", optional=True)
        if self.mode is not None and self.mode not in _X_SEARCH_MODES:
            raise ValueError(f"unknown x_search mode: {self.mode}")
        _require(self.limit, int, "action limit", optional=True)
        if self.limit is not None and self.limit < 1:
            raise ValueError("an x_search limit is at least 1")

    def fits(self, tool: str) -> bool:
        owner = _HOSTED_ACTIONS[self.type][0]
        return owner is None or owner == tool


_HOSTED_ACTION_FIELDS: tuple[str, ...] = (
    "queries", "url", "pattern", "variant", "query", "limit", "mode", "ref", "prompt", "size", "quality", "name",
    "model",
)


@dataclass(frozen=True)
class HostedSource:
    url: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    page_age: Optional[str] = None

    def __post_init__(self) -> None:
        _require_text(self.url, "source url")
        for name in ("title", "snippet", "page_age"):
            _require(getattr(self, name), str, f"source {name}", optional=True)


@dataclass(frozen=True)
class HostedImage:
    """A generated image saved as a local file; the model sees only this record. ``bytes`` is the file's size."""

    path: str
    mime: str
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text(self.path, "image path")
        _require(self.mime, str, "image mime")
        if self.mime not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type: {self.mime}")
        for name in ("width", "height", "bytes"):
            value = getattr(self, name)
            _require(value, int, f"image {name}", optional=True)
            if value is not None and value < 1:
                raise ValueError(f"image {name} is at least 1")


def image_deliverable(image: HostedImage) -> bool:
    """Whether a surface can send this generated image: its path names a file absolutely. A relative path, from a
    restored or foreign record, names no file anyone can attach.

    The one rule for both decisions a generated image takes part in: whether the turn's final response counts as a
    reply (``has_reply``), and which images are delivered with it. Kept in one place so a reply is never counted for
    an image delivery then skips.
    """
    return os.path.isabs(image.path)


@dataclass(frozen=True)
class HostedAttempt:
    """One candidate reached by a search chain, including candidates skipped before a request."""

    source_id: str
    label: str
    kind: HostedAttemptKind
    outcome: HostedAttemptOutcome
    error_code: Optional[str] = None
    skipped: Optional[str] = None
    retry_at: Optional[str] = None
    needs_action: Optional[str] = None
    cost: Optional[HostedCost] = None
    superseded: Optional[bool] = None

    def __post_init__(self) -> None:
        _require(self.source_id, str, "attempt source id")
        if not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise ValueError(f"invalid source id: {self.source_id!r}")
        _require_text(self.label, "attempt label")
        if len(self.label) > 64:
            raise ValueError("attempt label is at most 64 characters")
        _require(self.kind, str, "attempt kind")
        if self.kind not in HOSTED_ATTEMPT_KINDS:
            raise ValueError(f"unknown attempt kind: {self.kind}")
        _require(self.outcome, str, "attempt outcome")
        if self.outcome not in HOSTED_ATTEMPT_OUTCOMES:
            raise ValueError(f"unknown attempt outcome: {self.outcome}")
        _require(self.error_code, str, "attempt error code", optional=True)
        if self.error_code is not None and not _ERROR_CODE_RE.fullmatch(self.error_code):
            raise ValueError("attempt error code is snake_case")
        _require(self.skipped, str, "attempt skip reason", optional=True)
        if self.skipped is not None and self.skipped not in HOSTED_SKIP_REASONS:
            raise ValueError(f"unknown attempt skip reason: {self.skipped}")
        _require_text(self.retry_at, "attempt retry_at", optional=True)
        _require_text(self.needs_action, "attempt needs_action", optional=True)
        _require(self.cost, HostedCost, "attempt cost", optional=True)
        _require(self.superseded, bool, "attempt superseded", optional=True)
        if self.outcome == "skipped" and self.skipped is None:
            raise ValueError("a skipped attempt needs a skip reason")
        if self.outcome != "skipped" and self.skipped is not None:
            raise ValueError("only a skipped attempt has a skip reason")
        if self.skipped != "cooling" and self.retry_at is not None:
            raise ValueError("only a cooling attempt has retry_at")
        if self.outcome == "completed" and self.error_code is not None:
            raise ValueError("a completed attempt has no error code")


@dataclass(frozen=True)
class HostedRecord:
    """Per-call facts. ``images`` is set exactly for ``image_generate`` (possibly empty).

    ``error`` carries the reason as text, which the model reads: the provider's own, or one Avibe wrote. Every reason
    Avibe writes carries an ``error_code`` (``HOSTED_ERROR_CODES``, #2589), only beside an ``error``: the display key,
    which the Web card and the IM line render as that code's copy in the viewer's language. A reason without one is
    shown as written: the provider's own, or the one reason whose useful part is that call's own fact (the gateway's
    "no provider can run this search; add an API key from …"), which Avibe writes in the user's language.
    """

    status: HostedStatus
    error: Optional[str] = None
    sources: tuple[HostedSource, ...] = ()
    images: Optional[tuple[HostedImage, ...]] = None
    error_code: Optional[str] = None
    attempts: Optional[tuple[HostedAttempt, ...]] = None

    def __post_init__(self) -> None:
        _require(self.status, str, "hosted status")
        if self.status not in HOSTED_STATUSES:
            raise ValueError(f"unknown hosted status: {self.status}")
        _require_text(self.error, "hosted error", optional=True)
        if (self.error is not None) != (self.status in ("failed", "incomplete")):
            raise ValueError("a hosted error is present exactly for failed and incomplete records")
        if self.error is not None and len(self.error) > HOSTED_ERROR_MAX_CHARS:
            raise ValueError(f"a hosted error is at most {HOSTED_ERROR_MAX_CHARS} characters")
        _require(self.error_code, str, "hosted error code", optional=True)
        if self.error_code is not None and (self.error is None or not _ERROR_CODE_RE.fullmatch(self.error_code)):
            raise ValueError("a hosted error code is snake_case and comes only with an error")
        if not isinstance(self.sources, tuple) or not all(isinstance(s, HostedSource) for s in self.sources):
            raise ValueError("hosted sources must be a tuple of HostedSource")
        if self.images is not None and (
            not isinstance(self.images, tuple) or not all(isinstance(i, HostedImage) for i in self.images)
        ):
            raise ValueError("hosted images must be a tuple of HostedImage")
        if self.attempts is not None and (
            not isinstance(self.attempts, tuple) or not all(isinstance(a, HostedAttempt) for a in self.attempts)
        ):
            raise ValueError("hosted attempts must be a tuple of HostedAttempt")

    def check_tool(self, tool: str) -> None:
        if tool == "web_fetch":
            raise ValueError("web_fetch requires a WebFetchRecord")
        if (self.images is not None) != (tool == "image_generate"):
            raise ValueError("a hosted record lists images exactly for image_generate")


@dataclass(frozen=True)
class WebFetchRecord:
    """Immutable bounded page facts, discriminated by the enclosing tool name.

    The ordinary tool content is the same capped quotation, not a hidden page
    archive. Search's released HostedRecord is intentionally unchanged.
    """

    status: Literal["completed", "failed"]
    requested_url: str
    content: str
    size_bytes: int
    retrieved_at: str
    truncated: bool = False
    truncation_reason: Optional[Literal["budget", "provider_limit"]] = None
    budget_method: str = "utf8_bytes_div4_v1"
    final_url: Optional[str] = None
    canonical_url: Optional[str] = None
    title: Optional[str] = None
    content_type: Optional[str] = None
    source_content_type: Optional[str] = None
    source_size_bytes: Optional[int] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    attempts: tuple[HostedAttempt, ...] = ()
    transport_degraded: Optional[bool] = None
    transport_degraded_reason: Optional[str] = None
    charset_fallback: Optional[bool] = None

    def __post_init__(self) -> None:
        from datetime import datetime, timedelta
        from .web_fetch import frame_fetch_record

        if self.status not in ("completed", "failed"):
            raise ValueError("fetch status must be completed or failed")
        for name, bound in (("requested_url", 8192), ("final_url", 8192), ("canonical_url", 8192),
                            ("title", 300), ("content_type", 128), ("source_content_type", 128),
                            ("error", 1000), ("transport_degraded_reason", 400)):
            value = getattr(self, name)
            _require(value, str, f"fetch {name}", optional=name != "requested_url")
            if value is not None and len(value) > bound:
                raise ValueError(f"fetch {name} exceeds {bound} characters")
        if (self.error is not None) != (self.status == "failed") or self.error == "":
            raise ValueError("a fetch error is present exactly for failed records")
        _require(self.error_code, str, "fetch error code", optional=True)
        if self.error_code is not None and (self.error is None or not _ERROR_CODE_RE.fullmatch(self.error_code)):
            raise ValueError("a fetch error code is snake_case and comes only with an error")
        _require(self.content, str, "fetch content")
        _require(self.size_bytes, int, "fetch size_bytes")
        if self.size_bytes != len(self.content.encode("utf-8", "surrogatepass")):
            raise ValueError("fetch size_bytes must equal retained UTF-8 bytes")
        _require(self.source_size_bytes, int, "fetch source_size_bytes", optional=True)
        if self.source_size_bytes is not None and not 0 <= self.source_size_bytes <= MAX_HOSTED_COUNT:
            raise ValueError("fetch source_size_bytes is a nonnegative safe integer")
        _require(self.truncated, bool, "fetch truncated")
        if self.truncation_reason not in (None, "budget", "provider_limit") or self.truncated != (self.truncation_reason is not None):
            raise ValueError("fetch truncation must carry its reason")
        if self.budget_method != "utf8_bytes_div4_v1":
            raise ValueError("unknown fetch budget method")
        _require(self.retrieved_at, str, "fetch retrieved_at")
        try:
            instant = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
            if instant.utcoffset() != timedelta(0):
                raise ValueError
        except ValueError:
            raise ValueError("fetch retrieved_at must be UTC") from None
        if not isinstance(self.attempts, tuple) or not all(isinstance(a, HostedAttempt) for a in self.attempts):
            raise ValueError("fetch attempts must be a tuple of HostedAttempt")
        for name in ("transport_degraded", "charset_fallback"):
            _require(getattr(self, name), bool, f"fetch {name}", optional=True)
        if self.transport_degraded_reason is not None and self.transport_degraded is not True:
            raise ValueError("a transport degradation reason needs transport_degraded")
        if len(frame_fetch_record(self).encode("utf-8", "surrogatepass")) > 48000:
            raise ValueError("fetch frame exceeds the 48000-byte budget")

    def check_tool(self, tool: str) -> None:
        if tool != "web_fetch":
            raise ValueError("WebFetchRecord belongs only to web_fetch")


@dataclass(frozen=True)
class HostedNative:
    """The provider's own items for one call, verbatim and in order; their origin is the message's."""

    source_id: str
    payload: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        _require(self.source_id, str, "native source_id")
        if not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise ValueError(f"invalid source id: {self.source_id!r}")
        if not isinstance(self.payload, tuple) or not self.payload:
            raise ValueError("a native payload is a non-empty tuple of provider items")
        for index, item in enumerate(self.payload):
            _require(item, dict, f"native payload[{index}]")
            require_json_value(item, f"native payload[{index}]")
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))


@dataclass(frozen=True)
class HostedToolBlock:
    id: str
    tool: HostedToolName
    action: HostedAction
    record: HostedRecord
    native: HostedNative

    def __post_init__(self) -> None:
        _require_text(self.id, "hosted call id")
        _require(self.tool, str, "hosted tool")
        if self.tool not in HOSTED_TOOLS:
            raise ValueError(f"unknown hosted tool: {self.tool}")
        _require(self.action, HostedAction, "hosted action")
        _require(self.record, HostedRecord, "hosted record")
        _require(self.native, HostedNative, "native payload")
        if not self.action.fits(self.tool):
            raise ValueError(f"a {self.action.type} action does not belong to {self.tool}")
        self.record.check_tool(self.tool)


@dataclass(frozen=True)
class HostedCost:
    """Money for one response, from the pricing module or the provider; ``usd`` is absent exactly when included."""

    basis: str
    usd: Optional[float] = None
    estimate: Optional[bool] = None

    def __post_init__(self) -> None:
        _require(self.basis, str, "cost basis")
        if self.basis not in COST_BASES:
            raise ValueError(f"unknown cost basis: {self.basis}")
        if self.usd is not None and (isinstance(self.usd, bool) or not isinstance(self.usd, (int, float))):
            raise ValueError("cost usd must be a number")
        if (self.usd is None) != (self.basis == "included"):
            raise ValueError("cost usd is present exactly when the basis is not included")
        if self.usd is not None:
            require_json_value(self.usd, "cost usd")
            if self.usd < 0:
                raise ValueError("cost usd must not be negative")
        _require(self.estimate, bool, "cost estimate", optional=True)
        if self.estimate and self.usd is None:
            raise ValueError("an estimated cost needs usd")


@dataclass(frozen=True)
class HostedSummary:
    """Response-scoped hosted facts, present exactly when the message has a ``HostedToolBlock``.

    ``cost`` is absent when no price is known: unknown is never priced as zero.
    """

    counts: Mapping[str, int] = field(default_factory=dict)
    cited_sources: tuple[HostedSource, ...] = ()
    cost: Optional[HostedCost] = None

    def __post_init__(self) -> None:
        _require(self.counts, dict, "hosted counts")
        for name, value in self.counts.items():
            if not isinstance(name, str) or not _COUNTER_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid hosted counter name: {name!r}")
            _require(value, int, f"hosted count {name}")
            if not 0 <= value <= MAX_HOSTED_COUNT:
                raise ValueError(f"hosted counts are between 0 and {MAX_HOSTED_COUNT}")
        object.__setattr__(self, "counts", dict(self.counts))
        if not isinstance(self.cited_sources, tuple) or not all(
            isinstance(s, HostedSource) for s in self.cited_sources
        ):
            raise ValueError("cited sources must be a tuple of HostedSource")
        _require(self.cost, HostedCost, "hosted cost", optional=True)


@dataclass(frozen=True)
class FailedAttempt:
    """An earlier billable attempt of a proxied search chain, before the one that ran the call (§6)."""

    source_id: str
    billing: str
    cost: Optional[HostedCost] = None

    def __post_init__(self) -> None:
        _require(self.source_id, str, "attempt source id")
        if not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise ValueError(f"invalid source id: {self.source_id!r}")
        _require(self.billing, str, "attempt billing")
        if self.billing not in SOURCE_BILLINGS:
            raise ValueError(f"unknown billing: {self.billing}")
        _require(self.cost, HostedCost, "attempt cost", optional=True)


@dataclass(frozen=True)
class ProxyExecution:
    """What ran a hosted call outside the model: a search executor (``proxy``) or an image endpoint (``direct``);
    absent when the call never reached one. ``cost`` is this attempt's own; ``failed_attempts`` lists the earlier
    billed ones, and ``core/hosted_activity`` totals them for display."""

    executor_origin: Union[Origin, ServiceExecutorOrigin]
    executor_source_id: str
    executor_billing: str
    cost: Optional[HostedCost] = None
    mode: Literal["proxy", "direct"] = "proxy"
    failed_attempts: tuple[FailedAttempt, ...] = ()

    def __post_init__(self) -> None:
        _require(self.mode, str, "execution mode")
        if self.mode not in EXECUTION_MODES:
            raise ValueError(f"unknown execution mode: {self.mode}")
        if not isinstance(self.failed_attempts, tuple) or not all(
            isinstance(attempt, FailedAttempt) for attempt in self.failed_attempts
        ):
            raise ValueError("failed attempts must be a tuple of FailedAttempt")
        if not isinstance(self.executor_origin, (Origin, ServiceExecutorOrigin)):
            raise ValueError("executor origin must be Origin or ServiceExecutorOrigin")
        _require(self.executor_source_id, str, "executor source id")
        if not _SOURCE_ID_RE.fullmatch(self.executor_source_id):
            raise ValueError(f"invalid source id: {self.executor_source_id!r}")
        _require(self.executor_billing, str, "executor billing")
        if self.executor_billing not in SOURCE_BILLINGS:
            raise ValueError(f"unknown billing: {self.executor_billing}")
        _require(self.cost, HostedCost, "executor cost", optional=True)


@dataclass(frozen=True)
class HostedFallback:
    """The Source that refused to run a search tool natively, so the call ran as its function instead (C-6 ``refused``).
    Named by id only: a surface shows its current name."""

    source_id: str

    def __post_init__(self) -> None:
        _require(self.source_id, str, "fallback source id")
        if not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise ValueError("a fallback names its Source by id")


@dataclass(frozen=True)
class HostedResult:
    """``ToolResultMessage.hosted``: the canonical record of a proxied search or an ``image_generate`` call.
    ``action`` is what the executor reports it did, where that differs from the model's request: for
    ``image_generate``, the model it ran on, the prompt the endpoint revised it to when it did, and the output's
    resolved size and quality when the endpoint reports them."""

    record: Union[HostedRecord, WebFetchRecord]
    execution: Optional[ProxyExecution] = None
    action: Optional[HostedAction] = None
    #: A search call that ran as its function only because the turn's own Source refused it natively; absent when it
    #: ran where it was declared, or where the vendor never offers the tool natively.
    fallback_from: Optional[HostedFallback] = None

    def __post_init__(self) -> None:
        if not isinstance(self.record, (HostedRecord, WebFetchRecord)):
            raise ValueError("hosted record must be HostedRecord or WebFetchRecord")
        _require(self.execution, ProxyExecution, "proxy execution", optional=True)
        _require(self.action, HostedAction, "hosted result action", optional=True)
        _require(self.fallback_from, HostedFallback, "hosted fallback", optional=True)
        if self.execution is None and self.record.status != "failed":
            raise ValueError("a hosted result without an execution is a failed record")


UserContent = Union[TextBlock, ImageBlock]
AssistantContent = Union[TextBlock, ThinkingBlock, ToolCallBlock, HostedToolBlock]


@dataclass(frozen=True)
class Origin:
    """Who served an assistant message; equal only when all three fields are equal."""

    provider: str
    api: str
    model: str

    def __post_init__(self) -> None:
        for name in ("provider", "api", "model"):
            _require(getattr(self, name), str, f"origin.{name}")
        if not self.provider or not self.model:
            raise ValueError("an origin needs a provider and a model")
        if self.api not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {self.api}")


@dataclass(frozen=True)
class ServiceExecutorOrigin:
    """A hosted service/local origin, distinct from the four assistant-model wire protocols."""

    provider: str
    api: Literal["search_api", "fetch_api", "local_fetch"]
    model: str

    def __post_init__(self) -> None:
        _require(self.provider, str, "service origin.provider")
        _require(self.api, str, "service origin.api")
        if self.api not in ("search_api", "fetch_api", "local_fetch"):
            raise ValueError("service origin api must be search_api, fetch_api or local_fetch")
        _require(self.model, str, "service origin.model")
        if not self.provider or not self.model:
            raise ValueError("a service origin needs a provider and a service id")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            _require(getattr(self, name), int, name)
        _require(self.reasoning_tokens, int, "reasoning_tokens", optional=True)
        counts = (self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)
        if any(c < 0 for c in counts) or (self.reasoning_tokens is not None and self.reasoning_tokens < 0):
            raise ValueError("token counts must not be negative")


@dataclass(frozen=True)
class UserMessage:
    content: tuple[UserContent, ...]

    def __post_init__(self) -> None:
        _require_blocks(self.content, (TextBlock, ImageBlock), "user message")
        _refuse_citations(self.content, "user message")
        if not self.content:
            raise ValueError("a user message needs content")

    @property
    def role(self) -> Literal["user"]:
        return "user"


@dataclass(frozen=True)
class AssistantMessage:
    content: tuple[AssistantContent, ...]
    origin: Origin
    stop_reason: StopReason
    usage: Optional[Usage] = None
    error_message: Optional[str] = None
    hosted_summary: Optional[HostedSummary] = None
    #: The identity of the Source that served the message (credential, base URL, protocol, vendor, kind,
    #: credential generation), when known: its opaque payloads go back only to a hop of that identity, since an account
    #: refuses another's ciphertext (``cross-provider.md``). Credential-free.
    source_identity: Optional[str] = None

    def __post_init__(self) -> None:
        _require_blocks(self.content, (TextBlock, ThinkingBlock, ToolCallBlock, HostedToolBlock), "assistant message")
        _require(self.origin, Origin, "origin")
        _require(self.stop_reason, str, "stop_reason")
        _require(self.usage, Usage, "usage", optional=True)
        _require(self.error_message, str, "error_message", optional=True)
        _require(self.hosted_summary, HostedSummary, "hosted_summary", optional=True)
        _require_text(self.source_identity, "source_identity", optional=True)
        hosted = any(isinstance(block, HostedToolBlock) for block in self.content)
        if hosted != (self.hosted_summary is not None):
            raise ValueError("a hosted summary is present exactly when the message has hosted tool blocks")
        if self.error_message is not None and not self.error_message:
            raise ValueError("an error message, when present, must not be empty")
        if self.stop_reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason: {self.stop_reason}")

    @property
    def role(self) -> Literal["assistant"]:
        return "assistant"

    @property
    def tool_calls(self) -> tuple[ToolCallBlock, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCallBlock))


@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: tuple[UserContent, ...]
    is_error: bool = False
    hosted: Optional[HostedResult] = None
    """Set only on the result of a proxied hosted tool call."""

    def __post_init__(self) -> None:
        _require(self.tool_call_id, str, "tool_call_id")
        _require(self.tool_name, str, "tool_name")
        _require_blocks(self.content, (TextBlock, ImageBlock), "tool result")
        _refuse_citations(self.content, "tool result")
        _require(self.is_error, bool, "is_error")
        _require(self.hosted, HostedResult, "hosted", optional=True)
        if not self.tool_call_id or not self.tool_name:
            raise ValueError("a tool result needs its call id and tool name")
        if self.hosted is not None:
            if self.tool_name not in HOSTED_RESULT_TOOLS:
                raise ValueError(f"{self.tool_name} is not a hosted tool")
            self.hosted.record.check_tool(self.tool_name)
            if isinstance(self.hosted.record, WebFetchRecord):
                from .web_fetch import frame_fetch_record
                if self.content != (TextBlock(text=frame_fetch_record(self.hosted.record)),):
                    raise ValueError("web_fetch ordinary content must equal the complete capped frame")
            if self.hosted.action is not None and not self.hosted.action.fits(self.tool_name):
                raise ValueError(f"a {self.hosted.action.type} action does not belong to {self.tool_name}")
            if self.hosted.fallback_from is not None and self.tool_name not in HOSTED_TOOLS:
                raise ValueError(f"{self.tool_name} never falls back: only a search tool runs as its function")
            if self.is_error != (self.hosted.record.status in ("failed", "incomplete")):
                raise ValueError("a hosted result is an error exactly when its record failed or is incomplete")

    @property
    def role(self) -> Literal["tool_result"]:
        return "tool_result"


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


def text(value: str) -> TextBlock:
    return TextBlock(text=value)


def has_reply(message: AssistantMessage, *, generated_images: bool = False) -> bool:
    """Whether a turn's final response gives the user something: non-blank text, or, when it ended normally, the images
    the turn's calls generated (``generated_images``), which are delivered with it. A refusal, safety or length stop
    without text stays a failure, whatever the turn generated.

    The one rule for "the model replied". The loop decides it once for each response it commits, and the store's row
    type and live settlement take that decision. A generated image is an ``image_generate`` tool result, never part of
    the response itself, so only the turn knows of it.
    """
    if any(isinstance(block, TextBlock) and block.text and block.text.strip() for block in message.content):
        return True
    return generated_images and message.stop_reason == "stop"


# --- persisted form ---------------------------------------------------------
#
# Two owners, so no field can be checked differently from another:
# * the dataclasses above own value invariants (non-empty ids, non-negative
#   counts, enumerations, the large-reference pattern);
# * ``_read`` owns the wire shape of every object: it must be a JSON object,
#   unknown keys are refused, required keys must be present, an optional key is
#   either absent or holds a value (an explicit null is refused, because the
#   writer never produces one), and every value has exactly its JSON type.


def _read(data: Any, what: str, required: Mapping[str, type], optional: Mapping[str, type] = {}) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an object")
    unknown = set(data) - set(required) - set(optional)
    if unknown:
        raise ValueError(f"unknown {what} field(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for name, kind in {**required, **optional}.items():
        if name not in data:
            if name in required:
                raise ValueError(f"{what} needs {name}")
            continue
        value = data[name]
        # bool is an int subclass in Python; a JSON boolean is never accepted as a number.
        if value is None or not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
            raise ValueError(f"{what}.{name} must be {kind.__name__}")
        out[name] = value
    return out


def _put(out: dict[str, Any], name: str, value: Any) -> None:
    if value is not None:
        out[name] = value


def _source_to_dict(source: HostedSource) -> dict[str, Any]:
    out: dict[str, Any] = {"url": source.url}
    for name in ("title", "snippet", "page_age"):
        _put(out, name, getattr(source, name))
    return out


def _source_from_dict(data: Any) -> HostedSource:
    return HostedSource(**_read(data, "hosted source", {"url": str}, {"title": str, "snippet": str, "page_age": str}))


def _record_to_dict(record: Union[HostedRecord, WebFetchRecord]) -> dict[str, Any]:
    if isinstance(record, WebFetchRecord):
        from .web_fetch import fetch_record_to_dict
        return fetch_record_to_dict(record)
    out: dict[str, Any] = {"status": record.status}
    _put(out, "error", record.error)
    _put(out, "error_code", record.error_code)
    out["sources"] = [_source_to_dict(source) for source in record.sources]
    if record.attempts is not None:
        out["attempts"] = [_attempt_to_dict(attempt) for attempt in record.attempts]
    if record.images is not None:
        images = []
        for image in record.images:
            item: dict[str, Any] = {"path": image.path, "mime": image.mime}
            _put(item, "width", image.width)
            _put(item, "height", image.height)
            _put(item, "bytes", image.bytes)
            images.append(item)
        out["images"] = images
    return out


def _record_from_dict(data: Any) -> HostedRecord:
    f = _read(
        data,
        "hosted record",
        {"status": str, "sources": list},
        {"error": str, "error_code": str, "images": list, "attempts": list},
    )
    images = None
    if "images" in f:
        images = tuple(
            HostedImage(
                **_read(item, "hosted image", {"path": str, "mime": str}, {"width": int, "height": int, "bytes": int})
            )
            for item in f["images"]
        )
    return HostedRecord(
        status=f["status"],
        error=f.get("error"),
        sources=tuple(_source_from_dict(source) for source in f["sources"]),
        images=images,
        error_code=f.get("error_code"),
        attempts=tuple(_attempt_from_dict(attempt) for attempt in f["attempts"]) if "attempts" in f else None,
    )


def _action_to_dict(action: HostedAction) -> dict[str, Any]:
    out: dict[str, Any] = {"type": action.type}
    for name in _HOSTED_ACTION_FIELDS:
        value = getattr(action, name)
        _put(out, name, list(value) if isinstance(value, tuple) else value)
    return out


def _action_from_dict(data: Any) -> HostedAction:
    kind = data.get("type") if isinstance(data, Mapping) else None
    if kind not in _HOSTED_ACTIONS:
        raise ValueError(f"unknown hosted action type: {kind!r}")
    _, required, optional = _HOSTED_ACTIONS[kind]
    f = _read(data, f"{kind} action", {"type": str, **required}, optional)
    if "queries" in f:
        f["queries"] = tuple(f["queries"])
    return HostedAction(**f)


def _attempt_to_dict(attempt: HostedAttempt) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_id": attempt.source_id,
        "label": attempt.label,
        "kind": attempt.kind,
        "outcome": attempt.outcome,
    }
    for name in ("error_code", "skipped", "retry_at", "needs_action"):
        _put(out, name, getattr(attempt, name))
    if attempt.cost is not None:
        out["cost"] = _cost_to_dict(attempt.cost)
    _put(out, "superseded", attempt.superseded)
    return out


def _attempt_from_dict(data: Any) -> HostedAttempt:
    f = _read(
        data,
        "hosted attempt",
        {"source_id": str, "label": str, "kind": str, "outcome": str},
        {
            "error_code": str,
            "skipped": str,
            "retry_at": str,
            "needs_action": str,
            "cost": dict,
            "superseded": bool,
        },
    )
    return HostedAttempt(
        source_id=f["source_id"],
        label=f["label"],
        kind=f["kind"],
        outcome=f["outcome"],
        error_code=f.get("error_code"),
        skipped=f.get("skipped"),
        retry_at=f.get("retry_at"),
        needs_action=f.get("needs_action"),
        cost=_cost_from_dict(f["cost"]) if "cost" in f else None,
        superseded=f.get("superseded"),
    )


def _cost_to_dict(cost: HostedCost) -> dict[str, Any]:
    out: dict[str, Any] = {"basis": cost.basis}
    _put(out, "usd", cost.usd)
    _put(out, "estimate", cost.estimate)
    return out


def _cost_from_dict(data: Any) -> HostedCost:
    if isinstance(data, Mapping) and isinstance(data.get("usd"), int) and not isinstance(data.get("usd"), bool):
        data = {**data, "usd": float(data["usd"])}
    return HostedCost(**_read(data, "hosted cost", {"basis": str}, {"usd": float, "estimate": bool}))


def _citation_to_dict(citation: Citation) -> dict[str, Any]:
    out: dict[str, Any] = {"url": citation.url}
    for name in ("title", "cited_text", "start", "end"):
        _put(out, name, getattr(citation, name))
    return out


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        if block.ref is not None:
            return {"type": "text", "ref": {"ref": block.ref.ref, "bytes": block.ref.bytes}}
        out: dict[str, Any] = {"type": "text", "text": block.text}
        if block.citations is not None:
            out["citations"] = [_citation_to_dict(citation) for citation in block.citations]
        return out
    if isinstance(block, ImageBlock):
        out: dict[str, Any] = {"type": "image", "mime_type": block.mime_type, "media_token": block.media_token}
        _put(out, "name", block.name)
        return out
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "text": block.text}
        _put(out, "signature", block.signature)
        if block.redacted:
            out["redacted"] = True
        return out
    if isinstance(block, ToolCallBlock):
        # Arguments are a mutable dict on a frozen block; check again at the write boundary.
        require_json_value(block.arguments, "arguments")
        out = {"type": "tool_call", "id": block.id, "name": block.name, "arguments": copy.deepcopy(block.arguments)}
        _put(out, "native_id", block.native_id)
        _put(out, "signature", block.signature)
        return out
    if isinstance(block, HostedToolBlock):
        return {
            "type": "hosted_tool",
            "id": block.id,
            "tool": block.tool,
            "action": _action_to_dict(block.action),
            "record": _record_to_dict(block.record),
            "native": {"source_id": block.native.source_id, "payload": copy.deepcopy(list(block.native.payload))},
        }
    raise TypeError(f"not a content block: {type(block).__name__}")


def _unknown_hosted_text(data: Mapping[str, Any]) -> str:
    """Render an unknown native hosted block without validating its future shape."""

    tool = data.get("tool")
    tool_name = tool if isinstance(tool, str) and tool else "hosted tool"
    action = data.get("action") if isinstance(data.get("action"), Mapping) else {}
    record = data.get("record") if isinstance(data.get("record"), Mapping) else {}
    queries: list[str] = []
    raw_queries = action.get("queries")
    if isinstance(raw_queries, list):
        queries.extend(query for query in raw_queries if isinstance(query, str) and query)
    for name in ("query", "url"):
        value = action.get(name)
        if isinstance(value, str) and value and value not in queries:
            queries.append(value)
    lines: list[str] = []
    status = record.get("status")
    if isinstance(status, str) and status and status != "completed":
        suffix = ": no result" if status == "pending" else ""
        lines.append(f"[{tool_name} {status}{suffix}]")
    error = record.get("error")
    if isinstance(error, str) and error:
        lines.append(f"Error: {error}")
    if queries:
        lines.append(f"[{tool_name}: {'; '.join(queries)}]")
    raw_sources = record.get("sources")
    if isinstance(raw_sources, list):
        for number, source in enumerate(raw_sources, 1):
            if not isinstance(source, Mapping):
                continue
            url = source.get("url") if isinstance(source.get("url"), str) else None
            title = source.get("title") if isinstance(source.get("title"), str) else None
            snippet = source.get("snippet") if isinstance(source.get("snippet"), str) else None
            page_age = source.get("page_age") if isinstance(source.get("page_age"), str) else None
            if not any((url, title, snippet, page_age)):
                continue
            lines.append(f"{number}. {title or url or snippet or page_age}")
            if title and url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet}")
            if page_age:
                lines.append(f"   Published: {page_age}")
    return "\n".join(lines) if lines else f"[{tool_name} result unavailable in this version]"


def _block_from_dict(
    data: Any, allowed_types: tuple[str, ...], *, unknown_hosted_tools: list[str] | None = None
) -> Any:
    kind = data.get("type") if isinstance(data, Mapping) else None
    if kind not in allowed_types:
        raise ValueError(f"block type {kind!r} is not allowed here")
    if kind == "text":
        f = _read(data, "text block", {"type": str}, {"text": str, "ref": dict, "citations": list})
        ref = None
        if "ref" in f:
            r = _read(f["ref"], "large-content reference", {"ref": str, "bytes": int})
            ref = LargeRef(ref=r["ref"], bytes=r["bytes"])
        citations = None
        if "citations" in f:
            citations = tuple(
                Citation(
                    **_read(item, "citation", {"url": str}, {"title": str, "cited_text": str, "start": int, "end": int})
                )
                for item in f["citations"]
            )
        return TextBlock(text=f.get("text"), ref=ref, citations=citations)
    if kind == "hosted_tool":
        raw_tool = data.get("tool") if isinstance(data, Mapping) else None
        if isinstance(raw_tool, str) and raw_tool not in HOSTED_TOOLS:
            if unknown_hosted_tools is not None:
                unknown_hosted_tools.append(raw_tool)
            return TextBlock(text=_unknown_hosted_text(data))
        f = _read(
            data,
            "hosted tool block",
            {"type": str, "id": str, "tool": str, "action": dict, "record": dict, "native": dict},
        )
        native = _read(f["native"], "native payload", {"source_id": str, "payload": list})
        return HostedToolBlock(
            id=f["id"],
            tool=f["tool"],
            action=_action_from_dict(f["action"]),
            record=_record_from_dict(f["record"]),
            native=HostedNative(source_id=native["source_id"], payload=tuple(native["payload"])),
        )
    if kind == "image":
        f = _read(data, "image block", {"type": str, "mime_type": str, "media_token": str}, {"name": str})
        return ImageBlock(mime_type=f["mime_type"], media_token=f["media_token"], name=f.get("name"))
    if kind == "thinking":
        f = _read(data, "thinking block", {"type": str, "text": str}, {"signature": str, "redacted": bool})
        if f.get("redacted") is False:
            raise ValueError("thinking block.redacted is written only when true")
        return ThinkingBlock(text=f["text"], signature=f.get("signature"), redacted=bool(f.get("redacted")))
    f = _read(
        data,
        "tool call block",
        {"type": str, "id": str, "name": str, "arguments": dict},
        {"native_id": str, "signature": str},
    )
    return ToolCallBlock(
        id=f["id"], name=f["name"], arguments=dict(f["arguments"]), native_id=f.get("native_id"), signature=f.get("signature")
    )


_USAGE_REQUIRED = {"input_tokens": int, "output_tokens": int, "cache_read_tokens": int, "cache_write_tokens": int}


def usage_to_dict(usage: Usage) -> dict[str, Any]:
    out: dict[str, Any] = {name: getattr(usage, name) for name in _USAGE_REQUIRED}
    _put(out, "reasoning_tokens", usage.reasoning_tokens)
    return out


def usage_from_dict(data: Any) -> Usage:
    return Usage(**_read(data, "usage", _USAGE_REQUIRED, {"reasoning_tokens": int}))


def origin_from_dict(data: Any) -> Origin:
    return Origin(**_read(data, "origin", {"provider": str, "api": str, "model": str}))


def executor_origin_to_dict(origin: Union[Origin, ServiceExecutorOrigin]) -> dict[str, str]:
    return {"provider": origin.provider, "api": origin.api, "model": origin.model}


def executor_origin_from_dict(data: Any) -> Union[Origin, ServiceExecutorOrigin]:
    fields = _read(data, "executor origin", {"provider": str, "api": str, "model": str})
    if fields["api"] in ("search_api", "fetch_api", "local_fetch"):
        return ServiceExecutorOrigin(**fields)
    return Origin(**fields)


def message_to_dict(message: Message) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": [_block_to_dict(b) for b in message.content]}
    if isinstance(message, AssistantMessage):
        out: dict[str, Any] = {
            "role": "assistant",
            "content": [_block_to_dict(b) for b in message.content],
            "origin": {"provider": message.origin.provider, "api": message.origin.api, "model": message.origin.model},
            "stop_reason": message.stop_reason,
        }
        if message.usage is not None:
            out["usage"] = usage_to_dict(message.usage)
        _put(out, "error_message", message.error_message)
        if message.hosted_summary is not None:
            summary = message.hosted_summary
            hosted: dict[str, Any] = {
                "counts": dict(summary.counts),
                "cited_sources": [_source_to_dict(source) for source in summary.cited_sources],
            }
            if summary.cost is not None:
                hosted["cost"] = _cost_to_dict(summary.cost)
            out["hosted_summary"] = hosted
        _put(out, "source_identity", message.source_identity)
        return out
    if isinstance(message, ToolResultMessage):
        result: dict[str, Any] = {
            "role": "tool_result",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": [_block_to_dict(b) for b in message.content],
            "is_error": message.is_error,
        }
        if message.hosted is not None:
            hosted_result: dict[str, Any] = {"record": _record_to_dict(message.hosted.record)}
            execution = message.hosted.execution
            if execution is not None:
                hosted_result["execution"] = {
                    "mode": execution.mode,
                    "executor_origin": executor_origin_to_dict(execution.executor_origin),
                    "executor_source_id": execution.executor_source_id,
                    "executor_billing": execution.executor_billing,
                }
                if execution.cost is not None:
                    hosted_result["execution"]["cost"] = _cost_to_dict(execution.cost)
                if execution.failed_attempts:
                    hosted_result["execution"]["failed_attempts"] = [
                        {
                            "source_id": attempt.source_id,
                            "billing": attempt.billing,
                            **({"cost": _cost_to_dict(attempt.cost)} if attempt.cost is not None else {}),
                        }
                        for attempt in execution.failed_attempts
                    ]
            if message.hosted.action is not None:
                hosted_result["action"] = _action_to_dict(message.hosted.action)
            if message.hosted.fallback_from is not None:
                hosted_result["fallback_from"] = {"source_id": message.hosted.fallback_from.source_id}
            result["hosted"] = hosted_result
        return result
    raise TypeError(f"not a message: {type(message).__name__}")


def message_from_dict(data: Mapping[str, Any]) -> Message:
    role = data.get("role") if isinstance(data, Mapping) else None
    if role == "user":
        f = _read(data, "user message", {"role": str, "content": list})
        return UserMessage(content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]))
    if role == "assistant":
        f = _read(
            data,
            "assistant message",
            {"role": str, "content": list, "origin": dict, "stop_reason": str},
            {"usage": dict, "error_message": str, "hosted_summary": dict, "source_identity": str},
        )
        # Decode calls before their aggregate: unknown calls make response-wide facts unusable.
        unknown_hosted_tools: list[str] = []
        content = tuple(
            _block_from_dict(
                b, ("text", "thinking", "tool_call", "hosted_tool"), unknown_hosted_tools=unknown_hosted_tools
            )
            for b in f["content"]
        )
        summary = None
        if unknown_hosted_tools:
            logger.warning("stored message contains an unrecognized hosted tool; loading it portably")
            # Do not interpret a discarded aggregate's future shape. Keep an empty envelope for known blocks,
            # but do not invent a missing summary; retained blocks and the message envelope remain strict.
            if "hosted_summary" in f and any(isinstance(block, HostedToolBlock) for block in content):
                summary = HostedSummary()
        elif "hosted_summary" in f:
            h = _read(f["hosted_summary"], "hosted summary", {"counts": dict, "cited_sources": list}, {"cost": dict})
            summary = HostedSummary(
                # A release before the bound may have stored a count past it: that counter is dropped, not refused,
                # so its transcript still loads.
                counts={
                    name: value
                    for name, value in h["counts"].items()
                    if not (isinstance(value, int) and value > MAX_HOSTED_COUNT)
                },
                cited_sources=tuple(_source_from_dict(source) for source in h["cited_sources"]),
                cost=_cost_from_dict(h["cost"]) if "cost" in h else None,
            )
        return AssistantMessage(
            content=content,
            origin=origin_from_dict(f["origin"]),
            stop_reason=f["stop_reason"],
            usage=usage_from_dict(f["usage"]) if "usage" in f else None,
            error_message=f.get("error_message"),
            hosted_summary=summary,
            source_identity=f.get("source_identity"),
        )
    if role == "tool_result":
        f = _read(
            data,
            "tool result message",
            {"role": str, "tool_call_id": str, "tool_name": str, "content": list, "is_error": bool},
            {"hosted": dict},
        )
        hosted_result = None
        unknown_hosted_tool = "hosted" in f and f["tool_name"] not in HOSTED_RESULT_TOOLS
        if "hosted" in f and not unknown_hosted_tool:
            h = _read(
                f["hosted"], "hosted result", {"record": dict}, {"execution": dict, "action": dict, "fallback_from": dict}
            )
            execution = None
            if "execution" in h:
                e = _read(
                    h["execution"],
                    "proxy execution",
                    {
                        "mode": str,
                        "executor_origin": dict,
                        "executor_source_id": str,
                        "executor_billing": str,
                    },
                    {"cost": dict, "failed_attempts": list},
                )
                attempts = []
                for attempt in e.get("failed_attempts", ()):
                    a = _read(attempt, "failed attempt", {"source_id": str, "billing": str}, {"cost": dict})
                    attempts.append(
                        FailedAttempt(
                            source_id=a["source_id"],
                            billing=a["billing"],
                            cost=_cost_from_dict(a["cost"]) if "cost" in a else None,
                        )
                    )
                execution = ProxyExecution(
                    executor_origin=executor_origin_from_dict(e["executor_origin"]),
                    executor_source_id=e["executor_source_id"],
                    executor_billing=e["executor_billing"],
                    cost=_cost_from_dict(e["cost"]) if "cost" in e else None,
                    mode=e["mode"],
                    failed_attempts=tuple(attempts),
                )
            fallback = None
            if "fallback_from" in h:
                fallback = HostedFallback(_read(h["fallback_from"], "hosted fallback", {"source_id": str})["source_id"])
            if f["tool_name"] == "web_fetch":
                from .web_fetch import fetch_record_from_dict
                record = fetch_record_from_dict(h["record"])
            else:
                record = _record_from_dict(h["record"])
            hosted_result = HostedResult(
                record=record,
                execution=execution,
                action=_action_from_dict(h["action"]) if "action" in h else None,
                fallback_from=fallback,
            )
        if unknown_hosted_tool:
            logger.warning("stored message contains an unrecognized hosted tool; loading it portably")
        return ToolResultMessage(
            tool_call_id=f["tool_call_id"],
            tool_name=f["tool_name"],
            content=tuple(_block_from_dict(b, ("text", "image")) for b in f["content"]),
            is_error=f["is_error"],
            hosted=hosted_result,
        )
    raise ValueError(f"unknown message role: {role!r}")
