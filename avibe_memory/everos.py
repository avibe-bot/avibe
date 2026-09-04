"""Private provider port, real EverOS adapter, and test fake for Memory."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from avibe_memory.types import (
    CaptureAttachment,
    MAX_MEMORY_LIST_PAGE_SIZE,
    MemoryErrorCode,
    MemoryItem,
    MemoryListItem,
    MemoryListPage,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    ProviderSearchItem,
    ProviderSessionRef,
    is_memory_error_code,
    MemoryPreflightDiagnostic,
    MAX_AGENTIC_TIMEOUT_SECONDS,
)
from avibe_memory.observations import (
    AddAck,
    AddRejected,
    AddResult,
    FlushRejected,
    FlushRetryable,
    FlushResult,
    FlushSucceeded,
    FlushUnknown,
)


logger = logging.getLogger(__name__)

_APP_ID = "avibe"
_SIDECAR_TIMEOUT_SECONDS = 20.0
_AGENTIC_TIMEOUT_HEADER = "X-Avibe-Memory-Agentic-Timeout-Seconds"
_AGENTIC_ROUND_HEADER = "X-Avibe-Memory-Agentic-Round"
_SIDECAR_TIMEOUT_RESPONSE_MARGIN_SECONDS = 0.05
_ADD_TIMEOUT_SECONDS = 30.0
_FLUSH_TIMEOUT_SECONDS = 300.0
PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS = 8.0
PROCESSING_PROBE_DEADLINE_MARGIN_SECONDS = 2.0
PROCESSING_PROBE_MAX_ENDPOINTS = 4
PROCESSING_PROBE_MAX_DEADLINE_SECONDS = (
    PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS * PROCESSING_PROBE_MAX_ENDPOINTS
    + PROCESSING_PROBE_DEADLINE_MARGIN_SECONDS
)
_PROCESSING_TIMEOUT_SECONDS = PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS
_MAX_PROCESSING_PROBE_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PROCESSING_PROBE_VECTOR_ITEMS = 200_000
_PREFLIGHT_TIMEOUT_SECONDS = 30.0
_CHAT_PROBE_MAX_TOKENS = 8
_CHAT_PROBE_TERMINAL_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "function_call"}
)
_PREFLIGHT_IMAGE_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAYElEQVR42u3QAQ0AAAwC"
    "IPuX1hzfIQLpcxEgQIAAAQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAA"
    "AQIECBAgQIAAAQIECBAgQIAAAQIECBAgQIAAAQLuG0bQw7Ko2TvAAAAAAElFTkSuQmCC"
)
MULTIMODAL_EXPLICIT_ENV = "AVIBE_MEMORY_MULTIMODAL_EXPLICIT"
_PROFILE_QUERY = "profile"
MemoryRerankProvider = Literal["deepinfra", "vllm", "dashscope"]
DEFAULT_MEMORY_RERANK_PROVIDER: MemoryRerankProvider = "deepinfra"
DASHSCOPE_RERANK_PATH = "api/v1/services/rerank/text-rerank/text-rerank"

_EVEROS_EXACT_SORT_WINDOW = 20_000
_MAX_PROFILE_TIMESTAMP_MS = 4_102_444_800_000
# The pinned EverOS 1.2.3 `/add` route emits these only while ingesting attachment
# content, before boundary preparation or any durable provider write. Unknown
# codes stay out: destructive text-only replay requires positive no-write proof.
_ATTACHMENT_ADD_REJECTION_CODES_VALIDATED_EVEROS_VERSION = "1.2.3"
_ATTACHMENT_ADD_REJECTION_CODES_WITHOUT_WRITE = frozenset(
    {"UNSUPPORTED_FORMAT", "CAPABILITY_UNAVAILABLE"}
)

ProviderAttachment = CaptureAttachment


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    """Allowlisted public EverOS health facts, with no Avibe readiness verdict."""

    status: Literal["ok"]
    version: str
    capabilities: dict[str, bool]
    disabled_features: tuple[str, ...]
    cascade: dict[str, object] | None

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "version": self.version,
            "capabilities": dict(self.capabilities),
            "disabled_features": list(self.disabled_features),
            "cascade": dict(self.cascade) if self.cascade is not None else None,
        }


@dataclass
class AgenticRecallTelemetry:
    """Scrub-safe state retained across one bounded agentic recall."""

    round: Literal["round1", "round2", "unknown"] = "unknown"
    timed_out: bool = False


@dataclass(frozen=True)
class ProviderCapture:
    session_ref: ProviderSessionRef
    text: str
    provider_timestamp_ms: int
    attachments: tuple[CaptureAttachment, ...] = ()


def attachment_add_rejection_proves_no_write(
    capture: ProviderCapture,
    result: AddResult,
) -> bool:
    """Return whether EverOS proved an attachment add stopped before writing."""

    return (
        bool(capture.attachments)
        and isinstance(result, AddRejected)
        and result.error_code in _ATTACHMENT_ADD_REJECTION_CODES_WITHOUT_WRITE
    )


@dataclass(frozen=True)
class _ProcessingProbeSpec:
    base_url: str | None
    api_key: str | None
    path: str
    payload: dict[str, Any]
    validator: Callable[[Any], bool]


def processing_probe_deadline_seconds(
    *,
    llm: tuple[str | None, str | None],
    embedding: tuple[str | None, str | None],
    rerank: tuple[str | None, str | None] | None = None,
    multimodal: tuple[str | None, str | None] | None = None,
) -> float:
    """Bound a probe child by the largest serialized provider group."""

    group_sizes: dict[tuple[str, str], int] = {}
    for pair in (llm, embedding, rerank, multimodal):
        if pair is None:
            continue
        group = _processing_provider_group_key(*pair)
        if group is None:
            continue
        group_sizes[group] = group_sizes.get(group, 0) + 1
    largest_group = max(group_sizes.values(), default=1)
    deadline = (
        PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS * largest_group
        + PROCESSING_PROBE_DEADLINE_MARGIN_SECONDS
    )
    return min(deadline, PROCESSING_PROBE_MAX_DEADLINE_SECONDS)


class MemoryProviderFailure(RuntimeError):
    """A redaction-safe failure already classified by the provider adapter."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_processing_failed",
        *,
        retryable: bool = True,
        ambiguous: bool = False,
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_processing_failed"
        )
        super().__init__(closed_error)
        self.error = closed_error
        self.retryable = bool(retryable)
        self.ambiguous = bool(ambiguous)


class MemoryProviderSystemFailure(MemoryProviderFailure):
    """The sidecar or its configured processing dependencies are unavailable."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_sidecar_unavailable",
        *,
        ambiguous: bool = False,
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_sidecar_unavailable"
        )
        super().__init__(closed_error, retryable=True, ambiguous=ambiguous)


@dataclass(frozen=True)
class MemoryPreflightFailure:
    error: Literal[
        "memory_embedding_unavailable",
        "memory_llm_unavailable",
        "memory_rerank_unavailable",
        "memory_multimodal_unavailable",
    ]
    diagnostic: MemoryPreflightDiagnostic


@dataclass(frozen=True)
class MemoryPreflightResult:
    ok: bool
    failure: MemoryPreflightFailure | None = None

    def payload(self) -> dict[str, object]:
        if self.ok or self.failure is None:
            return {"ok": True}
        return {"ok": False, "error": self.failure.error, "diagnostic": self.failure.diagnostic.payload()}


class EverOSPort:
    """Private HTTP adapter for the pinned EverOS sidecar.

    The adapter owns the public EverOS payload shapes and response mapping.  It
    deliberately uses the sidecar's Unix socket only: no provider route or
    processing credential is exposed through the caller-facing Memory module.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
        embedding_api_key: str | None = None,
        rerank_base_url: str | None = None,
        rerank_model: str | None = None,
        rerank_api_key: str | None = None,
        rerank_provider: str | None = None,
        multimodal_base_url: str | None = None,
        multimodal_model: str | None = None,
        multimodal_api_key: str | None = None,
        processing_health_check: Callable[[], Awaitable[bool]] | None = None,
        runtime_active: Callable[[], bool] | None = None,
        sidecar_timeout_seconds: float = _SIDECAR_TIMEOUT_SECONDS,
        add_timeout_seconds: float = _ADD_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = _FLUSH_TIMEOUT_SECONDS,
        processing_timeout_seconds: float = _PROCESSING_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._llm_base_url = _normalized_endpoint_url(llm_base_url)
        self._llm_model = _optional_string(llm_model)
        self._llm_api_key = _optional_string(llm_api_key)
        self._embedding_base_url = _normalized_endpoint_url(embedding_base_url)
        self._embedding_model = _optional_string(embedding_model)
        self._embedding_api_key = _optional_string(embedding_api_key)
        self._rerank_base_url = _normalized_endpoint_url(rerank_base_url)
        self._rerank_model = _optional_string(rerank_model)
        self._rerank_api_key = _optional_string(rerank_api_key)
        self._rerank_provider = _normalized_rerank_provider(
            rerank_provider,
            base_url=rerank_base_url,
            model=rerank_model,
        )
        self._multimodal_base_url = _normalized_endpoint_url(multimodal_base_url)
        self._multimodal_model = _optional_string(multimodal_model)
        self._multimodal_api_key = _optional_string(multimodal_api_key)
        self._processing_health_check = processing_health_check
        self._runtime_active = runtime_active or (lambda: True)
        self._sidecar_timeout_seconds = _positive_timeout(sidecar_timeout_seconds, _SIDECAR_TIMEOUT_SECONDS)
        self._add_timeout_seconds = _positive_timeout(add_timeout_seconds, _ADD_TIMEOUT_SECONDS)
        self._flush_timeout_seconds = _positive_timeout(flush_timeout_seconds, _FLUSH_TIMEOUT_SECONDS)
        self._processing_timeout_seconds = _positive_timeout(
            processing_timeout_seconds,
            _PROCESSING_TIMEOUT_SECONDS,
        )
        self._processing_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        """The owned UDS endpoint, retained for process/runtime coordination."""

        return self._socket_path

    @property
    def agentic_budget_enforced(self) -> bool:
        """Whether this adapter enforces a bounded agentic wall-clock budget."""

        return True

    async def add(self, capture: ProviderCapture) -> AddResult:
        """Durably hand one capture to EverOS and return its acknowledgement."""

        content: str | list[dict[str, str]] = capture.text
        if capture.attachments:
            content = []
            if capture.text.strip():
                content.append({"type": "text", "text": capture.text})
            content.extend(
                {
                    "type": attachment.kind,
                    "name": attachment.name,
                    "uri": attachment.uri,
                    "ext": attachment.ext,
                }
                for attachment in capture.attachments
            )

        status_code, raw = await self._sidecar_write(
            "POST",
            "/api/v2/memory/add",
            {
                "session_id": capture.session_ref.session_id,
                "app_id": _APP_ID,
                "project_id": capture.session_ref.project_ref,
                "messages": [
                    {
                        "sender_id": capture.session_ref.principal_id,
                        "role": "user",
                        "timestamp": capture.provider_timestamp_ms,
                        "content": content,
                    }
                ],
            },
            timeout_seconds=self._add_timeout_seconds,
        )
        envelope = _optional_json_object(raw)
        if not 200 <= status_code < 300:
            logger.warning("EverOS add rejected status=%s", status_code)
            error = envelope.get("error") if envelope is not None else None
            error_code = error.get("code") if isinstance(error, dict) else None
            return AddRejected(
                request_id=_bounded_opaque_string(
                    envelope.get("request_id") if envelope else None
                ),
                error_code=_bounded_opaque_string(error_code),
                server_fault=status_code >= 500,
            )
        data = envelope.get("data") if envelope is not None else None
        status = data.get("status") if isinstance(data, dict) else None
        if envelope is None:
            logger.warning("EverOS add returned 2xx with an unusable response body")
        elif status is not None and status not in {"accumulated", "extracted"}:
            logger.warning("EverOS add returned an unsupported status value")
        return AddAck(
            request_id=_strict_receipt_id(envelope.get("request_id") if envelope else None),
            status=status if status in {"accumulated", "extracted"} else None,
        )

    async def flush(self, session_ref: ProviderSessionRef) -> FlushResult:
        """Trigger distillation and return a total provider outcome."""

        try:
            status_code, raw = await self._sidecar_write(
                "POST",
                "/api/v2/memory/flush",
                {
                    "session_id": session_ref.session_id,
                    "app_id": _APP_ID,
                    "project_id": session_ref.project_ref,
                },
                timeout_seconds=self._flush_timeout_seconds,
            )
        except MemoryProviderSystemFailure as failure:
            return (
                FlushUnknown(reason="transport")
                if failure.ambiguous
                else FlushRetryable()
            )
        except MemoryProviderFailure as failure:
            reason: Literal["timeout", "transport"] = (
                "timeout" if failure.error == "memory_provider_timeout" else "transport"
            )
            return (
                FlushUnknown(reason=reason)
                if failure.ambiguous or reason == "timeout"
                else FlushRetryable()
            )

        envelope = _optional_json_object(raw)
        raw_request_id = envelope.get("request_id") if envelope else None
        if 200 <= status_code < 300:
            request_id = _strict_receipt_id(raw_request_id)
            data = envelope.get("data") if envelope is not None else None
            status = data.get("status") if isinstance(data, dict) else None
            if envelope is None:
                logger.warning("EverOS flush returned 2xx with an unusable response body")
            elif status is not None and status not in {"extracted", "no_extraction"}:
                logger.warning("EverOS flush returned an unsupported status value")
            if (
                request_id is None
                or status not in {"extracted", "no_extraction"}
            ):
                return FlushUnknown(reason="transport")
            return FlushSucceeded(request_id=request_id, status=status)
        error = envelope.get("error") if envelope is not None else None
        error_code = error.get("code") if isinstance(error, dict) else None
        return FlushRejected(
            request_id=_bounded_opaque_string(raw_request_id),
            error_code=_bounded_opaque_string(error_code),
            server_fault=status_code >= 500,
        )

    async def _sidecar_write(
        self,
        method: str,
        route: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[int, bytes | None]:
        """Return the HTTP verdict even when its body is unusable."""

        try:
            runtime_active = bool(self._runtime_active())
        except Exception:
            runtime_active = False
        if not runtime_active:
            raise MemoryProviderSystemFailure("memory_operation_in_progress")

        started = time.monotonic()
        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://memory-sidecar",
                timeout=httpx.Timeout(timeout_seconds or self._sidecar_timeout_seconds, connect=3.0),
                trust_env=False,
            ) as client:
                async with client.stream(method, route, json=payload) as response:
                    status_code = response.status_code
                    try:
                        raw = await _read_response(response)
                    except (httpx.TransportError, OSError):
                        if 200 <= status_code < 300:
                            raise
                        logger.warning(
                            "EverOS sidecar rejection body lost route=%s status=%s",
                            route,
                            status_code,
                        )
                        raw = None
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            logger.warning("EverOS sidecar connection timeout route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderSystemFailure() from exc
        except httpx.TimeoutException as exc:
            logger.warning("EverOS sidecar timeout route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderFailure(
                "memory_provider_timeout",
                ambiguous=True,
            ) from exc
        except (
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.CloseError,
        ) as exc:
            logger.warning(
                "EverOS sidecar response lost route=%s latency_ms=%s",
                route,
                _elapsed_ms(started),
            )
            raise MemoryProviderSystemFailure(ambiguous=True) from exc
        except httpx.ConnectError as exc:
            logger.warning("EverOS sidecar unavailable route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderSystemFailure() from exc
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("EverOS sidecar transport failed route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderSystemFailure(ambiguous=True) from exc
        logger.debug(
            "EverOS sidecar write complete route=%s status=%s latency_ms=%s",
            route,
            status_code,
            _elapsed_ms(started),
        )
        return status_code, raw

    async def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
        *,
        method: Literal["keyword", "vector", "hybrid", "agentic"] = "hybrid",
        include_profile: bool = True,
        session_ref: ProviderSessionRef | None = None,
        timeout_seconds: float | None = None,
        agentic_telemetry: AgenticRecallTelemetry | None = None,
    ) -> tuple[ProviderSearchItem, ...]:
        response_metadata: dict[str, str] = {}
        try:
            data = await self._search_data(
                principal_id,
                project_id,
                query,
                limit,
                method=method,
                include_profile=include_profile,
                session_ref=session_ref,
                timeout_seconds=timeout_seconds,
                telemetry=response_metadata,
            )
            return _map_search_items(data, principal_id=principal_id, limit=limit)
        finally:
            if method == "agentic" and agentic_telemetry is not None:
                round_value = response_metadata.get("round")
                if round_value in {"round1", "round2"}:
                    agentic_telemetry.round = round_value

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]:
        del project_id
        body = await self._sidecar_request(
            "POST",
            "/api/v2/memory/get",
            {
                "user_id": principal_id,
                "app_id": _APP_ID,
                "project_id": "default",
                "memory_type": "profile",
                "page": 1,
                "page_size": 1,
            },
            require_json=True,
        )
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not _is_json_value(data):
            raise MemoryProviderFailure("memory_provider_response_invalid")
        profile = _map_profile_item(data, principal_id=principal_id)
        # "Valid response, no profile payload" is exactly "zero items returned",
        # so it needs no state on this provider: one EverOSPort serves every
        # principal, and a field here is whichever concurrent read finished last.
        return () if profile is None else (profile,)

    async def list_episodes(
        self,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListPage:
        """Return one strictly projected EverOS episode page."""

        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_MEMORY_LIST_PAGE_SIZE
        ):
            raise MemoryProviderFailure("memory_invalid_input", retryable=False)
        body = await self._sidecar_request(
            "POST",
            "/api/v2/memory/get",
            {
                "user_id": principal_id,
                "app_id": _APP_ID,
                "project_id": project_id,
                "memory_type": "episode",
                "page": page,
                "page_size": page_size,
                "sort_by": "timestamp",
                "sort_order": "desc",
            },
            require_json=True,
        )
        return _map_episode_page(
            body,
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
        )

    async def health(self) -> bool:
        try:
            await self.health_snapshot()
        except MemoryProviderFailure:
            return False
        return True

    async def health_snapshot(self) -> ProviderHealthSnapshot:
        """Read and strictly project the public sidecar health response once."""

        payload = await self._sidecar_request("GET", "/health", None, require_json=True)
        snapshot = _provider_health_snapshot(payload)
        if snapshot is None:
            raise MemoryProviderFailure("memory_provider_response_invalid", retryable=False)
        return snapshot

    async def processing_healthy(self) -> bool:
        """Probe the mandatory LLM and embedding endpoints.

        The worker may call this after ambiguous provider errors.  The lock keeps
        several queued rows from multiplying credential probes during an outage.
        Matching provider credentials are serialized; independent groups overlap.
        Optional rerank availability is reported by sidecar health and gates only
        agentic recall, so it is deliberately excluded from this aggregate.
        """

        async with self._processing_lock:
            if self._processing_health_check is not None:
                try:
                    return bool(await self._processing_health_check())
                except Exception:
                    return False
            if not self._processing_configured():
                return False
            probes = [
                _ProcessingProbeSpec(
                    base_url=self._llm_base_url,
                    api_key=self._llm_api_key,
                    path="chat/completions",
                    payload={
                        "model": self._llm_model,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                        "max_tokens": _CHAT_PROBE_MAX_TOKENS,
                        "temperature": 0,
                    },
                    validator=_valid_chat_probe_response,
                ),
                _ProcessingProbeSpec(
                    base_url=self._embedding_base_url,
                    api_key=self._embedding_api_key,
                    path="embeddings",
                    payload={"model": self._embedding_model, "input": "memory health check"},
                    validator=_valid_embedding_probe_response,
                ),
            ]
            if self._multimodal_configured():
                probes.append(
                    _ProcessingProbeSpec(
                        base_url=self._multimodal_base_url,
                        api_key=self._multimodal_api_key,
                        path="chat/completions",
                        payload=_multimodal_preflight_payload(self._multimodal_model),
                        validator=_valid_chat_probe_response,
                    )
                )
            groups: dict[tuple[str, str], list[_ProcessingProbeSpec]] = {}
            for probe in probes:
                group = _processing_provider_group_key(probe.base_url, probe.api_key)
                if group is None:
                    return False
                groups.setdefault(group, []).append(probe)
            results = await asyncio.gather(
                *(self._run_processing_probe_group(group) for group in groups.values()),
                return_exceptions=True,
            )
            return all(result is True for result in results)

    async def preflight(self) -> MemoryPreflightResult:
        """Run bounded requests for startup-critical processing endpoints."""
        checks = [
            (
                "llm",
                self._llm_base_url,
                self._llm_api_key,
                "chat/completions",
                {
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": "OK"}],
                    "max_tokens": _CHAT_PROBE_MAX_TOKENS,
                    "temperature": 0,
                },
                _valid_chat_probe_response,
            ),
            ("embedding", self._embedding_base_url, self._embedding_api_key, "embeddings", {
                "model": self._embedding_model, "input": "OK",
            }, _valid_embedding_probe_response),
        ]
        if self._multimodal_configured():
            checks.append(
                (
                    "multimodal",
                    self._multimodal_base_url,
                    self._multimodal_api_key,
                    "chat/completions",
                    _multimodal_preflight_payload(self._multimodal_model),
                    _valid_chat_probe_response,
                )
            )
        first_failure = None
        for side, base_url, api_key, path, payload, validator in checks:
            try:
                failure = await asyncio.wait_for(
                    self._preflight_endpoint(
                        side,
                        base_url,
                        api_key,
                        path,
                        payload,
                        validator,
                    ),
                    timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                error_name = _preflight_error_name(side)
                failure = MemoryPreflightFailure(
                    error_name,
                    MemoryPreflightDiagnostic(side, message="provider_request_timed_out"),
                )
            if failure is not None and first_failure is None:
                first_failure = failure
        return MemoryPreflightResult(first_failure is None, first_failure)

    def _processing_configured(self) -> bool:
        return all(
            (
                self._llm_base_url,
                self._llm_model,
                self._llm_api_key,
                self._embedding_base_url,
                self._embedding_model,
                self._embedding_api_key,
            )
        )

    def _rerank_configured(self) -> bool:
        return all((self._rerank_base_url, self._rerank_model, self._rerank_api_key))

    def _rerank_probe_spec(self) -> _ProcessingProbeSpec:
        return _rerank_probe_spec(
            base_url=self._rerank_base_url,
            api_key=self._rerank_api_key,
            model=self._rerank_model,
            provider=self._rerank_provider,
        )

    def _multimodal_configured(self) -> bool:
        return all(
            (
                self._multimodal_base_url,
                self._multimodal_model,
                self._multimodal_api_key,
            )
        )

    async def _search_data(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
        *,
        method: Literal["keyword", "vector", "hybrid", "agentic"],
        include_profile: bool,
        session_ref: ProviderSessionRef | None,
        timeout_seconds: float | None,
        telemetry: dict[str, str],
    ) -> dict[str, Any]:
        if method not in {"keyword", "vector", "hybrid", "agentic"}:
            raise MemoryProviderFailure("memory_invalid_input", retryable=False)
        if session_ref is not None and (
            session_ref.principal_id != principal_id
            or session_ref.project_ref != project_id
        ):
            raise MemoryProviderFailure("memory_access_denied", retryable=False)
        request: dict[str, Any] = {
            "user_id": principal_id,
            "app_id": _APP_ID,
            "project_id": project_id,
            "query": query,
            "method": method,
            "top_k": limit,
            "include_profile": include_profile,
            "enable_llm_rerank": False,
        }
        if session_ref is not None:
            request["filters"] = {"session_id": session_ref.session_id}
        try:
            if method == "agentic":
                request_timeout = min(
                    _positive_timeout(
                        timeout_seconds,
                        MAX_AGENTIC_TIMEOUT_SECONDS,
                    ),
                    MAX_AGENTIC_TIMEOUT_SECONDS,
                )
                body = await asyncio.wait_for(
                    self._sidecar_request(
                        "POST",
                        "/api/v2/memory/search",
                        request,
                        require_json=True,
                        capability_rejection=True,
                        timeout_seconds=request_timeout,
                        response_metadata=telemetry,
                    ),
                    timeout=request_timeout,
                )
            else:
                body = await self._sidecar_request(
                    "POST",
                    "/api/v2/memory/search",
                    request,
                    require_json=True,
                    capability_rejection=True,
                )
            if not isinstance(body, dict):
                raise MemoryProviderFailure("memory_provider_response_invalid")
            data = body.get("data")
            if not isinstance(data, dict) or not _is_json_value(data):
                raise MemoryProviderFailure("memory_provider_response_invalid")
        except asyncio.TimeoutError as exc:
            raise MemoryProviderFailure("memory_provider_timeout") from exc
        return data

    async def _sidecar_request(
        self,
        method: str,
        route: str,
        payload: dict[str, Any] | None,
        *,
        require_json: bool,
        capability_rejection: bool = False,
        timeout_seconds: float | None = None,
        response_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        request_timeout = _positive_timeout(
            timeout_seconds,
            self._sidecar_timeout_seconds,
        )
        headers = None
        if timeout_seconds is not None:
            sidecar_timeout = max(
                0.001,
                request_timeout - _SIDECAR_TIMEOUT_RESPONSE_MARGIN_SECONDS,
            )
            headers = {_AGENTIC_TIMEOUT_HEADER: str(sidecar_timeout)}
        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://memory-sidecar",
                timeout=httpx.Timeout(
                    request_timeout,
                    connect=min(3.0, request_timeout),
                ),
                trust_env=False,
            ) as client:
                async with client.stream(
                    method,
                    route,
                    json=payload,
                    headers=headers,
                ) as response:
                    round_value = response.headers.get(_AGENTIC_ROUND_HEADER)
                    if (
                        response_metadata is not None
                        and round_value in {"round1", "round2"}
                    ):
                        response_metadata["round"] = round_value
                    if not 200 <= response.status_code < 300:
                        logger.warning(
                            "EverOS sidecar request failed route=%s status=%s latency_ms=%s",
                            route,
                            response.status_code,
                            _elapsed_ms(started),
                        )
                        raise MemoryProviderFailure(
                            "memory_provider_timeout"
                            if timeout_seconds is not None and response.status_code == 504
                            else (
                                "memory_capability_unavailable"
                                if capability_rejection and response.status_code == 422
                                else "memory_processing_failed"
                            )
                        )
                    if not require_json:
                        await _read_response(response)
                        logger.debug(
                            "EverOS sidecar request complete route=%s status=%s latency_ms=%s",
                            route,
                            response.status_code,
                            _elapsed_ms(started),
                        )
                        return None
                    raw = await _read_response(response)
        except MemoryProviderFailure:
            raise
        except httpx.TimeoutException as exc:
            logger.warning("EverOS sidecar timeout route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderFailure("memory_provider_timeout") from exc
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("EverOS sidecar unavailable route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderSystemFailure() from exc

        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise MemoryProviderFailure("memory_provider_response_invalid") from exc
        if not isinstance(value, dict):
            raise MemoryProviderFailure("memory_provider_response_invalid")
        logger.debug("EverOS sidecar request complete route=%s latency_ms=%s", route, _elapsed_ms(started))
        return value

    async def _probe_processing_endpoint(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        path: str,
        payload: dict[str, Any],
        validator: Callable[[Any], bool],
    ) -> bool:
        if not base_url or not api_key:
            return False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._processing_timeout_seconds, connect=3.0),
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        logger.info(
                            "Memory processing probe failed endpoint=%s status=%s",
                            path,
                            response.status_code,
                        )
                        return False
                    raw = await _read_bounded_processing_response(response)
            value = json.loads(raw)
        except (httpx.HTTPError, OSError, TypeError, ValueError, MemoryProviderFailure):
            logger.info("Memory processing probe unavailable endpoint=%s", path)
            return False
        return bool(validator(value))

    async def _run_processing_probe_group(
        self,
        probes: list[_ProcessingProbeSpec],
    ) -> bool:
        healthy = True
        for probe in probes:
            try:
                result = await self._probe_processing_endpoint(
                    base_url=probe.base_url,
                    api_key=probe.api_key,
                    path=probe.path,
                    payload=probe.payload,
                    validator=probe.validator,
                )
            except Exception:
                result = False
            healthy = healthy and result is True
        return healthy

    async def _preflight_endpoint(
        self,
        side,
        base_url,
        api_key,
        path,
        payload,
        validator,
    ):
        error_name = _preflight_error_name(side)
        diagnostic = MemoryPreflightDiagnostic(side)
        if not base_url or not api_key or not path:
            failure = MemoryPreflightFailure(error_name, replace(diagnostic, message="endpoint_not_configured"))
            return failure
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(_PREFLIGHT_TIMEOUT_SECONDS, connect=2.0), trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    raw = await _read_bounded_processing_response(response)
                    status_code = response.status_code
            try:
                value = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                value = None
            if 200 <= status_code < 300 and validator(value):
                return None
            code = None
            if 200 <= status_code < 300:
                message = _probe_response_issue(side, value) or "provider_response_invalid"
            else:
                message = f"HTTP {status_code}"
            if isinstance(value, dict) and isinstance(value.get("error"), dict):
                error = value["error"]
                code = _bounded_opaque_string(
                    _bounded_preflight_message(
                        error.get("code"),
                        api_key=api_key,
                        base_url=base_url,
                    )
                )
                message = "provider_error"
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, status_code, code, message))
            return failure
        except httpx.TimeoutException:
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, message="provider_request_timed_out"))
            return failure
        except MemoryProviderFailure:
            failure = MemoryPreflightFailure(
                error_name,
                MemoryPreflightDiagnostic(side, message="provider_response_too_large"),
            )
            return failure
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, message="provider_unavailable"))
            return failure


def _bounded_preflight_message(
    value: object,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    if not isinstance(value, str):
        return ""
    message = " ".join(value.split())
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    if base_url:
        message = message.replace(base_url.rstrip("/"), "[URL]")
    message = re.sub(r"https?://[^\s]+", "[URL]", message)
    message = re.sub(r"([?&](?:api[_-]?key|token|secret|password)=)[^&\s]+", r"\1[REDACTED]", message, flags=re.IGNORECASE)
    return message[:512]


async def _read_response(response: httpx.Response) -> bytes:
    return await response.aread()


async def _read_bounded_processing_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_PROCESSING_PROBE_RESPONSE_BYTES:
            raise MemoryProviderFailure("memory_provider_response_invalid")
        chunks.append(chunk)
    return b"".join(chunks)


def _map_search_items(
    data: dict[str, Any],
    *,
    principal_id: str,
    limit: int,
) -> tuple[ProviderSearchItem, ...]:
    episodes = data.get("episodes", [])
    if not isinstance(episodes, list):
        raise MemoryProviderFailure("memory_provider_response_invalid")

    items: list[ProviderSearchItem] = []
    for episode in episodes:
        if len(items) >= limit:
            break
        if not isinstance(episode, dict):
            continue
        if episode.get("user_id") != principal_id:
            continue
        episode_id = _strict_receipt_id(episode.get("id"))
        episode_score = _provider_score(episode)
        episode_timestamp = _first_record_timestamp(episode)
        text = _episode_text(episode)
        if text is not None:
            items.append(
                ProviderSearchItem(
                    item=MemoryItem(kind="episode", text=text, date=_record_date(episode)),
                    score=episode_score,
                    episode_id=episode_id,
                    timestamp=episode_timestamp,
                    provider_rank=len(items),
                    queried_owner=principal_id,
                )
            )
        if len(items) >= limit:
            break
        facts = episode.get("atomic_facts", [])
        if facts is None:
            facts = []
        if not isinstance(facts, list):
            raise MemoryProviderFailure("memory_provider_response_invalid")
        for fact in facts:
            if len(items) >= limit:
                break
            if not isinstance(fact, dict):
                continue
            text = _safe_text(fact.get("content"))
            if text is not None:
                fact_score = _provider_score(fact)
                items.append(
                    ProviderSearchItem(
                        item=MemoryItem(kind="fact", text=text, date=_record_date(fact, episode)),
                        score=fact_score if fact_score is not None else episode_score,
                        episode_id=episode_id,
                        timestamp=_first_record_timestamp(fact, episode),
                        provider_rank=len(items),
                        queried_owner=principal_id,
                    )
                )
    return tuple(items)


def _provider_score(record: dict[str, Any]) -> float | None:
    for key in ("score", "relevance_score"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def _first_record_timestamp(*records: dict[str, Any]) -> str | None:
    for record in records:
        for key in ("timestamp", "created_at", "createdAt", "date"):
            timestamp = _record_timestamp(record.get(key))
            if timestamp is not None:
                return timestamp
    return None


def _map_episode_page(
    body: dict[str, Any] | None,
    *,
    principal_id: str,
    project_id: str,
    page: int,
    page_size: int,
) -> MemoryListPage:
    """Validate the pinned EverOS `/get` envelope before projection."""

    if not isinstance(body, dict) or set(body) != {"request_id", "data"}:
        raise MemoryProviderFailure("memory_provider_response_invalid")
    request_id = _strict_receipt_id(body.get("request_id"))
    data = body.get("data")
    data_keys = {
        "episodes",
        "profiles",
        "agent_cases",
        "agent_skills",
        "total_count",
        "count",
    }
    if (
        not request_id
        or not isinstance(data, dict)
        or set(data) != data_keys
        or not _is_json_value(data)
    ):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    episodes = data.get("episodes")
    total_count = data.get("total_count")
    count = data.get("count")
    if (
        not isinstance(episodes, list)
        or len(episodes) > page_size
        or any(data.get(key) != [] for key in ("profiles", "agent_cases", "agent_skills"))
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(episodes)
        or total_count < count
        or count != min(
            page_size,
            max(total_count - (page - 1) * page_size, 0),
        )
    ):
        raise MemoryProviderFailure("memory_provider_response_invalid")

    items = tuple(
        _map_list_episode(
            episode,
            principal_id=principal_id,
            project_id=project_id,
        )
        for episode in episodes
    )
    if len({item.id for item in items}) != len(items):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    timestamps = tuple(
        datetime.fromisoformat(item.timestamp.replace("Z", "+00:00"))
        for item in items
    )
    if any(
        next_timestamp > timestamp
        for timestamp, next_timestamp in zip(timestamps, timestamps[1:])
    ):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    warnings = (
        ("memory_list_truncated",)
        if total_count > _EVEROS_EXACT_SORT_WINDOW
        else ()
    )
    return MemoryListPage(
        items=items,
        page=page,
        page_size=page_size,
        count=count,
        total_count=total_count,
        warnings=warnings,
    )


def _map_list_episode(
    value: Any,
    *,
    principal_id: str,
    project_id: str,
) -> MemoryListItem:
    item_keys = {
        "id",
        "user_id",
        "app_id",
        "project_id",
        "session_id",
        "timestamp",
        "sender_ids",
        "summary",
        "subject",
        "episode",
        "type",
    }
    if not isinstance(value, dict) or set(value) != item_keys:
        raise MemoryProviderFailure("memory_provider_response_invalid")
    item_id = _strict_receipt_id(value.get("id"))
    session_id = _strict_receipt_id(value.get("session_id"))
    sender_ids = value.get("sender_ids")
    subject = _safe_list_text(value.get("subject"), allow_empty=True)
    summary = _safe_list_text(value.get("summary"), allow_empty=True)
    episode = _safe_list_text(value.get("episode"), allow_empty=False)
    timestamp = _record_timestamp(value.get("timestamp"))
    if (
        not item_id
        or not session_id
        or value.get("user_id") != principal_id
        or value.get("app_id") != _APP_ID
        or value.get("project_id") != project_id
        or value.get("type") != "Conversation"
        or not isinstance(sender_ids, list)
        or any(not _strict_receipt_id(sender_id) for sender_id in sender_ids)
        or subject is None
        or summary is None
        or episode is None
        or timestamp is None
    ):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    return MemoryListItem(
        id=item_id,
        subject=subject,
        summary=summary,
        body=episode,
        timestamp=timestamp,
        project=project_id,
    )


def _map_profile_item(data: dict[str, Any], *, principal_id: str) -> MemoryItem | None:
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("user_id") != principal_id:
            continue
        profile_data = profile.get("profile_data")
        structured_profile = _structured_profile(profile_data)
        text = _canonical_profile_text(profile_data)
        if text is not None:
            updated_at = structured_profile.updated_at if structured_profile is not None else None
            return MemoryItem(
                kind="profile",
                text=text,
                date=updated_at.split("T", 1)[0] if updated_at is not None else _record_date(profile),
                profile=structured_profile,
            )
    return None


def _structured_profile(value: Any) -> MemoryProfile | None:
    """Map only the known EverOS profile fields into stable caller-facing types."""

    if not isinstance(value, dict):
        return None

    summary = _safe_text(value.get("summary")) if "summary" in value else None
    explicit_info = _structured_explicit_info(value)
    implicit_traits = _structured_implicit_traits(value)
    summary = _repair_stale_profile_summary(summary, explicit_info, implicit_traits)
    updated_at = _normalized_profile_timestamp(value.get("profile_timestamp_ms"))

    # A provider timestamp is metadata, not readable profile content. Unknown
    # shapes retain their canonical raw-text fallback for compatibility.
    if summary is None and not explicit_info and not implicit_traits:
        return None
    return MemoryProfile(
        summary=summary,
        explicit_info=explicit_info,
        implicit_traits=implicit_traits,
        updated_at=updated_at,
    )


def _repair_stale_profile_summary(
    summary: str | None,
    explicit_info: tuple[MemoryProfileExplicitInfo, ...],
    implicit_traits: tuple[MemoryProfileTrait, ...],
) -> str | None:
    """Repair EverAlgo summaries surfaced by EverOS when pinned to item one.

    EverAlgo's profile updater appends new items but rebuilds ``summary`` from
    the first non-empty explicit description. That makes a transient first
    message the permanent headline. Only the distinctive stale shape is
    changed here; provider-authored summaries that differ from the first item
    remain authoritative, and the raw provider JSON stays untouched.
    """

    if summary is None:
        return None

    if len(explicit_info) > 1 and summary == explicit_info[0].description:
        for info in reversed(explicit_info[1:]):
            if info.description != summary:
                return info.description
        return summary

    return summary


def _structured_explicit_info(value: dict[str, Any]) -> tuple[MemoryProfileExplicitInfo, ...]:
    if "explicit_info" not in value:
        return ()
    entries = value["explicit_info"]
    if not isinstance(entries, list):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    mapped: list[MemoryProfileExplicitInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        description = _safe_text(entry.get("description"))
        if description is None:
            continue
        mapped.append(
            MemoryProfileExplicitInfo(
                description=description,
                category=_safe_text(entry.get("category")),
                evidence=_safe_text(entry.get("evidence")),
            )
        )
    return tuple(mapped)


def _structured_implicit_traits(value: dict[str, Any]) -> tuple[MemoryProfileTrait, ...]:
    if "implicit_traits" not in value:
        return ()
    entries = value["implicit_traits"]
    if not isinstance(entries, list):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    mapped: list[MemoryProfileTrait] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        description = _safe_text(entry.get("description"))
        if description is None:
            continue
        mapped.append(
            MemoryProfileTrait(
                description=description,
                trait=_safe_text(entry.get("trait")),
                basis=_safe_text(entry.get("basis")),
                evidence=_safe_text(entry.get("evidence")),
            )
        )
    return tuple(mapped)


def _normalized_profile_timestamp(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_PROFILE_TIMESTAMP_MS:
        return None
    try:
        instant = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return instant.isoformat().replace("+00:00", "Z")


def _episode_text(episode: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("subject", "summary", "episode", "content"):
        text = _safe_text(episode.get(key))
        if text is not None and text not in parts:
            parts.append(text)
    if not parts:
        return None
    return _safe_text("\n".join(parts))


def _canonical_profile_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _safe_text(value)
    if not isinstance(value, (dict, list)) or not _is_json_value(value):
        return None
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (RecursionError, TypeError, ValueError):
        return None
    return _safe_text(rendered)


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    raw = _utf8_bytes(text)
    if not text or raw is None:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t", "\r"} for character in text):
        return None
    return text


def _safe_list_text(value: Any, *, allow_empty: bool) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    raw = _utf8_bytes(text)
    if (
        (not allow_empty and not text)
        or raw is None
    ):
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t", "\r"} for character in text):
        return None
    return text


def _record_date(*records: dict[str, Any]) -> str | None:
    for record in records:
        for key in ("date", "created_at", "timestamp", "createdAt"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                try:
                    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
                except (OverflowError, OSError, ValueError):
                    continue
            if not isinstance(value, str) or len(value) > 128:
                continue
            candidate = value.strip()
            try:
                return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                try:
                    return datetime.strptime(candidate, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    continue
    return None


def _record_timestamp(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            instant = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and len(value) <= 128:
        try:
            instant = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
            instant = instant.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return None
    else:
        return None
    return instant.isoformat().replace("+00:00", "Z")


def _is_json_value(value: Any) -> bool:
    """Validate JSON value types without imposing Avibe payload limits."""

    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            if _utf8_bytes(item) is None:
                return False
            continue
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                return False
            continue
        if isinstance(item, list):
            pending.extend(item)
            continue
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str) or _utf8_bytes(key) is None:
                    return False
                pending.append(nested)
            continue
        return False
    return True


def _valid_chat_probe_response(value: Any) -> bool:
    return _chat_probe_response_issue(value) is None


def _chat_probe_response_issue(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "provider_response_not_object"
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        return "provider_response_missing_choices"
    if not isinstance(choices[0], dict):
        return "provider_response_invalid_choice"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "provider_response_missing_message"
    # Reasoning-model probes may omit content; treat absence like content: null.
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return "provider_response_invalid_content"
    if content is None or not content.strip():
        if message.get("role") != "assistant":
            return "provider_response_invalid_role"
        finish_reason = choices[0].get("finish_reason")
        if finish_reason is None:
            return "provider_response_missing_finish_reason"
        if not isinstance(finish_reason, str):
            return "provider_response_invalid_finish_reason"
        if finish_reason not in _CHAT_PROBE_TERMINAL_FINISH_REASONS:
            return "provider_response_invalid_finish_reason"
    return None


def _probe_response_issue(side: str, value: Any) -> str | None:
    if side in {"llm", "multimodal"}:
        return _chat_probe_response_issue(value)
    return f"provider_{side}_response_invalid"


def _valid_embedding_probe_response(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return False
    vector = data[0].get("embedding")
    return (
        isinstance(vector, list)
        and bool(vector)
        and len(vector) <= _MAX_PROCESSING_PROBE_VECTOR_ITEMS
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in vector)
    )


def _valid_rerank_probe_response(value: Any) -> bool:
    return _valid_deepinfra_rerank_probe_response(value)


def _valid_deepinfra_rerank_probe_response(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    scores = value.get("scores")
    return (
        isinstance(scores, list)
        and len(scores) == 1
        and isinstance(scores[0], list)
        and len(scores[0]) == 1
        and isinstance(scores[0][0], (int, float))
        and not isinstance(scores[0][0], bool)
        and math.isfinite(scores[0][0])
    )


def _valid_ranked_results_probe_response(
    value: Any,
    *,
    results_key: tuple[str, ...] = ("results",),
) -> bool:
    current: Any = value
    for key in results_key:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    if not isinstance(current, list) or len(current) != 1 or not isinstance(current[0], dict):
        return False
    score = current[0].get("relevance_score")
    return isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)


def _rerank_probe_spec(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    provider: MemoryRerankProvider,
) -> _ProcessingProbeSpec:
    if provider == "vllm":
        return _ProcessingProbeSpec(
            base_url=base_url,
            api_key=api_key,
            path="rerank",
            payload={"model": model, "query": "OK", "documents": ["OK"]},
            validator=_valid_ranked_results_probe_response,
        )
    if provider == "dashscope":
        return _ProcessingProbeSpec(
            base_url=base_url,
            api_key=api_key,
            path=DASHSCOPE_RERANK_PATH,
            payload={
                "model": model,
                "input": {"query": "OK", "documents": ["OK"]},
                "parameters": {"return_documents": False, "top_n": 1},
            },
            validator=lambda value: _valid_ranked_results_probe_response(
                value,
                results_key=("output", "results"),
            ),
        )
    return _ProcessingProbeSpec(
        base_url=base_url,
        api_key=api_key,
        path=model or "",
        payload={"queries": ["OK"], "documents": ["OK"]},
        validator=_valid_deepinfra_rerank_probe_response,
    )


def _multimodal_preflight_payload(model: str | None) -> dict[str, Any]:
    """Build a minimal synthetic vision request containing no user data."""

    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reply with OK."},
                    {
                        "type": "image_url",
                        "image_url": {"url": _PREFLIGHT_IMAGE_DATA_URI},
                    },
                ],
            }
        ],
        "max_tokens": _CHAT_PROBE_MAX_TOKENS,
        "temperature": 0,
    }


def _processing_provider_group_key(
    base_url: str | None,
    api_key: str | None,
) -> tuple[str, str] | None:
    normalized_url = _normalized_endpoint_url(base_url)
    credential_identity = _optional_string(api_key)
    if normalized_url is None or credential_identity is None:
        return None
    return normalized_url, credential_identity


def _preflight_error_name(
    side: Literal["llm", "embedding", "rerank", "multimodal"],
) -> Literal[
    "memory_llm_unavailable",
    "memory_embedding_unavailable",
    "memory_rerank_unavailable",
    "memory_multimodal_unavailable",
]:
    return {
        "llm": "memory_llm_unavailable",
        "embedding": "memory_embedding_unavailable",
        "rerank": "memory_rerank_unavailable",
        "multimodal": "memory_multimodal_unavailable",
    }[side]


def _optional_string(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _bounded_opaque_string(value: object, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    raw = _utf8_bytes(value)
    if raw is None:
        return None
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _strict_receipt_id(value: object, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    raw = _utf8_bytes(value)
    return value if value and raw is not None and len(raw) <= max_bytes else None


def _utf8_bytes(value: str) -> bytes | None:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        return None


def _optional_json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and _is_json_value(value) else None


def _provider_health_snapshot(payload: dict[str, Any] | None) -> ProviderHealthSnapshot | None:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    version = payload.get("version")
    if not _safe_health_token(version, max_bytes=64, allow_dot=True):
        return None
    capabilities = payload.get("capabilities")
    required_capabilities = {"llm", "embed", "rerank", "multimodal_llm", "parser"}
    if (
        not isinstance(capabilities, dict)
        or not required_capabilities.issubset(capabilities)
        or len(capabilities) > 32
        or any(
            not _safe_health_token(key, max_bytes=64)
            or type(value) is not bool
            for key, value in capabilities.items()
        )
    ):
        return None
    disabled = payload.get("disabled_features")
    if (
        not isinstance(disabled, list)
        or len(disabled) > 32
        or any(not _safe_health_token(item, max_bytes=64) for item in disabled)
    ):
        return None
    cascade = _project_cascade_health(payload.get("cascade"))
    if payload.get("cascade") is not None and cascade is None:
        return None
    return ProviderHealthSnapshot(
        status="ok",
        version=version,
        capabilities={key: capabilities[key] for key in sorted(capabilities)},
        disabled_features=tuple(disabled),
        cascade=cascade,
    )


def _project_cascade_health(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    expected = {
        "healthy",
        "reasons",
        "pending",
        "failed_permanent",
        "failed_retryable",
        "drain_consecutive_failures",
        "unrecoverable_total",
        "optimize_failure_streak",
        "prune_stale_seconds",
    }
    if not isinstance(value, dict) or set(value) != expected or type(value.get("healthy")) is not bool:
        return None
    count_keys = expected - {"healthy", "reasons", "prune_stale_seconds"}
    if any(
        type(value.get(key)) is not int or not 0 <= value[key] <= 2**53
        for key in count_keys
    ):
        return None
    stale = value.get("prune_stale_seconds")
    if (
        isinstance(stale, bool)
        or not isinstance(stale, (int, float))
        or not math.isfinite(float(stale))
        or not 0 <= float(stale) <= 10**12
    ):
        return None
    reasons = value.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > 8 or any(not isinstance(item, str) for item in reasons):
        return None
    projected_reasons = tuple(dict.fromkeys(_cascade_reason_token(item) for item in reasons))
    return {
        "healthy": value["healthy"],
        "reasons": list(projected_reasons),
        **{key: value[key] for key in sorted(count_keys)},
        "prune_stale_seconds": float(stale),
    }


def _cascade_reason_token(value: str) -> str:
    if value.startswith("drain loop failing"):
        return "drain_failures"
    if value.startswith("optimize stuck"):
        return "optimize_stuck"
    if value.startswith("version cleanup stalled"):
        return "prune_stale"
    if value.startswith("cascade health probe failed"):
        return "health_probe_failed"
    return "unknown"


def _safe_health_token(value: object, *, max_bytes: int, allow_dot: bool = False) -> bool:
    if not isinstance(value, str) or not value or not value.isascii():
        return False
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if allow_dot:
        allowed += ".+"
    return len(value.encode("ascii")) <= max_bytes and all(character in allowed for character in value)


def _normalized_endpoint_url(value: str | None) -> str | None:
    normalized = _optional_string(value)
    return normalized.rstrip("/") if normalized else None


def _normalized_rerank_provider(
    value: str | None,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> MemoryRerankProvider:
    provider = _optional_string(value)
    if provider in {"deepinfra", "vllm", "dashscope"}:
        return provider
    hostname = (urlsplit(_normalized_endpoint_url(base_url) or "").hostname or "").lower()
    if hostname.endswith(".maas.aliyuncs.com"):
        return "dashscope"
    return DEFAULT_MEMORY_RERANK_PROVIDER


def _positive_timeout(value: object, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) and parsed > 0 else fallback


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


@runtime_checkable
class MemoryProviderPort(Protocol):
    async def add(self, capture: ProviderCapture) -> AddResult: ...

    async def flush(self, session_ref: ProviderSessionRef) -> FlushResult: ...

    async def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
        *,
        method: Literal["keyword", "vector", "hybrid", "agentic"] = "hybrid",
        include_profile: bool = True,
        session_ref: ProviderSessionRef | None = None,
        timeout_seconds: float | None = None,
        agentic_telemetry: AgenticRecallTelemetry | None = None,
    ) -> tuple[ProviderSearchItem, ...]: ...

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]: ...

    async def list_episodes(
        self,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListPage: ...

    async def health(self) -> bool: ...

    async def health_snapshot(self) -> ProviderHealthSnapshot: ...

    async def processing_healthy(self) -> bool: ...

    @property
    def agentic_budget_enforced(self) -> bool: ...


@dataclass
class FakeMemoryProvider:
    """In-memory provider fake for Memory module and worker contract tests."""

    healthy: bool = True
    processing_healthy_flag: bool = True
    search_items: tuple[MemoryItem | ProviderSearchItem, ...] = ()
    search_items_by_owner: dict[str, tuple[MemoryItem | ProviderSearchItem, ...]] = field(default_factory=dict)
    profile_items: tuple[MemoryItem, ...] = ()
    list_page: MemoryListPage = field(
        default_factory=lambda: MemoryListPage(
            items=(),
            page=1,
            page_size=20,
            count=0,
            total_count=0,
        )
    )
    captures: list[ProviderCapture] = field(default_factory=list)
    flushes: list[ProviderSessionRef] = field(default_factory=list)
    search_scopes: list[tuple[str, str]] = field(default_factory=list)
    profile_scopes: list[tuple[str, str]] = field(default_factory=list)
    list_requests: list[tuple[str, str, int, int]] = field(default_factory=list)
    search_policies: list[
        tuple[str, bool, ProviderSessionRef | None]
    ] = field(default_factory=list)
    search_timeouts: list[float | None] = field(default_factory=list)
    ingest_failures: Deque[BaseException] = field(default_factory=deque)
    add_results: Deque[AddResult] = field(default_factory=deque)
    flush_results: Deque[FlushResult] = field(default_factory=deque)
    search_failure: BaseException | None = None
    search_failures_by_owner: dict[str, BaseException] = field(default_factory=dict)
    profile_failure: BaseException | None = None
    profile_items_by_owner: dict[str, tuple[MemoryItem, ...]] = field(default_factory=dict)
    profile_failures_by_owner: dict[str, BaseException] = field(default_factory=dict)
    list_failure: BaseException | None = None
    health_failure: BaseException | None = None
    agentic_budget_enforced_flag: bool = False
    agentic_round: Literal["round1", "round2", "unknown"] = "unknown"
    processing_health_failure: BaseException | None = None
    health_snapshot_value: ProviderHealthSnapshot = field(
        default_factory=lambda: ProviderHealthSnapshot(
            status="ok",
            version="1.2.3",
            capabilities={
                "llm": True,
                "embed": True,
                "rerank": True,
                "multimodal_llm": True,
                "parser": True,
            },
            disabled_features=(),
            cascade=None,
        )
    )
    add_hook: Callable[[ProviderCapture], Awaitable[None]] | None = None
    flush_hook: Callable[[ProviderSessionRef], Awaitable[None]] | None = None
    processing_healthy_hook: Callable[[], Awaitable[None]] | None = None

    async def add(self, capture: ProviderCapture) -> AddResult:
        if self.ingest_failures:
            raise self.ingest_failures.popleft()
        self.captures.append(capture)
        if self.add_hook is not None:
            await self.add_hook(capture)
        if self.add_results:
            return self.add_results.popleft()
        return AddAck(request_id=f"fake-add-{len(self.captures)}", status="accumulated")

    async def flush(self, session_ref: ProviderSessionRef) -> FlushResult:
        self.flushes.append(session_ref)
        if self.flush_hook is not None:
            await self.flush_hook(session_ref)
        if self.flush_results:
            return self.flush_results.popleft()
        return FlushSucceeded(request_id=f"fake-flush-{len(self.flushes)}", status="extracted")

    async def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
        *,
        method: Literal["keyword", "vector", "hybrid", "agentic"] = "hybrid",
        include_profile: bool = True,
        session_ref: ProviderSessionRef | None = None,
        timeout_seconds: float | None = None,
        agentic_telemetry: AgenticRecallTelemetry | None = None,
    ) -> tuple[ProviderSearchItem, ...]:
        self.search_scopes.append((principal_id, project_id))
        self.search_policies.append((method, include_profile, session_ref))
        self.search_timeouts.append(timeout_seconds)
        if method == "agentic" and agentic_telemetry is not None:
            agentic_telemetry.round = self.agentic_round
        del query, limit
        failure = self.search_failures_by_owner.get(principal_id, self.search_failure)
        if failure is not None:
            raise failure
        raw_items = self.search_items_by_owner.get(
            principal_id,
            () if principal_id.endswith("-agent") else self.search_items,
        )
        return tuple(
            item
            if isinstance(item, ProviderSearchItem)
            else ProviderSearchItem(
                item=item,
                score=None,
                episode_id=None,
                timestamp=None,
                provider_rank=rank,
                queried_owner=principal_id,
            )
            for rank, item in enumerate(raw_items)
        )

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]:
        self.profile_scopes.append((principal_id, project_id))
        failure = self.profile_failures_by_owner.get(principal_id, self.profile_failure)
        if failure is not None:
            raise failure
        return self.profile_items_by_owner.get(
            principal_id,
            () if principal_id.endswith("-agent") else self.profile_items,
        )

    async def list_episodes(
        self,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListPage:
        self.list_requests.append((principal_id, project_id, page, page_size))
        if self.list_failure is not None:
            raise self.list_failure
        return replace(self.list_page, page=page, page_size=page_size)

    async def health(self) -> bool:
        if self.health_failure is not None:
            raise self.health_failure
        return self.healthy

    async def health_snapshot(self) -> ProviderHealthSnapshot:
        if self.health_failure is not None:
            raise self.health_failure
        if not self.healthy:
            raise MemoryProviderSystemFailure()
        return self.health_snapshot_value

    async def processing_healthy(self) -> bool:
        """Whether the configured processing (LLM/embedding) endpoints are reachable.

        Distinct from sidecar ``health``: the sidecar process can answer /health
        while its configured model endpoint is down. The disambiguation between a
        system outage and a poison row depends on this. The fake returns a flag; the
        real EverOS adapter performs bounded authenticated LLM+embedding
        probes.
        """
        if self.processing_healthy_hook is not None:
            await self.processing_healthy_hook()
        if self.processing_health_failure is not None:
            raise self.processing_health_failure
        return self.processing_healthy_flag

    @property
    def agentic_budget_enforced(self) -> bool:
        return self.agentic_budget_enforced_flag
