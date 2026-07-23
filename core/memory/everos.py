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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Deque, Literal, Protocol, runtime_checkable

import httpx

from core.memory.types import MemoryErrorCode, MemoryItem, is_memory_error_code
from core.memory.observations import (
    AddAck,
    FlushRejected,
    FlushResult,
    FlushSucceeded,
    FlushUnknown,
)


logger = logging.getLogger(__name__)

_APP_ID = "avibe"
_PROJECT_ID = "personal"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ITEM_BYTES = 64 * 1024
_MAX_RESPONSE_DEPTH = 8
_MAX_RESPONSE_COLLECTION = 200
_SIDECAR_TIMEOUT_SECONDS = 20.0
_ADD_TIMEOUT_SECONDS = 30.0
_FLUSH_TIMEOUT_SECONDS = 300.0
_PROCESSING_TIMEOUT_SECONDS = 8.0
_PROFILE_QUERY = "profile"


@dataclass(frozen=True)
class ProviderCapture:
    principal_id: str
    session_ref: str
    text: str
    provider_timestamp_ms: int


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
        self._profile_empty_warning = False

    @property
    def socket_path(self) -> Path:
        """The owned UDS endpoint, retained for process/runtime coordination."""

        return self._socket_path

    @property
    def profile_empty_warning(self) -> bool:
        """Whether the latest profile read was valid but had no profile payload."""

        return self._profile_empty_warning

    async def add(self, capture: ProviderCapture) -> AddAck:
        """Durably hand one capture to EverOS and return its acknowledgement."""

        status_code, raw = await self._sidecar_write(
            "POST",
            "/api/v1/memory/add",
            {
                "session_id": capture.session_ref,
                "app_id": _APP_ID,
                "project_id": _PROJECT_ID,
                "messages": [
                    {
                        "sender_id": capture.principal_id,
                        "role": "user",
                        "timestamp": capture.provider_timestamp_ms,
                        "content": capture.text,
                    }
                ],
            },
            timeout_seconds=self._add_timeout_seconds,
        )
        if not 200 <= status_code < 300:
            logger.warning("EverOS add rejected status=%s", status_code)
            raise MemoryProviderFailure("memory_processing_failed")
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

    async def flush(self, session_ref: str) -> FlushResult:
        """Trigger distillation and return a total provider outcome."""

        try:
            status_code, raw = await self._sidecar_write(
                "POST",
                "/api/v1/memory/flush",
                {
                    "session_id": session_ref,
                    "app_id": _APP_ID,
                    "project_id": _PROJECT_ID,
                },
                timeout_seconds=self._flush_timeout_seconds,
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
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]:
        data = await self._search_data(principal_id, query, limit)
        return _map_search_items(data, principal_id=principal_id, limit=limit)

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]:
        data = await self._search_data(principal_id, _PROFILE_QUERY, 1)
        profile = _map_profile_item(data, principal_id=principal_id)
        self._profile_empty_warning = profile is None
        return () if profile is None else (profile,)

    async def health(self) -> bool:
        try:
            await self._sidecar_request("GET", "/health", None, require_json=False)
        except MemoryProviderFailure:
            return False
        return True

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

    async def _search_data(self, principal_id: str, query: str, limit: int) -> dict[str, Any]:
        body = await self._sidecar_request(
            "POST",
            "/api/v1/memory/search",
            {
                "user_id": principal_id,
                "app_id": _APP_ID,
                "project_id": _PROJECT_ID,
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
        text = _canonical_profile_text(profile.get("profile_data"))
        if text is not None:
            return MemoryItem(kind="profile", text=text, date=_record_date(profile))
    return None


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
                    return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
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

    async def flush(self, session_ref: str) -> FlushResult: ...

    async def search(
        self,
        principal_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]: ...

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]: ...

    async def health(self) -> bool: ...

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
    ingest_failures: Deque[BaseException] = field(default_factory=deque)
    flush_results: Deque[FlushResult] = field(default_factory=deque)
    search_failure: BaseException | None = None
    profile_failure: BaseException | None = None
    health_failure: BaseException | None = None
    processing_health_failure: BaseException | None = None

    async def add(self, capture: ProviderCapture) -> AddAck:
        if self.ingest_failures:
            raise self.ingest_failures.popleft()
        self.captures.append(capture)
        return AddAck(request_id=None, status="accumulated")

    async def flush(self, session_ref: str) -> FlushResult:
        self.flushes.append(session_ref)
        if self.flush_results:
            return self.flush_results.popleft()
        return FlushSucceeded(request_id=None, status="extracted")

    async def search(
        self,
        principal_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]:
        del principal_id, query, limit
        if self.search_failure is not None:
            raise self.search_failure
        return self.search_items

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]:
        del principal_id
        if self.profile_failure is not None:
            raise self.profile_failure
        return self.profile_items

    async def health(self) -> bool:
        if self.health_failure is not None:
            raise self.health_failure
        return self.healthy

    async def processing_healthy(self) -> bool:
        """Whether the configured processing (LLM/embedding) endpoints are reachable.

        Distinct from sidecar ``health``: the sidecar process can answer /health
        while its configured model endpoint is down. The disambiguation between a
        system outage and a poison row depends on this. The fake returns a flag; the
        real EverOS adapter (Slice 2) performs bounded authenticated LLM+embedding
        probes.
        """
        if self.processing_health_failure is not None:
            raise self.processing_health_failure
        return self.processing_healthy_flag
