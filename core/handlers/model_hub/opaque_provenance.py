"""Process-local provenance for provider-owned opaque history values.

The registry intentionally knows nothing about the values it indexes.  It keeps
only one-way fingerprints and the Source identities that produced them.  This
module is separate from turn provenance because opaque values are never allowed
to become persisted turn or transcript data.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import ijson

from .request import ModelHubRequest, without_opaque_history


DEFAULT_MAX_ENTRIES = 4096
DEFAULT_TTL_SECONDS = 60 * 60
_MAX_STREAM_EVENT_BYTES = 1024 * 1024
_MAX_OBSERVED_OPAQUE_BYTES = 512 * 1024
_MAX_IDENTITIES_PER_FINGERPRINT = 2
_TYPE_FIELD = "type"


def _event_type(value: Any) -> Any:
    """Read event/item type without treating it as an error-envelope extractor."""

    return value.get(_TYPE_FIELD) if isinstance(value, Mapping) else None


@dataclass
class _FingerprintRecord:
    identities: set[str] = field(default_factory=set)
    touched_at: float = 0.0


class OpaqueProvenanceRegistry:
    """Bounded, process-local provenance for protocol-owned opaque strings."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._records: OrderedDict[str, _FingerprintRecord] = OrderedDict()

    @staticmethod
    def fingerprint(protocol: str, value: str) -> str:
        """Return a domain-separated one-way fingerprint without retaining value."""

        raw = f"avibe-model-hub-opaque-v1\0{protocol}\0".encode("utf-8") + value.encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def _expire_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        while self._records:
            key, record = next(iter(self._records.items()))
            if record.touched_at >= cutoff:
                break
            self._records.pop(key, None)

    def observe(self, protocol: str, value: str, identity: str | None) -> None:
        """Stage one complete opaque value as produced by ``identity``.

        Empty or absent identities are deliberately not recorded: an observation
        without a verified serving Source cannot make later replay safe.
        """

        if not isinstance(value, str) or not value or not isinstance(identity, str) or not identity:
            return
        fingerprint = self.fingerprint(protocol, value)
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            record = self._records.pop(fingerprint, None)
            if record is None:
                record = _FingerprintRecord()
            if len(record.identities) < _MAX_IDENTITIES_PER_FINGERPRINT:
                record.identities.add(identity)
            record.touched_at = now
            self._records[fingerprint] = record
            while len(self._records) > self.max_entries:
                self._records.popitem(last=False)

    def accepts(self, protocol: str, value: Any, identity: str | None) -> bool:
        """Whether one opaque value has one unambiguous, current identity."""

        if not isinstance(value, str) or not value or not isinstance(identity, str) or not identity:
            return False
        fingerprint = self.fingerprint(protocol, value)
        now = time.monotonic()
        with self._lock:
            self._expire_locked(now)
            record = self._records.get(fingerprint)
            if record is None or len(record.identities) != 1 or identity not in record.identities:
                return False
            record.touched_at = now
            self._records.move_to_end(fingerprint)
            return True

    def snapshot_metadata(self) -> dict[str, Any]:
        """Testing/diagnostic metadata; no opaque value is returned."""

        with self._lock:
            self._expire_locked(time.monotonic())
            return {
                "size": len(self._records),
                "max_entries": self.max_entries,
                "fingerprints": tuple(self._records),
                "identity_counts": tuple(sorted(len(item.identities) for item in self._records.values())),
            }

    def begin_response(self, protocol: str, identity: str | None) -> "OpaqueResponseObserver":
        return OpaqueResponseObserver(self, protocol, identity)


@dataclass
class _StagedOpaque:
    protocol: str
    value: str


class OpaqueResponseObserver:
    """Observe response wire data without retaining the response body."""

    def __init__(self, registry: OpaqueProvenanceRegistry, protocol: str, identity: str | None) -> None:
        self.registry = registry
        self.protocol = protocol
        self.identity = identity
        self._staged: dict[tuple[str, str], _StagedOpaque] = {}
        self._fragments: dict[tuple[str, str], list[str]] = {}
        self._retained_bytes = 0
        self._sse_buffer = bytearray()
        self._available = True
        self._terminal_seen = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    def _stage(self, value: Any, *, slot: str = "value", fragment: bool = False) -> None:
        if not isinstance(value, str) or not value:
            return
        value_bytes = len(value.encode("utf-8"))
        if self._retained_bytes + value_bytes > _MAX_OBSERVED_OPAQUE_BYTES:
            self._available = False
            self._staged.clear()
            self._fragments.clear()
            return
        if fragment:
            self._fragments.setdefault((slot, self.protocol), []).append(value)
        else:
            key = (slot, self.protocol)
            if key in self._staged:
                suffix = 1
                while (f"{slot}:{suffix}", self.protocol) in self._staged:
                    suffix += 1
                key = (f"{slot}:{suffix}", self.protocol)
            self._staged[key] = _StagedOpaque(self.protocol, value)
        self._retained_bytes += value_bytes

    def _stage_payload(self, payload: Any) -> None:
        for slot, value, is_fragment in _response_opaque_values(self.protocol, payload):
            self._stage(value, slot=slot, fragment=is_fragment)
        if _response_is_terminal(self.protocol, payload):
            self._terminal_seen = True

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        """Consume raw SSE bytes; only bounded event fragments are retained."""

        if not self._available or not isinstance(chunk, (bytes, bytearray, memoryview)):
            self._available = False
            return
        self._sse_buffer.extend(bytes(chunk))
        if len(self._sse_buffer) > _MAX_STREAM_EVENT_BYTES:
            self._available = False
            self._sse_buffer.clear()
            return
        while True:
            newline_marker = self._sse_buffer.find(b"\n\n")
            crlf_marker = self._sse_buffer.find(b"\r\n\r\n")
            if newline_marker < 0 and crlf_marker < 0:
                return
            if crlf_marker >= 0 and (newline_marker < 0 or crlf_marker < newline_marker):
                marker, separator_size = crlf_marker, 4
            else:
                marker, separator_size = newline_marker, 2
            frame = bytes(self._sse_buffer[:marker])
            del self._sse_buffer[: marker + separator_size]
            data_lines = [
                line[5:].lstrip()
                for line in frame.splitlines()
                if line.startswith(b"data:")
            ]
            if not data_lines:
                continue
            data = b"\n".join(data_lines).strip()
            if data == b"[DONE]":
                self._terminal_seen = True
                continue
            try:
                payload = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._available = False
                continue
            self._stage_payload(payload)

    def observe_buffered(self, payload: BinaryIO | bytes | bytearray | memoryview) -> None:
        """Parse a complete JSON response incrementally with ijson."""

        if not self._available:
            return
        try:
            if isinstance(payload, (bytes, bytearray, memoryview)):
                source: BinaryIO = io.BytesIO(bytes(payload))
            else:
                source = payload
            for prefix, event, value in ijson.parse(source):
                if event != "string":
                    continue
                _buffered_opaque_value(self.protocol, prefix, value, self)
            self._terminal_seen = True
        except (OSError, ValueError, TypeError, ijson.common.IncompleteJSONError, ijson.common.JSONError):
            self._available = False

    def finish(self, *, complete: bool) -> None:
        """Commit only a complete, parseable response."""

        if not complete or not self._available or (self.protocol in _SSE_PROTOCOLS and not self._terminal_seen):
            return
        for (slot, protocol), value in self._staged.items():
            self.registry.observe(protocol, value.value, self.identity)
        for (slot, protocol), fragments in self._fragments.items():
            if fragments:
                self.registry.observe(protocol, "".join(fragments), self.identity)


_SSE_PROTOCOLS = {"anthropic", "openai_chat", "openai_responses"}


def _response_is_terminal(protocol: str, payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    event_type = _event_type(payload)
    if protocol == "anthropic":
        return event_type == "message_stop"
    if protocol == "openai_responses":
        return event_type in {"response.completed", "response.failed", "response.incomplete"}
    if protocol == "openai_chat":
        return False
    return False


def _response_opaque_values(protocol: str, payload: Any) -> list[tuple[str, str, bool]]:
    """Extract only provider-owned opaque fields from one response event."""

    values: list[tuple[str, str, bool]] = []

    def responses_item(item: Any, slot: str) -> None:
        if not isinstance(item, Mapping):
            return
        if _event_type(item) == "reasoning" and isinstance(item.get("encrypted_content"), str):
            values.append((slot, item["encrypted_content"], False))

    if protocol == "openai_responses":
        if isinstance(payload, Mapping):
            event_type = _event_type(payload)
            if event_type == "response.output_item.done":
                responses_item(payload.get("item"), "responses:output_item")
            elif event_type == "response.completed":
                response = payload.get("response")
                if isinstance(response, Mapping):
                    for index, item in enumerate(response.get("output", ())):
                        responses_item(item, f"responses:output:{index}")
            elif _event_type(payload) == "reasoning":
                responses_item(payload, "responses:item")
            else:
                output = payload.get("output")
                if isinstance(output, list):
                    for index, item in enumerate(output):
                        responses_item(item, f"responses:output:{index}")
    elif protocol == "anthropic":
        if isinstance(payload, Mapping):
            event_type = _event_type(payload)
            if event_type == "content_block_start":
                block = payload.get("content_block")
                if isinstance(block, Mapping):
                    if _event_type(block) == "thinking" and isinstance(block.get("signature"), str):
                        values.append((f"anthropic:block:{payload.get('index', 0)}", block["signature"], False))
                    elif _event_type(block) == "redacted_thinking" and isinstance(block.get("data"), str):
                        values.append((f"anthropic:block:{payload.get('index', 0)}", block["data"], False))
            elif event_type == "content_block_delta":
                delta = payload.get("delta")
                if isinstance(delta, Mapping) and _event_type(delta) == "signature_delta":
                    values.append((f"anthropic:block:{payload.get('index', 0)}", delta.get("signature", ""), True))
            elif event_type == "message_start":
                message = payload.get("message")
                if isinstance(message, Mapping):
                    for index, block in enumerate(message.get("content", ())):
                        if isinstance(block, Mapping):
                            if _event_type(block) == "thinking" and isinstance(block.get("signature"), str):
                                values.append((f"anthropic:block:{index}", block["signature"], False))
                            elif _event_type(block) == "redacted_thinking" and isinstance(block.get("data"), str):
                                values.append((f"anthropic:block:{index}", block["data"], False))
            else:
                for index, block in enumerate(payload.get("content", ()) if isinstance(payload.get("content"), list) else ()):
                    if isinstance(block, Mapping):
                        if _event_type(block) == "thinking" and isinstance(block.get("signature"), str):
                            values.append((f"anthropic:block:{index}", block["signature"], False))
                        elif _event_type(block) == "redacted_thinking" and isinstance(block.get("data"), str):
                            values.append((f"anthropic:block:{index}", block["data"], False))
    elif protocol == "openai_chat":
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if isinstance(choices, list):
            for choice_index, choice in enumerate(choices):
                if not isinstance(choice, Mapping):
                    continue
                for container_name in ("message", "delta"):
                    container = choice.get(container_name)
                    if not isinstance(container, Mapping):
                        continue
                    details = container.get("reasoning_details")
                    if not isinstance(details, list):
                        continue
                    for detail_index, detail in enumerate(details):
                        if not isinstance(detail, Mapping):
                            continue
                        for key in ("signature", "data", "encrypted_content"):
                            if isinstance(detail.get(key), str):
                                values.append((f"chat:{choice_index}:{container_name}:{detail_index}:{key}", detail[key], False))
    elif protocol == "google":
        candidates = payload.get("candidates") if isinstance(payload, Mapping) else None
        if isinstance(candidates, list):
            for candidate_index, candidate in enumerate(candidates):
                content = candidate.get("content") if isinstance(candidate, Mapping) else None
                parts = content.get("parts") if isinstance(content, Mapping) else None
                if isinstance(parts, list):
                    for part_index, part in enumerate(parts):
                        if isinstance(part, Mapping) and part.get("thought") is True and isinstance(part.get("thoughtSignature"), str):
                            values.append((f"google:{candidate_index}:{part_index}", part["thoughtSignature"], False))
    return values


def _buffered_opaque_value(protocol: str, prefix: str, value: str, observer: OpaqueResponseObserver) -> None:
    if protocol == "openai_responses":
        if prefix.endswith(".item.type"):
            return
        if prefix.endswith(".item.encrypted_content"):
            # The response-owned output item is the only Responses location
            # whose encrypted content is protocol opaque history.
            observer._stage(value, slot=prefix, fragment=False)
    elif protocol == "anthropic":
        if prefix.endswith("content.item.signature") or prefix.endswith("content.item.data"):
            observer._stage(value, slot=prefix, fragment=False)
    elif protocol == "openai_chat":
        if ".reasoning_details.item." in prefix and prefix.rsplit(".", 1)[-1] in {"signature", "data", "encrypted_content"}:
            observer._stage(value, slot=prefix, fragment=False)
    elif protocol == "google":
        if prefix.endswith("parts.item.thoughtSignature"):
            observer._stage(value, slot=prefix, fragment=False)


def _accepted(registry: OpaqueProvenanceRegistry, protocol: str, value: Any, identity: str | None) -> bool:
    return registry.accepts(protocol, value, identity)


def filter_opaque_history(
    payload: Mapping[str, Any],
    *,
    protocol: str,
    identity: str | None,
    registry: OpaqueProvenanceRegistry,
) -> dict[str, Any]:
    """Copy a request and remove only unverified protocol-owned opaque fields."""

    result = copy.deepcopy(dict(payload))
    if protocol == "anthropic":
        if isinstance(result.get("messages"), list):
            result["messages"] = _filter_anthropic_messages(result["messages"], registry, identity)
    elif protocol == "openai_responses":
        if isinstance(result.get("input"), list):
            result["input"] = _filter_responses_input(result["input"], registry, identity)
    elif protocol == "openai_chat":
        if isinstance(result.get("messages"), list):
            result["messages"] = _filter_chat_messages(result["messages"], registry, identity)
    elif protocol == "google":
        if isinstance(result.get("contents"), list):
            result["contents"] = _filter_google_contents(result["contents"], registry, identity)
    if isinstance(payload, ModelHubRequest):
        return ModelHubRequest(
            result,
            protocol=payload.protocol,
            headers=payload.headers,
            primary_origin=payload.primary_origin,
            pinned_hop=payload.pinned_hop,
            pin_identity_claim=payload.pin_identity_claim,
        )
    return result


def _filter_anthropic_messages(messages: list[Any], registry: OpaqueProvenanceRegistry, identity: str | None) -> list[Any]:
    output: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping):
            output.append(copy.deepcopy(message))
            continue
        item = dict(message)
        content = item.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if not isinstance(block, Mapping):
                    blocks.append(copy.deepcopy(block))
                    continue
                kind = _event_type(block)
                if kind == "redacted_thinking":
                    if _accepted(registry, "anthropic", block.get("data"), identity):
                        blocks.append(copy.deepcopy(dict(block)))
                    continue
                if kind == "thinking" and isinstance(block.get("signature"), str):
                    if _accepted(registry, "anthropic", block["signature"], identity):
                        blocks.append(copy.deepcopy(dict(block)))
                    elif isinstance(block.get("thinking"), str) and block["thinking"]:
                        blocks.append({"type": "text", "text": block["thinking"]})
                    continue
                blocks.append(copy.deepcopy(dict(block)))
            item["content"] = blocks
            if item.get("role") == "assistant" and not blocks and not item.get("tool_calls"):
                continue
        output.append(item)
    return output


def _portable_responses_reasoning(item: Mapping[str, Any]) -> list[Any]:
    portable = without_opaque_history({"input": [dict(item)]})
    values = portable.get("input") if isinstance(portable, Mapping) else None
    return values if isinstance(values, list) else []


def _filter_responses_input(items: list[Any], registry: OpaqueProvenanceRegistry, identity: str | None) -> list[Any]:
    output: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping) or _event_type(item) != "reasoning":
            output.append(copy.deepcopy(item))
            continue
        encrypted = item.get("encrypted_content")
        if _accepted(registry, "openai_responses", encrypted, identity):
            output.append(copy.deepcopy(item))
            continue
        portable = _portable_responses_reasoning(item)
        output.extend(copy.deepcopy(portable))
    return output


def _filter_chat_messages(messages: list[Any], registry: OpaqueProvenanceRegistry, identity: str | None) -> list[Any]:
    output: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping):
            output.append(copy.deepcopy(message))
            continue
        item = copy.deepcopy(dict(message))
        details = item.get("reasoning_details")
        if isinstance(details, list):
            kept_details = []
            for detail in details:
                if not isinstance(detail, Mapping):
                    kept_details.append(detail)
                    continue
                kept = dict(detail)
                for key in ("signature", "data", "encrypted_content"):
                    if key in kept and not _accepted(registry, "openai_chat", kept[key], identity):
                        kept.pop(key, None)
                if kept:
                    kept_details.append(kept)
            item["reasoning_details"] = kept_details
        output.append(item)
    return output


def _filter_google_contents(contents: list[Any], registry: OpaqueProvenanceRegistry, identity: str | None) -> list[Any]:
    output: list[Any] = []
    for content in contents:
        if not isinstance(content, Mapping):
            output.append(copy.deepcopy(content))
            continue
        item = copy.deepcopy(dict(content))
        parts = item.get("parts")
        if isinstance(parts, list):
            kept = []
            for part in parts:
                if isinstance(part, Mapping) and part.get("thought") is True and "thoughtSignature" in part:
                    if not _accepted(registry, "google", part.get("thoughtSignature"), identity):
                        part = dict(part)
                        part.pop("thoughtSignature", None)
                        part.pop("thought", None)
                kept.append(part)
            item["parts"] = kept
        output.append(item)
    return output


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "OpaqueProvenanceRegistry",
    "OpaqueResponseObserver",
    "filter_opaque_history",
]
