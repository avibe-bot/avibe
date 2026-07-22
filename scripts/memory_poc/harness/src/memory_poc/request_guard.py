from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

MAX_BODY_BYTES = 64 * 1024


def validate_request(method: str, path: str, body: bytes) -> str | None:
    """Return a closed, payload-free rejection reason for non-MVP requests."""
    if method == "GET" and path == "/health":
        return None
    if method != "POST" or path not in {
        "/api/v1/memory/add",
        "/api/v1/memory/flush",
        "/api/v1/memory/search",
        "/api/v1/memory/get",
    }:
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
        "/api/v1/memory/search": _validate_search,
        "/api/v1/memory/get": _validate_get,
    }
    return validators[path](payload)


def _keys_are(payload: dict[str, Any], allowed: Iterable[str], required: Iterable[str]) -> bool:
    allowed_set = set(allowed)
    required_set = set(required)
    return required_set.issubset(payload) and set(payload).issubset(allowed_set)


def _valid_scope(payload: dict[str, Any]) -> bool:
    return payload.get("app_id") == "avibe" and payload.get("project_id") == "personal"


def _validate_add(payload: dict[str, Any]) -> str | None:
    if not _keys_are(payload, {"session_id", "app_id", "project_id", "messages"}, {"session_id", "app_id", "project_id", "messages"}):
        return "add_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("session_id"), str):
        return "add_scope_rejected"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 500:
        return "add_messages_rejected"
    for message in messages:
        if not isinstance(message, dict):
            return "add_message_rejected"
        if not _keys_are(
            message,
            {"sender_id", "sender_name", "role", "timestamp", "content"},
            {"sender_id", "role", "timestamp", "content"},
        ):
            return "add_message_rejected"
        if message.get("role") not in {"user", "assistant"}:
            return "add_message_rejected"
        if not isinstance(message.get("sender_id"), str) or not isinstance(message.get("timestamp"), int):
            return "add_message_rejected"
        if not isinstance(message.get("content"), str):
            return "add_non_text_rejected"
    return None


def _validate_flush(payload: dict[str, Any]) -> str | None:
    if not _keys_are(payload, {"session_id", "app_id", "project_id"}, {"session_id", "app_id", "project_id"}):
        return "flush_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("session_id"), str):
        return "flush_scope_rejected"
    return None


def _validate_search(payload: dict[str, Any]) -> str | None:
    allowed = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    required = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    if not _keys_are(payload, allowed, required):
        return "search_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("user_id"), str) or not isinstance(payload.get("query"), str):
        return "search_scope_rejected"
    if payload.get("method") not in {"keyword", "vector", "hybrid"}:
        return "search_method_rejected"
    if payload.get("enable_llm_rerank") is not False:
        return "search_rerank_rejected"
    return None


def _validate_get(payload: dict[str, Any]) -> str | None:
    allowed = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    required = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    if not _keys_are(payload, allowed, required):
        return "get_shape_rejected"
    if not _valid_scope(payload) or not isinstance(payload.get("user_id"), str):
        return "get_scope_rejected"
    if payload.get("memory_type") not in {"profile", "episode"}:
        return "get_memory_type_rejected"
    return None
