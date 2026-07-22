from __future__ import annotations

import json

from memory_poc.request_guard import validate_request


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_health_is_the_only_allowed_get() -> None:
    assert validate_request("GET", "/health", b"") is None
    assert validate_request("GET", "/metrics", b"") == "route_not_allowed"


def test_text_only_add_shape_is_allowed() -> None:
    payload = {
        "session_id": "s1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [{"sender_id": "owner", "role": "user", "timestamp": 1, "content": "synthetic"}],
    }

    assert validate_request("POST", "/api/v1/memory/add", _body(payload)) is None


def test_multimodal_add_and_llm_rerank_are_rejected() -> None:
    add = {
        "session_id": "s1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [{"sender_id": "owner", "role": "user", "timestamp": 1, "content": [{"type": "image"}]}],
    }
    search = {
        "user_id": "owner",
        "app_id": "avibe",
        "project_id": "personal",
        "query": "synthetic",
        "method": "hybrid",
        "top_k": 8,
        "include_profile": True,
        "enable_llm_rerank": True,
    }

    assert validate_request("POST", "/api/v1/memory/add", _body(add)) == "add_non_text_rejected"
    assert validate_request("POST", "/api/v1/memory/search", _body(search)) == "search_rerank_rejected"
