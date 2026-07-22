from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

MAX_BODY_BYTES = 64 * 1024


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_request(
    method: str,
    path: str,
    body: bytes,
    *,
    owner_id: str,
    phase: str = "unattributed",
) -> str | None:
    """Return a closed, payload-free rejection reason for non-MVP requests."""
    if not isinstance(owner_id, str) or not owner_id:
        return "fixed_owner_missing"
    if method == "GET" and path == "/health":
        return None
    phase_routes = {
        "ingestion": {"/api/v1/memory/add", "/api/v1/memory/flush"},
        "read": {"/api/v1/memory/search"},
        "research": {"/api/v1/memory/get", "/api/v1/memory/search"},
    }
    if method != "POST" or path not in phase_routes.get(phase, set()):
        return "route_not_allowed"
    if len(body) > MAX_BODY_BYTES:
        return "body_too_large"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "invalid_json"
    if not isinstance(payload, dict):
        return "invalid_shape"
    validators = {
        "/api/v1/memory/add": _validate_add,
        "/api/v1/memory/flush": _validate_flush,
        "/api/v1/memory/get": _validate_get,
    }
    if path == "/api/v1/memory/search":
        return _validate_search(payload, owner_id, allow_session_filter=phase == "research")
    return validators[path](payload, owner_id)


def _keys_are(payload: dict[str, Any], allowed: Iterable[str], required: Iterable[str]) -> bool:
    allowed_set = set(allowed)
    required_set = set(required)
    return required_set.issubset(payload) and set(payload).issubset(allowed_set)


def _valid_scope(payload: dict[str, Any]) -> bool:
    return payload.get("app_id") == "avibe" and payload.get("project_id") == "personal"


def _validate_add(payload: dict[str, Any], owner_id: str) -> str | None:
    if not _keys_are(payload, {"session_id", "app_id", "project_id", "messages"}, {"session_id", "app_id", "project_id", "messages"}):
        return "add_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("session_id"), str):
        return "add_scope_rejected"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 500:
        return "add_messages_rejected"
    user_message_seen = False
    for message in messages:
        if not isinstance(message, dict):
            return "add_message_rejected"
        if not _keys_are(
            message,
            {"sender_id", "role", "timestamp", "content"},
            {"sender_id", "role", "timestamp", "content"},
        ):
            return "add_message_rejected"
        if message.get("role") not in {"user", "assistant"}:
            return "add_message_rejected"
        if not isinstance(message.get("sender_id"), str) or not isinstance(message.get("timestamp"), int):
            return "add_message_rejected"
        if not isinstance(message.get("content"), str):
            return "add_non_text_rejected"
        if message["role"] == "user":
            if message["sender_id"] != owner_id:
                return "add_owner_rejected"
            user_message_seen = True
    if not user_message_seen:
        return "add_owner_rejected"
    return None


def _validate_flush(payload: dict[str, Any], _owner_id: str) -> str | None:
    if not _keys_are(payload, {"session_id", "app_id", "project_id"}, {"session_id", "app_id", "project_id"}):
        return "flush_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("session_id"), str):
        return "flush_scope_rejected"
    return None


def _validate_search(payload: dict[str, Any], owner_id: str, *, allow_session_filter: bool = False) -> str | None:
    allowed = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    if "filters" in payload and not allow_session_filter:
        return "search_filters_rejected"
    if allow_session_filter:
        allowed.add("filters")
    required = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    if not _keys_are(payload, allowed, required):
        return "search_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("user_id"), str) or not isinstance(payload.get("query"), str):
        return "search_scope_rejected"
    if payload["user_id"] != owner_id:
        return "search_owner_rejected"
    if payload.get("method") != "hybrid":
        return "search_method_rejected"
    if not _is_strict_int(payload.get("top_k")) or payload["top_k"] != 8 or payload.get("include_profile") is not True:
        return "search_options_rejected"
    if payload.get("enable_llm_rerank") is not False:
        return "search_rerank_rejected"
    if "filters" in payload:
        filters = payload["filters"]
        if not (
            allow_session_filter
            and isinstance(filters, dict)
            and set(filters) == {"session_id"}
            and isinstance(filters.get("session_id"), str)
            and filters["session_id"]
        ):
            return "search_filters_rejected"
    return None


def _validate_get(payload: dict[str, Any], owner_id: str) -> str | None:
    allowed = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    required = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    if not _keys_are(payload, allowed, required):
        return "get_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("user_id"), str):
        return "get_scope_rejected"
    if payload["user_id"] != owner_id:
        return "get_owner_rejected"
    if payload.get("memory_type") not in {"profile", "episode"}:
        return "get_memory_type_rejected"
    if (
        not _is_strict_int(payload.get("page"))
        or payload["page"] != 1
        or not _is_strict_int(payload.get("page_size"))
        or payload["page_size"] != 20
        or payload.get("sort_by") != "timestamp"
        or payload.get("sort_order") != "desc"
    ):
        return "get_options_rejected"
    return None
