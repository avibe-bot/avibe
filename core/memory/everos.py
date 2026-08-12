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

import httpx

from core.memory.types import (
    CaptureAttachment,
    MemoryErrorCode,
    MemoryItem,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    ProviderSessionRef,
    is_memory_error_code,
    MemoryPreflightDiagnostic,
)
from core.memory.observations import (
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
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ITEM_BYTES = 64 * 1024
_MAX_RESPONSE_DEPTH = 8
_MAX_RESPONSE_COLLECTION = 200
_SIDECAR_TIMEOUT_SECONDS = 20.0
_ADD_TIMEOUT_SECONDS = 30.0
_FLUSH_TIMEOUT_SECONDS = 300.0
_PROCESSING_TIMEOUT_SECONDS = 8.0
_PREFLIGHT_TIMEOUT_SECONDS = 5.0
_PREFLIGHT_RESPONSE_BYTES = 64 * 1024
_PROFILE_QUERY = "profile"
_MAX_PROFILE_TIMESTAMP_MS = 4_102_444_800_000
_RECORDER_HEALTH_FALLBACK = {"state": "degraded", "reason": "writer_failures"}
_RECORDER_HEALTH_REASONS = {
    "writer_failures",
    "serialization_failed",
    "call_log_corrupt",
}

ProviderAttachment = CaptureAttachment


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    """Allowlisted public EverOS health facts, with no Avibe readiness verdict."""

    status: Literal["ok"]
    version: str
    capabilities: dict[str, bool]
    disabled_features: tuple[str, ...]
    cascade: dict[str, object] | None
    recorder: dict[str, str | None]

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "version": self.version,
            "capabilities": dict(self.capabilities),
            "disabled_features": list(self.disabled_features),
            "cascade": dict(self.cascade) if self.cascade is not None else None,
            "recorder": dict(self.recorder),
        }


@dataclass(frozen=True)
class ProviderCapture:
    session_ref: ProviderSessionRef
    text: str
    provider_timestamp_ms: int
    attachments: tuple[CaptureAttachment, ...] = ()


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
    error: Literal["memory_embedding_unavailable", "memory_llm_unavailable"]
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
        processing_health_check: Callable[[], Awaitable[bool]] | None = None,
        sidecar_timeout_seconds: float = _SIDECAR_TIMEOUT_SECONDS,
        add_timeout_seconds: float = _ADD_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = _FLUSH_TIMEOUT_SECONDS,
        processing_timeout_seconds: float = _PROCESSING_TIMEOUT_SECONDS,
        preflight_call_recorder: Callable[..., None] | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._llm_base_url = _normalized_endpoint_url(llm_base_url)
        self._llm_model = _optional_string(llm_model)
        self._llm_api_key = _optional_string(llm_api_key)
        self._embedding_base_url = _normalized_endpoint_url(embedding_base_url)
        self._embedding_model = _optional_string(embedding_model)
        self._embedding_api_key = _optional_string(embedding_api_key)
        self._processing_health_check = processing_health_check
        self._sidecar_timeout_seconds = _positive_timeout(sidecar_timeout_seconds, _SIDECAR_TIMEOUT_SECONDS)
        self._add_timeout_seconds = _positive_timeout(add_timeout_seconds, _ADD_TIMEOUT_SECONDS)
        self._flush_timeout_seconds = _positive_timeout(flush_timeout_seconds, _FLUSH_TIMEOUT_SECONDS)
        self._processing_timeout_seconds = _positive_timeout(
            processing_timeout_seconds,
            _PROCESSING_TIMEOUT_SECONDS,
        )
        self._preflight_call_recorder = preflight_call_recorder
        self._processing_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        """The owned UDS endpoint, retained for process/runtime coordination."""

        return self._socket_path

    @property
    def agentic_budget_enforced(self) -> bool:
        """EverOS 1.2.3 exposes no model-call or token budget enforcement."""

        return False

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
        request_id = _bounded_opaque_string(envelope.get("request_id") if envelope else None)
        if 200 <= status_code < 300:
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
            request_id=request_id,
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
        """Return the HTTP verdict even when its bounded body is unusable."""

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
                        raw = await _read_bounded_response(response)
                    except MemoryProviderFailure:
                        raw = None
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
    ) -> tuple[MemoryItem, ...]:
        data = await self._search_data(
            principal_id,
            project_id,
            query,
            limit,
            method=method,
            include_profile=include_profile,
            session_ref=session_ref,
        )
        return _map_search_items(data, principal_id=principal_id, limit=limit)

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
        if not isinstance(data, dict) or not _is_bounded_json_value(data):
            raise MemoryProviderFailure("memory_provider_response_invalid")
        profile = _map_profile_item(data, principal_id=principal_id)
        # "Valid response, no profile payload" is exactly "zero items returned",
        # so it needs no state on this provider: one EverOSPort serves every
        # principal, and a field here is whichever concurrent read finished last.
        return () if profile is None else (profile,)

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

    async def recorder_health(self) -> dict[str, str | None]:
        """Return the closed recorder state projected by the sidecar health route."""
        try:
            snapshot = await self.health_snapshot()
        except MemoryProviderFailure:
            return dict(_RECORDER_HEALTH_FALLBACK)
        return dict(snapshot.recorder)

    async def processing_healthy(self) -> bool:
        """Probe both configured model endpoints with fixed synthetic requests.

        The worker may call this after ambiguous provider errors.  The lock keeps
        several queued rows from multiplying credential probes during an outage.
        """

        async with self._processing_lock:
            if self._processing_health_check is not None:
                try:
                    return bool(await self._processing_health_check())
                except Exception:
                    return False
            if not self._processing_configured():
                return False
            return await self._probe_processing_endpoint(
                base_url=self._llm_base_url,
                api_key=self._llm_api_key,
                path="chat/completions",
                payload={
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
                validator=_valid_chat_probe_response,
            ) and await self._probe_processing_endpoint(
                base_url=self._embedding_base_url,
                api_key=self._embedding_api_key,
                path="embeddings",
                payload={"model": self._embedding_model, "input": "memory health check"},
                validator=_valid_embedding_probe_response,
            )

    async def preflight(self) -> MemoryPreflightResult:
        """Run one bounded request for each configured processing endpoint."""
        checks = (
            ("llm", self._llm_base_url, self._llm_api_key, "chat/completions", {
                "model": self._llm_model, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 1, "temperature": 0,
            }, _valid_chat_probe_response),
            ("embedding", self._embedding_base_url, self._embedding_api_key, "embeddings", {
                "model": self._embedding_model, "input": "OK",
            }, _valid_embedding_probe_response),
        )
        first_failure = None
        for side, base_url, api_key, path, payload, validator in checks:
            try:
                failure = await asyncio.wait_for(
                    self._preflight_endpoint(side, base_url, api_key, path, payload, validator),
                    timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                error_name = "memory_llm_unavailable" if side == "llm" else "memory_embedding_unavailable"
                failure = MemoryPreflightFailure(
                    error_name,
                    MemoryPreflightDiagnostic(side, message="provider request timed out"),
                )
                self._record_preflight(
                    side,
                    payload,
                    None,
                    failure,
                    base_url=base_url,
                    api_key=api_key,
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
    ) -> dict[str, Any]:
        if method not in {"keyword", "vector", "hybrid", "agentic"}:
            raise MemoryProviderFailure("memory_invalid_input", retryable=False)
        if method == "agentic" and not self.agentic_budget_enforced:
            raise MemoryProviderFailure(
                "memory_capability_unavailable",
                retryable=False,
            )
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
        if not isinstance(data, dict) or not _is_bounded_json_value(data):
            raise MemoryProviderFailure("memory_provider_response_invalid")
        return data

    async def _sidecar_request(
        self,
        method: str,
        route: str,
        payload: dict[str, Any] | None,
        *,
        require_json: bool,
        capability_rejection: bool = False,
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://memory-sidecar",
                timeout=httpx.Timeout(self._sidecar_timeout_seconds, connect=3.0),
                trust_env=False,
            ) as client:
                async with client.stream(method, route, json=payload) as response:
                    if not 200 <= response.status_code < 300:
                        logger.warning(
                            "EverOS sidecar request failed route=%s status=%s latency_ms=%s",
                            route,
                            response.status_code,
                            _elapsed_ms(started),
                        )
                        raise MemoryProviderFailure(
                            "memory_capability_unavailable"
                            if capability_rejection and response.status_code == 422
                            else "memory_processing_failed"
                        )
                    if not require_json:
                        await _read_bounded_response(response)
                        logger.debug(
                            "EverOS sidecar request complete route=%s status=%s latency_ms=%s",
                            route,
                            response.status_code,
                            _elapsed_ms(started),
                        )
                        return None
                    raw = await _read_bounded_response(response)
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
                    raw = await _read_bounded_response(response)
            value = json.loads(raw)
        except (httpx.HTTPError, OSError, TypeError, ValueError, MemoryProviderFailure):
            logger.info("Memory processing probe unavailable endpoint=%s", path)
            return False
        return bool(validator(value))

    async def _preflight_endpoint(self, side, base_url, api_key, path, payload, validator):
        error_name = "memory_llm_unavailable" if side == "llm" else "memory_embedding_unavailable"
        diagnostic = MemoryPreflightDiagnostic(side)
        if not base_url or not api_key:
            failure = MemoryPreflightFailure(error_name, replace(diagnostic, message="endpoint is not configured"))
            self._record_preflight(side, payload, None, failure, base_url=base_url, api_key=api_key)
            return failure
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(_PREFLIGHT_TIMEOUT_SECONDS, connect=2.0), trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{base_url}/{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    raw = await _read_bounded_response(response, max_bytes=_PREFLIGHT_RESPONSE_BYTES)
                    status_code = response.status_code
            try:
                value = json.loads(raw[:4096]) if raw else None
            except (TypeError, ValueError):
                value = None
            if 200 <= status_code < 300 and validator(value):
                self._record_preflight(side, payload, value, None, base_url=base_url, api_key=api_key)
                return None
            code = None
            message = f"HTTP {status_code}"
            if isinstance(value, dict) and isinstance(value.get("error"), dict):
                error = value["error"]
                code = _bounded_opaque_string(error.get("code"))
                message = _bounded_preflight_message(error.get("message"), api_key=api_key) or f"HTTP {status_code}"
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, status_code, code, message))
            self._record_preflight(side, payload, value if isinstance(value, dict) else None, failure, base_url=base_url, api_key=api_key)
            return failure
        except httpx.TimeoutException:
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, message="provider request timed out"))
            self._record_preflight(side, payload, None, failure, base_url=base_url, api_key=api_key)
            return failure
        except MemoryProviderFailure:
            failure = MemoryPreflightFailure(
                error_name,
                MemoryPreflightDiagnostic(side, message="provider response exceeded the bounded limit"),
            )
            self._record_preflight(side, payload, None, failure, base_url=base_url, api_key=api_key)
            return failure
        except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
            failure = MemoryPreflightFailure(error_name, MemoryPreflightDiagnostic(side, message=_bounded_preflight_message(str(exc), api_key=api_key) or "provider unavailable"))
            self._record_preflight(side, payload, None, failure, base_url=base_url, api_key=api_key)
            return failure

    def _record_preflight(self, side, request, response, failure, *, base_url, api_key) -> None:
        if self._preflight_call_recorder is None:
            return
        try:
            self._preflight_call_recorder(
                side=side,
                request=request,
                response=response,
                failure=failure,
                base_url=base_url,
                api_key=api_key,
            )
        except Exception:
            logger.debug("memory preflight call recorder failed", exc_info=True)


def _bounded_preflight_message(value: object, *, api_key: str | None = None) -> str:
    if not isinstance(value, str):
        return ""
    message = " ".join(value.split())
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"https?://[^\s]+", "[URL]", message)
    message = re.sub(r"([?&](?:api[_-]?key|token|secret|password)=)[^&\s]+", r"\1[REDACTED]", message, flags=re.IGNORECASE)
    return message[:512]


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise MemoryProviderFailure("memory_provider_response_invalid")
        chunks.append(chunk)
    return b"".join(chunks)


def _map_search_items(
    data: dict[str, Any],
    *,
    principal_id: str,
    limit: int,
) -> tuple[MemoryItem, ...]:
    episodes = data.get("episodes", [])
    if not isinstance(episodes, list):
        raise MemoryProviderFailure("memory_provider_response_invalid")
    if len(episodes) > _MAX_RESPONSE_COLLECTION:
        raise MemoryProviderFailure("memory_provider_response_invalid")

    items: list[MemoryItem] = []
    for episode in episodes:
        if len(items) >= limit:
            break
        if not isinstance(episode, dict):
            continue
        if episode.get("user_id") != principal_id:
            continue
        text = _episode_text(episode)
        if text is not None:
            items.append(MemoryItem(kind="episode", text=text, date=_record_date(episode)))
        if len(items) >= limit:
            break
        facts = episode.get("atomic_facts", [])
        if facts is None:
            facts = []
        if not isinstance(facts, list) or len(facts) > _MAX_RESPONSE_COLLECTION:
            raise MemoryProviderFailure("memory_provider_response_invalid")
        for fact in facts:
            if len(items) >= limit:
                break
            if not isinstance(fact, dict):
                continue
            text = _safe_text(fact.get("content"))
            if text is not None:
                items.append(MemoryItem(kind="fact", text=text, date=_record_date(fact, episode)))
    return tuple(items)


def _map_profile_item(data: dict[str, Any], *, principal_id: str) -> MemoryItem | None:
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list) or len(profiles) > _MAX_RESPONSE_COLLECTION:
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


def _structured_explicit_info(value: dict[str, Any]) -> tuple[MemoryProfileExplicitInfo, ...]:
    if "explicit_info" not in value:
        return ()
    entries = value["explicit_info"]
    if not isinstance(entries, list) or len(entries) > _MAX_RESPONSE_COLLECTION:
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
    if not isinstance(entries, list) or len(entries) > _MAX_RESPONSE_COLLECTION:
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
    if not isinstance(value, (dict, list)) or not _is_bounded_json_value(value):
        return None
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return _safe_text(rendered)


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text.encode("utf-8")) > _MAX_ITEM_BYTES:
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


def _is_bounded_json_value(value: Any, *, depth: int = 0) -> bool:
    if depth > _MAX_RESPONSE_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool)):
        return not isinstance(value, str) or len(value.encode("utf-8")) <= _MAX_ITEM_BYTES
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= _MAX_RESPONSE_COLLECTION and all(
            _is_bounded_json_value(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= _MAX_RESPONSE_COLLECTION and all(
            isinstance(key, str)
            and len(key.encode("utf-8")) <= 128
            and _is_bounded_json_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _valid_chat_probe_response(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, str) and bool(content.strip())


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
        and len(vector) <= _MAX_RESPONSE_COLLECTION * 1000
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in vector)
    )


def _optional_string(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _bounded_opaque_string(value: object, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _strict_receipt_id(value: object, *, max_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value.encode("utf-8")) <= max_bytes else None


def _optional_json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and _is_bounded_json_value(value) else None


def _provider_health_snapshot(payload: dict[str, Any] | None) -> ProviderHealthSnapshot | None:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    version = payload.get("version")
    if not _safe_health_token(version, max_bytes=64, allow_dot=True):
        return None
    capabilities = payload.get("capabilities")
    capability_keys = {"llm", "embed", "rerank", "multimodal_llm", "parser"}
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != capability_keys
        or any(type(capabilities[key]) is not bool for key in capability_keys)
    ):
        return None
    disabled = payload.get("disabled_features")
    if (
        not isinstance(disabled, list)
        or len(disabled) > 32
        or any(not _safe_health_token(item, max_bytes=64) for item in disabled)
    ):
        return None
    recorder = _project_recorder_health(payload.get("recorder"))
    if recorder is None:
        return None
    cascade = _project_cascade_health(payload.get("cascade"))
    if payload.get("cascade") is not None and cascade is None:
        return None
    return ProviderHealthSnapshot(
        status="ok",
        version=version,
        capabilities={key: capabilities[key] for key in sorted(capability_keys)},
        disabled_features=tuple(disabled),
        cascade=cascade,
        recorder=recorder,
    )


def _project_recorder_health(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict) or set(value) != {"state", "reason"}:
        return None
    state = value.get("state")
    reason = value.get("reason")
    valid = (
        (state == "active" and reason is None)
        or (state == "degraded" and reason in _RECORDER_HEALTH_REASONS)
        or (state == "disabled" and reason in {None, "writer_failures"})
    )
    return {"state": state, "reason": reason} if valid else None


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


def _positive_timeout(value: float, fallback: float) -> float:
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
    ) -> tuple[MemoryItem, ...]: ...

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]: ...

    async def health(self) -> bool: ...

    async def health_snapshot(self) -> ProviderHealthSnapshot: ...

    async def recorder_health(self) -> dict[str, str | None]: ...

    async def processing_healthy(self) -> bool: ...

    @property
    def agentic_budget_enforced(self) -> bool: ...


@dataclass
class FakeMemoryProvider:
    """In-memory provider fake for Memory module and worker contract tests."""

    healthy: bool = True
    processing_healthy_flag: bool = True
    search_items: tuple[MemoryItem, ...] = ()
    profile_items: tuple[MemoryItem, ...] = ()
    captures: list[ProviderCapture] = field(default_factory=list)
    flushes: list[ProviderSessionRef] = field(default_factory=list)
    search_scopes: list[tuple[str, str]] = field(default_factory=list)
    profile_scopes: list[tuple[str, str]] = field(default_factory=list)
    search_policies: list[
        tuple[str, bool, ProviderSessionRef | None]
    ] = field(default_factory=list)
    ingest_failures: Deque[BaseException] = field(default_factory=deque)
    add_results: Deque[AddResult] = field(default_factory=deque)
    flush_results: Deque[FlushResult] = field(default_factory=deque)
    search_failure: BaseException | None = None
    profile_failure: BaseException | None = None
    health_failure: BaseException | None = None
    processing_health_failure: BaseException | None = None
    recorder_health_state: dict[str, str | None] = field(
        default_factory=lambda: {"state": "disabled", "reason": None}
    )
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
            recorder={"state": "disabled", "reason": None},
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
    ) -> tuple[MemoryItem, ...]:
        self.search_scopes.append((principal_id, project_id))
        self.search_policies.append((method, include_profile, session_ref))
        del query, limit
        if self.search_failure is not None:
            raise self.search_failure
        return self.search_items

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]:
        self.profile_scopes.append((principal_id, project_id))
        if self.profile_failure is not None:
            raise self.profile_failure
        return self.profile_items

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

    async def recorder_health(self) -> dict[str, str | None]:
        return dict(self.recorder_health_state)

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
        return False
