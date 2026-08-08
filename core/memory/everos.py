"""Private provider port, real EverOS adapter, and test fake for Memory."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    is_memory_error_code,
)
from core.memory.observations import (
    AddAck,
    FlushPreSubmission,
    FlushRejected,
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
class ProviderCapture:
    principal_id: str
    session_ref: str
    text: str
    provider_timestamp_ms: int
    project_ref: str
    attachments: tuple[CaptureAttachment, ...] = ()


class MemoryProviderFailure(RuntimeError):
    """A redaction-safe failure already classified by the provider adapter."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_processing_failed",
        *,
        retryable: bool = True,
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_processing_failed"
        )
        super().__init__(closed_error)
        self.error = closed_error
        self.retryable = bool(retryable)


class MemoryProviderPreSubmissionFailure(MemoryProviderFailure):
    """A write could not be submitted, so retrying cannot duplicate it."""

    def __init__(self, error: MemoryErrorCode = "memory_provider_timeout") -> None:
        super().__init__(error, retryable=True)


class MemoryProviderSystemFailure(MemoryProviderFailure):
    """The sidecar or its configured processing dependencies are unavailable."""

    def __init__(
        self,
        error: MemoryErrorCode = "memory_sidecar_unavailable",
    ) -> None:
        closed_error: MemoryErrorCode = (
            error if is_memory_error_code(error) else "memory_sidecar_unavailable"
        )
        super().__init__(closed_error, retryable=True)


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
        self._processing_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        """The owned UDS endpoint, retained for process/runtime coordination."""

        return self._socket_path

    async def add(self, capture: ProviderCapture) -> AddAck:
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
                "session_id": capture.session_ref,
                "app_id": _APP_ID,
                "project_id": capture.project_ref,
                "messages": [
                    {
                        "sender_id": capture.principal_id,
                        "role": "user",
                        "timestamp": capture.provider_timestamp_ms,
                        "content": content,
                    }
                ],
            },
            timeout_seconds=self._add_timeout_seconds,
        )
        if not 200 <= status_code < 300:
            logger.warning("EverOS add rejected status=%s", status_code)
            # A request the provider rejects on its own terms fails identically
            # however often it is replayed. Retrying one keeps a poison row
            # cycling the shared processing-fault breaker, which freezes capture
            # for every session, so it is sent straight to terminal instead.
            raise MemoryProviderFailure(
                "memory_processing_failed",
                retryable=not _deterministic_client_rejection(status_code, raw),
            )
        envelope = _optional_json_object(raw)
        data = envelope.get("data") if envelope is not None else None
        status = data.get("status") if isinstance(data, dict) else None
        if envelope is None:
            logger.warning("EverOS add returned 2xx with an unusable response body")
        elif status is not None and status not in {"accumulated", "extracted"}:
            logger.warning("EverOS add returned an unsupported status value")
        return AddAck(
            request_id=_bounded_opaque_string(envelope.get("request_id") if envelope else None),
            status=status if status in {"accumulated", "extracted"} else None,
        )

    async def flush(self, session_ref: str, project_id: str) -> FlushResult:
        """Trigger distillation and return a total provider outcome."""

        try:
            status_code, raw = await self._sidecar_write(
                "POST",
                "/api/v2/memory/flush",
                {
                    "session_id": session_ref,
                    "app_id": _APP_ID,
                    "project_id": project_id,
                },
                timeout_seconds=self._flush_timeout_seconds,
            )
        except MemoryProviderPreSubmissionFailure as failure:
            return FlushPreSubmission(
                reason="timeout"
                if failure.error == "memory_provider_timeout"
                else "transport"
            )
        except MemoryProviderSystemFailure:
            return FlushUnknown(reason="transport")
        except MemoryProviderFailure as failure:
            reason: Literal["timeout", "transport"] = (
                "timeout" if failure.error == "memory_provider_timeout" else "transport"
            )
            return FlushUnknown(reason=reason)

        envelope = _optional_json_object(raw)
        request_id = _bounded_opaque_string(envelope.get("request_id") if envelope else None)
        if 200 <= status_code < 300:
            data = envelope.get("data") if envelope is not None else None
            status = data.get("status") if isinstance(data, dict) else None
            if envelope is None:
                logger.warning("EverOS flush returned 2xx with an unusable response body")
            elif status is not None and status not in {"extracted", "no_extraction"}:
                logger.warning("EverOS flush returned an unsupported status value")
            return FlushSucceeded(
                request_id=request_id,
                status=status if status in {"extracted", "no_extraction"} else None,
            )
        error = envelope.get("error") if envelope is not None else None
        error_code = error.get("code") if isinstance(error, dict) else None
        return FlushRejected(
            request_id=request_id,
            error_code=_bounded_opaque_string(error_code),
            server_fault=status_code >= 500,
            retryable=not _deterministic_client_rejection(status_code, raw),
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
                    try:
                        raw = await _read_bounded_response(response)
                    except MemoryProviderFailure:
                        raw = None
                    status_code = response.status_code
        except httpx.ConnectTimeout as exc:
            logger.warning(
                "EverOS sidecar connect timeout route=%s latency_ms=%s",
                route,
                _elapsed_ms(started),
            )
            raise MemoryProviderPreSubmissionFailure() from exc
        except httpx.ConnectError as exc:
            logger.warning(
                "EverOS sidecar connection refused route=%s latency_ms=%s",
                route,
                _elapsed_ms(started),
            )
            raise MemoryProviderPreSubmissionFailure("memory_sidecar_unavailable") from exc
        except httpx.TimeoutException as exc:
            logger.warning("EverOS sidecar timeout route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderFailure("memory_provider_timeout") from exc
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("EverOS sidecar unavailable route=%s latency_ms=%s", route, _elapsed_ms(started))
            raise MemoryProviderSystemFailure() from exc
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
    ) -> tuple[MemoryItem, ...]:
        data = await self._search_data(principal_id, project_id, query, limit)
        return _map_search_items(data, principal_id=principal_id, limit=limit)

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]:
        data = await self._search_data(principal_id, project_id, _PROFILE_QUERY, 1)
        profile = _map_profile_item(data, principal_id=principal_id)
        # "Valid response, no profile payload" is exactly "zero items returned",
        # so it needs no state on this provider: one EverOSPort serves every
        # principal, and a field here is whichever concurrent read finished last.
        return () if profile is None else (profile,)

    async def health(self) -> bool:
        try:
            await self._sidecar_request("GET", "/health", None, require_json=False)
        except MemoryProviderFailure:
            return False
        return True

    async def recorder_health(self) -> dict[str, str | None]:
        """Return the closed recorder state projected by the sidecar health route."""
        try:
            payload = await self._sidecar_request(
                "GET", "/health", None, require_json=True
            )
        except MemoryProviderFailure:
            return dict(_RECORDER_HEALTH_FALLBACK)
        recorder = payload.get("recorder") if payload is not None else None
        if not isinstance(recorder, dict) or set(recorder) != {"state", "reason"}:
            return dict(_RECORDER_HEALTH_FALLBACK)
        state = recorder.get("state")
        reason = recorder.get("reason")
        valid = (
            (state == "active" and reason is None)
            or (
                state == "degraded"
                and reason in _RECORDER_HEALTH_REASONS
            )
            or (
                state == "disabled"
                and reason in {None, "writer_failures"}
            )
        )
        if not valid:
            return dict(_RECORDER_HEALTH_FALLBACK)
        return {"state": state, "reason": reason}

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
    ) -> dict[str, Any]:
        body = await self._sidecar_request(
            "POST",
            "/api/v2/memory/search",
            {
                "user_id": principal_id,
                "app_id": _APP_ID,
                "project_id": project_id,
                "query": query,
                "method": "hybrid",
                "top_k": limit,
                "include_profile": True,
                "enable_llm_rerank": False,
            },
            require_json=True,
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
                        raise MemoryProviderFailure("memory_processing_failed")
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


#: 4xx statuses that describe a passing condition rather than a request the
#: provider can never accept. Everything else in 4xx is deterministic, and the
#: default is deliberately the strict one because the two mistakes do not cost
#: the same: a wrongly retryable capture is replayed up to MAX_MESSAGE_ATTEMPTS
#: times, and every replay can re-open the shared processing-fault breaker, which
#: freezes capture for BREAKER_RETRY_SECONDS across every session; a wrongly
#: terminal one drops a single capture for a single session.
_TRANSIENT_CLIENT_STATUS_CODES = frozenset({408, 409, 423, 425, 429})


def _deterministic_client_rejection(status_code: int, raw: bytes | None = None) -> bool:
    """Whether a status means this exact request can never be accepted.

    EverOS 1.2.1 uses 422 both for deterministic DTO rejection and for a
    missing provider configuration. The latter can recover after settings are
    repaired, so its machine-readable envelope overrides the status taxonomy.

    408, 425 and 429 are temporary by definition; 409 and 423 describe a state
    a later attempt may find cleared, which is worth the bounded replay even
    though a conflict can also be permanent.
    """

    envelope = _optional_json_object(raw)
    error = envelope.get("error") if envelope is not None else None
    error_code = error.get("code") if isinstance(error, dict) else None
    if status_code == 422 and error_code == "PROVIDER_NOT_CONFIGURED":
        return False
    return 400 <= status_code < 500 and status_code not in _TRANSIENT_CLIENT_STATUS_CODES


async def _read_bounded_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
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


def _optional_json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and _is_bounded_json_value(value) else None


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
    async def add(self, capture: ProviderCapture) -> AddAck: ...

    async def flush(self, session_ref: str, project_id: str) -> FlushResult: ...

    async def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]: ...

    async def profile(self, principal_id: str, project_id: str) -> tuple[MemoryItem, ...]: ...

    async def health(self) -> bool: ...

    async def recorder_health(self) -> dict[str, str | None]: ...

    async def processing_healthy(self) -> bool: ...


@dataclass
class FakeMemoryProvider:
    """In-memory provider fake for Memory module and worker contract tests."""

    healthy: bool = True
    processing_healthy_flag: bool = True
    search_items: tuple[MemoryItem, ...] = ()
    profile_items: tuple[MemoryItem, ...] = ()
    captures: list[ProviderCapture] = field(default_factory=list)
    flushes: list[str] = field(default_factory=list)
    flush_projects: list[str] = field(default_factory=list)
    search_scopes: list[tuple[str, str]] = field(default_factory=list)
    profile_scopes: list[tuple[str, str]] = field(default_factory=list)
    ingest_failures: Deque[BaseException] = field(default_factory=deque)
    flush_results: Deque[FlushResult] = field(default_factory=deque)
    search_failure: BaseException | None = None
    profile_failure: BaseException | None = None
    health_failure: BaseException | None = None
    processing_health_failure: BaseException | None = None
    recorder_health_state: dict[str, str | None] = field(
        default_factory=lambda: {"state": "disabled", "reason": None}
    )

    async def add(self, capture: ProviderCapture) -> AddAck:
        if self.ingest_failures:
            raise self.ingest_failures.popleft()
        self.captures.append(capture)
        return AddAck(request_id=None, status="accumulated")

    async def flush(self, session_ref: str, project_id: str) -> FlushResult:
        self.flushes.append(session_ref)
        self.flush_projects.append(project_id)
        if self.flush_results:
            return self.flush_results.popleft()
        return FlushSucceeded(request_id=None, status="extracted")

    async def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]:
        self.search_scopes.append((principal_id, project_id))
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
        if self.processing_health_failure is not None:
            raise self.processing_health_failure
        return self.processing_healthy_flag
