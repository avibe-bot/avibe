from __future__ import annotations

import json

from memory_poc.request_guard import validate_request

OWNER_ID = "00000000-0000-4000-8000-000000000001"


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_health_is_the_only_allowed_get() -> None:
    assert validate_request("GET", "/health", b"", owner_id=OWNER_ID) is None
    assert validate_request("GET", "/metrics", b"", owner_id=OWNER_ID) == "route_not_allowed"


def test_text_only_add_shape_is_allowed() -> None:
    payload = {
        "session_id": "s1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [{"sender_id": OWNER_ID, "role": "user", "timestamp": 1, "content": "synthetic"}],
    }

    assert validate_request("POST", "/api/v1/memory/add", _body(payload), owner_id=OWNER_ID) is None


def test_multimodal_add_and_llm_rerank_are_rejected() -> None:
    add = {
        "session_id": "s1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [{"sender_id": OWNER_ID, "role": "user", "timestamp": 1, "content": [{"type": "image"}]}],
    }
    search = {
        "user_id": OWNER_ID,
        "app_id": "avibe",
        "project_id": "personal",
        "query": "synthetic",
        "method": "hybrid",
        "top_k": 8,
        "include_profile": True,
        "enable_llm_rerank": True,
    }

    assert validate_request("POST", "/api/v1/memory/add", _body(add), owner_id=OWNER_ID) == "add_non_text_rejected"
    assert validate_request("POST", "/api/v1/memory/search", _body(search), owner_id=OWNER_ID) == "search_rerank_rejected"


def test_non_hybrid_search_is_rejected() -> None:
    search = {
        "user_id": OWNER_ID,
        "app_id": "avibe",
        "project_id": "personal",
        "query": "synthetic",
        "method": "agentic",
        "top_k": 8,
        "include_profile": True,
        "enable_llm_rerank": False,
    }

    assert validate_request("POST", "/api/v1/memory/search", _body(search), owner_id=OWNER_ID) == "search_method_rejected"


def test_search_and_get_reject_non_mvp_pagination_options() -> None:
    search = {
        "user_id": OWNER_ID,
        "app_id": "avibe",
        "project_id": "personal",
        "query": "synthetic",
        "method": "hybrid",
        "top_k": 9,
        "include_profile": True,
        "enable_llm_rerank": False,
    }
    get = {
        "user_id": OWNER_ID,
        "app_id": "avibe",
        "project_id": "personal",
        "memory_type": "episode",
        "page": 2,
        "page_size": 20,
        "sort_by": "timestamp",
        "sort_order": "desc",
    }

    assert validate_request("POST", "/api/v1/memory/search", _body(search), owner_id=OWNER_ID) == "search_options_rejected"
    assert validate_request("POST", "/api/v1/memory/get", _body(get), owner_id=OWNER_ID) == "get_options_rejected"


def test_reads_and_user_messages_are_bound_to_the_synthetic_owner() -> None:
    search = {
        "user_id": "different-owner",
        "app_id": "avibe",
        "project_id": "personal",
        "query": "synthetic",
        "method": "hybrid",
        "top_k": 8,
        "include_profile": True,
        "enable_llm_rerank": False,
    }
    get = {
        "user_id": "different-owner",
        "app_id": "avibe",
        "project_id": "personal",
        "memory_type": "episode",
        "page": 1,
        "page_size": 20,
        "sort_by": "timestamp",
        "sort_order": "desc",
    }
    add = {
        "session_id": "s1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [{"sender_id": "different-owner", "role": "user", "timestamp": 1, "content": "synthetic"}],
    }

    assert validate_request("POST", "/api/v1/memory/search", _body(search), owner_id=OWNER_ID) == "search_owner_rejected"
    assert validate_request("POST", "/api/v1/memory/get", _body(get), owner_id=OWNER_ID) == "get_owner_rejected"
    assert validate_request("POST", "/api/v1/memory/add", _body(add), owner_id=OWNER_ID) == "add_owner_rejected"
