from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .constants import APP_ID, PROJECT_ID
from .errors import LaunchError


class EverOSClient:
    """Small UDS client for the public provider API used by the POC."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def health(self) -> None:
        self._request("GET", "/health", require_data=False)

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
        )

    def flush(self, *, session_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/memory/flush",
            {"session_id": session_id, "app_id": APP_ID, "project_id": PROJECT_ID},
        )

    def get(self, *, owner_id: str, memory_type: str) -> dict[str, Any]:
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
                "method": "keyword",
                "top_k": 8,
                "include_profile": True,
                "enable_llm_rerank": False,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        require_data: bool = True,
    ) -> dict[str, Any]:
        transport = httpx.HTTPTransport(uds=str(self.socket_path))
        try:
            with httpx.Client(
                transport=transport,
                base_url="http://memory-poc-uds",
                timeout=self.timeout_seconds,
                trust_env=False,
            ) as client:
                response = client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise LaunchError(f"provider_{method.lower()}_transport_failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise LaunchError(f"provider_{method.lower()}_status_{response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LaunchError(f"provider_{method.lower()}_invalid_json") from exc
        if not isinstance(body, dict):
            raise LaunchError(f"provider_{method.lower()}_invalid_envelope")
        if not require_data:
            return body
        data = body.get("data")
        if not isinstance(data, dict):
            raise LaunchError(f"provider_{method.lower()}_invalid_data")
        return data
