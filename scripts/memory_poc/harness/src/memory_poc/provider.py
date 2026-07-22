from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .constants import APP_ID, PROJECT_ID
from .errors import LaunchError

_SAFE_SCHEMA_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
_MAX_SCHEMA_PATHS = 64
_MAX_SCHEMA_DEPTH = 6


@dataclass(frozen=True)
class HttpShape:
    phase: str
    method: str
    route: str
    status_code: int
    request_keys: tuple[str, ...]
    response_keys: tuple[str, ...]
    data_keys: tuple[str, ...]
    response_schema_paths: tuple[str, ...]
    closed_code: int | str | None


class EverOSClient:
    """Small UDS client for the public provider API used by the POC."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = 30.0,
        safety_check: Callable[[], None] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self._safety_check = safety_check
        self._observations: list[HttpShape] = []

    @property
    def observed_http_shapes(self) -> tuple[HttpShape, ...]:
        return tuple(self._observations)

    def health(self) -> None:
        self._request("GET", "/health", require_data=False, phase="health")

    def add(self, *, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/add",
            {
                "session_id": session_id,
                "app_id": APP_ID,
                "project_id": PROJECT_ID,
                "messages": messages,
            },
            phase="ingestion",
        )

    def flush(self, *, session_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/flush",
            {"session_id": session_id, "app_id": APP_ID, "project_id": PROJECT_ID},
            phase="ingestion",
        )

    def research_diagnostic_get(self, *, owner_id: str, memory_type: str) -> dict[str, Any]:
        """Use `/get` only for isolated research diagnostics, never MVP reads."""
        return self._request(
            "POST",
            "/api/v1/memory/get",
            {
                "user_id": owner_id,
                "app_id": APP_ID,
                "project_id": PROJECT_ID,
                "memory_type": memory_type,
                "page": 1,
                "page_size": 20,
                "sort_by": "timestamp",
                "sort_order": "desc",
            },
            phase="research",
        )

    def search(self, *, owner_id: str, query: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/search",
            {
                "user_id": owner_id,
                "app_id": APP_ID,
                "project_id": PROJECT_ID,
                "query": query,
                "method": "hybrid",
                "top_k": 8,
                "include_profile": True,
                "enable_llm_rerank": False,
            },
            phase="read",
        )

    def research_buffer(self, *, owner_id: str, session_id: str) -> dict[str, Any]:
        """Inspect an in-flight session through public ``/search`` only.

        This is a POC-only diagnostic. Production retrieval continues to use
        :meth:`search`, whose request shape does not include filters.
        """
        return self._request(
            "POST",
            "/api/v1/memory/search",
            {
                "user_id": owner_id,
                "app_id": APP_ID,
                "project_id": PROJECT_ID,
                "query": "memory-poc-buffer-observation",
                "method": "hybrid",
                "top_k": 8,
                "include_profile": True,
                "enable_llm_rerank": False,
                "filters": {"session_id": session_id},
            },
            phase="research",
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        require_data: bool = True,
        phase: str,
    ) -> dict[str, Any]:
        if self._safety_check is not None:
            self._safety_check()
        transport = httpx.HTTPTransport(uds=str(self.socket_path))
        try:
            with httpx.Client(
                transport=transport,
                base_url="http://memory-poc-uds",
                timeout=self.timeout_seconds,
                trust_env=False,
            ) as client:
                response = client.request(method, path, json=payload, headers={"X-Memory-Poc-Phase": phase})
        except httpx.HTTPError as exc:
            raise LaunchError(f"provider_{method.lower()}_transport_failed") from exc
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            pass
        self._observations.append(
            HttpShape(
                phase=phase,
                method=method,
                route=path,
                status_code=response.status_code,
                request_keys=_mapping_keys(payload),
                response_keys=_mapping_keys(body),
                data_keys=_mapping_keys(body.get("data")) if isinstance(body, dict) else (),
                response_schema_paths=_schema_paths(body),
                closed_code=_closed_code(body),
            )
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise LaunchError(f"provider_{method.lower()}_status_{response.status_code}")
        if not isinstance(body, dict):
            raise LaunchError(f"provider_{method.lower()}_invalid_envelope")
        if self._safety_check is not None:
            self._safety_check()
        if not require_data:
            return body
        data = body.get("data")
        if not isinstance(data, dict):
            raise LaunchError(f"provider_{method.lower()}_invalid_data")
        return data


def _mapping_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted(key for key in value if isinstance(key, str) and _safe_schema_key(key)))


def _safe_schema_key(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(character in _SAFE_SCHEMA_KEY for character in value)


def _schema_paths(value: Any) -> tuple[str, ...]:
    """Capture a bounded value-free response signature for public API evidence."""
    paths: set[str] = set()

    def visit(item: Any, path: str, depth: int) -> None:
        if isinstance(item, dict):
            kind = "object"
        elif isinstance(item, list):
            kind = "array"
        elif isinstance(item, str):
            kind = "string"
        elif isinstance(item, bool):
            kind = "boolean"
        elif item is None:
            kind = "null"
        elif isinstance(item, (int, float)):
            kind = "number"
        else:
            kind = "other"
        if path:
            paths.add(f"{path}:{kind}")
        if depth >= _MAX_SCHEMA_DEPTH or len(paths) >= _MAX_SCHEMA_PATHS:
            return
        if isinstance(item, dict):
            for key in sorted(item):
                if isinstance(key, str) and _safe_schema_key(key):
                    visit(item[key], f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(item, list):
            for child in item[:3]:
                visit(child, f"{path}[]", depth + 1)

    visit(value, "", 0)
    return tuple(sorted(paths)[:_MAX_SCHEMA_PATHS])


def _closed_code(value: Any) -> int | str | None:
    if not isinstance(value, dict):
        return None
    candidates = [value]
    nested_error = value.get("error")
    if isinstance(nested_error, dict):
        candidates.append(nested_error)
    for candidate_map in candidates:
        for key in ("code", "error_code"):
            candidate = candidate_map.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and -999_999 <= candidate <= 999_999:
                return candidate
            if isinstance(candidate, str) and _safe_schema_key(candidate):
                return candidate
    return None
