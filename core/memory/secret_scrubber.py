"""Canonical secret/path redaction used by Memory persistence and sync artifacts."""

from __future__ import annotations

import importlib
import os
import re
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


REDACTED = "[REDACTED]"
LOCAL_PATH = "[LOCAL_PATH]"
PROVIDER_BASE_URL = "[PROVIDER_BASE_URL]"
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTHORIZATION_VALUE_RE = re.compile(r"(?im)(\b(?:proxy[-_ ]?)?authorization\s*[:=]\s*)[^\r\n]*")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|auth[-_ ]?token|refresh[-_ ]?token)\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_PREFIXED_KEY_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk|api)-[A-Za-z0-9_-]{8,}")
_FILE_URL_RE = re.compile(r"(?i)\bfile:///(?:[^\s\"'<>]|\\ )+")
_POSIX_PATH_RE = re.compile(r"(?<![:/\w])(?<!\[PROVIDER_BASE_URL\])/(?:[^\s\"'<>]|\\ )+")
_WINDOWS_PATH_RE = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>]+")
_SECRET_KEY_SUFFIXES = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "secretkey",
    "token",
)


def normalize_provider_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.rstrip("/"))
    except ValueError:
        return value.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return value.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, "", ""))


def scrub_text(value: str, *, base_urls: tuple[str, ...] = (), exact_values: tuple[str, ...] = ()) -> str:
    scrubbed = value
    for exact_value in sorted(set(exact_values), key=len, reverse=True):
        scrubbed = scrubbed.replace(exact_value, REDACTED)
    for base_url in sorted(base_urls, key=len, reverse=True):
        normalized = normalize_provider_base_url(base_url)
        if normalized:
            parts = urlsplit(normalized)
            if parts.scheme and parts.netloc:
                pattern = re.compile(
                    "(?i:" + re.escape(f"{parts.scheme}://{parts.netloc}") + ")" + re.escape(parts.path)
                )
                scrubbed = pattern.sub(PROVIDER_BASE_URL, scrubbed)
            else:
                scrubbed = scrubbed.replace(normalized, PROVIDER_BASE_URL)
    scrubbed = _AUTHORIZATION_VALUE_RE.sub(lambda match: match.group(1) + REDACTED, scrubbed)
    scrubbed = _BEARER_RE.sub("Bearer " + REDACTED, scrubbed)
    scrubbed = _LABELED_SECRET_RE.sub(lambda match: match.group(1) + REDACTED, scrubbed)
    scrubbed = _PREFIXED_KEY_RE.sub(REDACTED, scrubbed)
    scrubbed = _FILE_URL_RE.sub(LOCAL_PATH, scrubbed)
    scrubbed = _WINDOWS_PATH_RE.sub(LOCAL_PATH, scrubbed)
    return _POSIX_PATH_RE.sub(LOCAL_PATH, scrubbed)


def scrub_json(
    value: Any,
    *,
    base_urls: tuple[str, ...] = (),
    exact_values: tuple[str, ...] = (),
) -> Any:
    """Recursively scrub display JSON without depending on call recording."""

    if isinstance(value, str):
        return scrub_text(value, base_urls=base_urls, exact_values=exact_values)
    if isinstance(value, list):
        return [
            scrub_json(item, base_urls=base_urls, exact_values=exact_values)
            for item in value
        ]
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = scrub_text(
                str(key), base_urls=base_urls, exact_values=exact_values
            )
            scrubbed[clean_key] = (
                REDACTED
                if _is_secret_key(str(key))
                else scrub_json(
                    item,
                    base_urls=base_urls,
                    exact_values=exact_values,
                )
            )
        return scrubbed
    return value


def _is_secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized.endswith(_SECRET_KEY_SUFFIXES)


def scrub_from_environment(value: str) -> str:
    exact_values = tuple(
        os.environ[name]
        for name in (
            "EVEROS_LLM__API_KEY",
            "EVEROS_MULTIMODAL__API_KEY",
            "EVEROS_EMBEDDING__API_KEY",
        )
        if os.environ.get(name)
    )
    base_urls = tuple(
        os.environ[name]
        for name in (
            "EVEROS_LLM__BASE_URL",
            "EVEROS_MULTIMODAL__BASE_URL",
            "EVEROS_EMBEDDING__BASE_URL",
        )
        if os.environ.get(name)
    )
    return scrub_text(value, base_urls=base_urls, exact_values=exact_values)


def _persisted_error(value: str) -> str:
    try:
        return scrub_from_environment(value)
    except Exception:
        return REDACTED


def _patch(owner: Any, name: str, factory: Callable[[Callable[..., Any]], Callable[..., Any]]) -> None:
    current = getattr(owner, name)
    if getattr(current, "__avibe_memory_sync_scrubber__", False):
        return
    wrapped = factory(current)
    setattr(wrapped, "__avibe_memory_sync_scrubber__", True)
    setattr(owner, name, wrapped)


def install_error_scrubbers() -> None:
    run_record = importlib.import_module("everos.infra.ome._stores.run_record")
    md_change_state = importlib.import_module("everos.infra.persistence.sqlite.repos.md_change_state")

    def run_status(original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        async def wrapped(self: Any, run_id: str, status: Any, finished_at: Any, error: str | None) -> Any:
            clean = _persisted_error(error) if isinstance(error, str) else error
            return await original(self, run_id, status, finished_at, clean)

        return wrapped

    def mark_failed(original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        async def wrapped(self: Any, md_path: str, *, retryable: bool, error: str, new_retry_count: int) -> Any:
            return await original(
                self,
                md_path,
                retryable=retryable,
                error=_persisted_error(error),
                new_retry_count=new_retry_count,
            )

        return wrapped

    _patch(run_record.RunRecordStore, "_update_status", run_status)
    _patch(type(md_change_state.md_change_state_repo), "mark_failed", mark_failed)


_scrub = scrub_from_environment
