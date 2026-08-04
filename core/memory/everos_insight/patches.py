"""Pinned EverOS provider-call patches installed by the child sidecar."""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from .recorder import (
    _EMBEDDING_INPUT_COUNT,
    ProviderCallInput,
    ProviderKind,
    RecorderHandle,
    _scrub_text,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CallContext:
    stage: str
    kind: ProviderKind | None = None
    request_id: str | None = None
    strategy_name: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    memcell_id: str | None = None
    app_id: str | None = None
    project_id: str | None = None
    owner_id: str | None = None
    md_path: str | None = None
    entry_id: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None


_boundary_active: ContextVar[bool] = ContextVar(
    "avibe_memory_boundary_request", default=False
)
_current_context: ContextVar[_CallContext | None] = ContextVar(
    "avibe_memory_call_context", default=None
)
_active_handle: RecorderHandle | Any | None = None
_persisted_error_base_urls: tuple[str, ...] = ()
_persisted_error_exact_values: tuple[str, ...] = ()


def prepare_call_recorder(db_path: Path) -> RecorderHandle | None:
    """Install pinned-wheel patches and return an unstarted recorder handle."""
    global _active_handle

    previous = _active_handle
    try:
        handle = RecorderHandle(
            Path(db_path),
            provider_base_urls=_provider_base_urls(),
            exact_redaction_values=_provider_api_keys(),
        )
        _install_patches()
        _active_handle = handle
        return handle
    except Exception:
        _active_handle = previous
        logger.warning("memory_call_recorder_prepare_failed", exc_info=True)
        return None


def install_error_scrubbers() -> None:
    """Scrub provider credentials before EverOS persists diagnostic errors."""
    global _persisted_error_base_urls, _persisted_error_exact_values

    _persisted_error_base_urls = _provider_base_urls()
    _persisted_error_exact_values = tuple(
        sorted(set(_provider_api_keys()), key=len, reverse=True)
    )
    run_record = importlib.import_module("everos.infra.ome._stores.run_record")
    md_change_state = importlib.import_module(
        "everos.infra.persistence.sqlite.repos.md_change_state"
    )
    _patch_attribute(
        run_record.RunRecordStore,
        "_update_status",
        _run_record_status_wrapper,
    )
    _patch_attribute(
        type(md_change_state.md_change_state_repo),
        "mark_failed",
        _md_change_failure_wrapper,
    )


@contextmanager
def boundary_request() -> Iterator[None]:
    """Mark one validated add/flush request as eligible for capture."""
    token = _boundary_active.set(True)
    try:
        yield
    finally:
        _boundary_active.reset(token)


@contextmanager
def _call_context(**values: Any) -> Iterator[None]:
    token = _current_context.set(_CallContext(**values))
    try:
        yield
    finally:
        _current_context.reset(token)


def _install_patches() -> None:
    compat = importlib.import_module("everalgo.llm.providers.openai_compat")
    embedding = importlib.import_module(
        "everos.component.embedding.openai_provider"
    )
    user_memory = importlib.import_module(
        "everos.memory.extract.pipeline.user_memory"
    )
    parser = importlib.import_module("everos.component.parser")
    episode = importlib.import_module(
        "everos.memory.cascade.handlers.episode"
    )
    atomic_fact = importlib.import_module(
        "everos.memory.cascade.handlers.atomic_fact"
    )

    _patch_attribute(compat.OpenAICompatClient, "chat", _chat_wrapper)
    _patch_attribute(
        embedding.OpenAIEmbeddingProvider,
        "_embed_chunk",
        _embedding_wrapper,
    )
    _patch_attribute(
        user_memory, "_extract_with_retry", _episode_wrapper
    )
    _patch_attribute(parser, "aparse_file", _parser_wrapper)
    _patch_attribute(episode.EpisodeHandler, "_build_row", _cascade_wrapper)
    _patch_attribute(
        atomic_fact.AtomicFactHandler, "_build_row", _cascade_wrapper
    )


def _patch_attribute(owner: Any, name: str, factory: Callable[[Any], Any]) -> None:
    current = getattr(owner, name)
    if getattr(current, "__avibe_memory_call_patch__", False):
        return
    wrapped = factory(current)
    setattr(wrapped, "__avibe_memory_call_patch__", True)
    setattr(owner, name, wrapped)


def _persisted_error(error: str) -> str:
    try:
        return _scrub_text(
            error,
            base_urls=_persisted_error_base_urls,
            exact_values=_persisted_error_exact_values,
        )
    except Exception:
        return "[REDACTED]"


def _run_record_status_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(
        self: Any,
        run_id: str,
        status: Any,
        finished_at: Any,
        error: str | None,
    ) -> Any:
        clean_error = _persisted_error(error) if isinstance(error, str) else error
        return await original(self, run_id, status, finished_at, clean_error)

    return wraps(original)(wrapped)


def _md_change_failure_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(
        self: Any,
        md_path: str,
        *,
        retryable: bool,
        error: str,
        new_retry_count: int,
    ) -> Any:
        return await original(
            self,
            md_path,
            retryable=retryable,
            error=_persisted_error(error),
            new_retry_count=new_retry_count,
        )

    return wraps(original)(wrapped)


def _chat_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(
        self: Any,
        messages: list[Any],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Any = None,
        **extra: Any,
    ) -> Any:
        return await _record_call(
            lambda: original(
                self,
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                **extra,
            ),
            kind="llm",
            request_builder=lambda: (
                _chat_request(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    extra=extra,
                ),
                model or _safe_configured_model(self),
            ),
            response_builder=_llm_response,
        )

    return wraps(original)(wrapped)


def _embedding_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(self: Any, chunk: list[str]) -> Any:
        return await _record_call(
            lambda: original(self, chunk),
            kind="embedding",
            request_builder=lambda: _embedding_request(self, chunk),
            response_builder=_embedding_response,
        )

    return wraps(original)(wrapped)


def _episode_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(
        extractor: Any,
        cell: Any,
        prompt: str | None,
        memcell_id: str,
    ) -> Any:
        with _call_context(stage="episode_extract", memcell_id=memcell_id):
            return await original(extractor, cell, prompt, memcell_id)

    return wraps(original)(wrapped)


def _parser_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _call_context(stage="parse", kind="multimodal_llm"):
            return await original(*args, **kwargs)

    return wraps(original)(wrapped)


def _cascade_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(self: Any, **kwargs: Any) -> Any:
        entry = kwargs.get("entry")
        inline = getattr(getattr(entry, "structured", None), "inline", {})
        if not isinstance(inline, Mapping):
            inline = {}
        with _call_context(
            stage="cascade",
            app_id=kwargs.get("app_id", "default"),
            project_id=kwargs.get("project_id", "default"),
            owner_id=kwargs.get("owner_id"),
            md_path=kwargs.get("md_path"),
            entry_id=getattr(entry, "entry_id", None),
            parent_type=inline.get("parent_type") or "memcell",
            parent_id=inline.get("parent_id", ""),
        ):
            return await original(self, **kwargs)

    return wraps(original)(wrapped)


async def _record_call(
    invoke: Callable[[], Any],
    *,
    kind: ProviderKind,
    request_builder: Callable[[], tuple[dict[str, Any], str | None]],
    response_builder: Callable[[Any], tuple[dict[str, Any], dict[str, Any]]],
) -> Any:
    try:
        context = _resolved_context(kind)
    except BaseException:
        context = None
    if context is None or _active_handle is None:
        return await invoke()
    try:
        request, model = request_builder()
    except BaseException:
        request, model = {}, None

    started_at_ms = int(time.time() * 1000)
    started = time.monotonic()
    try:
        result = await invoke()
    except BaseException as exc:
        try:
            _submit(
                context,
                started_at_ms=started_at_ms,
                duration_ms=_duration_ms(started),
                request=request,
                model=model,
                status="error",
                error=_safe_error(exc),
            )
        except BaseException:
            pass
        raise

    try:
        response, metadata = response_builder(result)
        _submit(
            context,
            started_at_ms=started_at_ms,
            duration_ms=_duration_ms(started),
            request=request,
            response=response,
            model=metadata.get("model") or model,
            status="ok",
            finish_reason=metadata.get("finish_reason"),
            prompt_tokens=metadata.get("prompt_tokens"),
            completion_tokens=metadata.get("completion_tokens"),
        )
    except BaseException:
        pass
    return result


def _resolved_context(kind: ProviderKind) -> _CallContext | None:
    explicit = _current_context.get()
    strategy = _strategy_context()
    run_id = _string_or_none(strategy.get("run_id"))
    request_id = _request_id() if _boundary_active.get() else None

    if explicit is not None:
        context = replace(explicit, kind=explicit.kind or kind)
        if run_id is not None:
            return replace(
                context,
                strategy_name=_string_or_none(strategy.get("strategy_name")),
                run_id=run_id,
                attempt=_int_or_none(strategy.get("attempt")),
            )
        if request_id is not None:
            return replace(context, request_id=request_id)
        return context
    if run_id is not None:
        return _CallContext(
            stage="strategy",
            kind=kind,
            strategy_name=_string_or_none(strategy.get("strategy_name")),
            run_id=run_id,
            attempt=_int_or_none(strategy.get("attempt")),
        )
    if request_id is not None:
        return _CallContext(stage="boundary", kind=kind, request_id=request_id)
    return None


def _submit(
    context: _CallContext,
    *,
    started_at_ms: int,
    duration_ms: int,
    request: dict[str, Any],
    status: str,
    response: dict[str, Any] | None = None,
    model: str | None = None,
    error: str | None = None,
    finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    handle = _active_handle
    if handle is None:
        return
    try:
        handle.submit(
            ProviderCallInput(
                id=uuid4().hex,
                started_at_ms=started_at_ms,
                duration_ms=duration_ms,
                kind=context.kind,
                stage=context.stage,
                status=status,
                request=request,
                response=response,
                model=model,
                error=error,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                request_id=context.request_id,
                strategy_name=context.strategy_name,
                run_id=context.run_id,
                attempt=context.attempt,
                memcell_id=context.memcell_id,
                app_id=context.app_id,
                project_id=context.project_id,
                owner_id=context.owner_id,
                md_path=context.md_path,
                entry_id=context.entry_id,
                parent_type=context.parent_type,
                parent_id=context.parent_id,
            )
        )
    except Exception:
        logger.warning("memory_call_recorder_submit_failed", exc_info=True)


def _llm_response(result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    usage = getattr(result, "usage", None)
    return (
        {"content": getattr(result, "content", None)},
        {
            "model": _string_or_none(getattr(result, "model", None)),
            "finish_reason": _string_or_none(
                getattr(result, "finish_reason", None)
            ),
            "prompt_tokens": _int_or_none(
                getattr(usage, "prompt_tokens", None)
            ),
            "completion_tokens": _int_or_none(
                getattr(usage, "completion_tokens", None)
            ),
        },
    )


def _embedding_response(result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    vector_count = len(result) if isinstance(result, list) else 0
    dimension = (
        len(result[0])
        if vector_count and isinstance(result[0], (list, tuple))
        else 0
    )
    return {"vector_count": vector_count, "dimension": dimension}, {}


def _message_value(message: Any) -> Any:
    converted = _json_value(message)
    return None if converted is _OMIT else converted


_OMIT = object()


def _chat_request(
    messages: list[Any],
    *,
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: Any,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        selected_messages: list[Any]
        if len(messages) > 2:
            selected_messages = [
                messages[0],
                {"omitted_messages": len(messages) - 2},
                messages[-1],
            ]
        else:
            selected_messages = messages
        request: dict[str, Any] = {
            "messages": [_message_value(message) for message in selected_messages]
        }
        if model is not None:
            request["model"] = model
        if temperature is not None:
            request["temperature"] = temperature
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if response_format is not None:
            request["response_format"] = _schema_name(response_format)
        for key, value in extra.items():
            converted = _json_value(value)
            if converted is not _OMIT:
                request[key] = converted
        return request
    except BaseException:
        return {}


def _embedding_request(
    provider: Any, chunk: list[str]
) -> tuple[dict[str, Any], str | None]:
    try:
        model = _string_or_none(getattr(provider, "_model", None))
        return (
            {
                "model": model,
                "dimensions": getattr(provider, "_dimensions", None),
                "input_count": len(chunk),
                "inputs": list(chunk[:_EMBEDDING_INPUT_COUNT]),
                **(
                    {"omitted_inputs": len(chunk) - _EMBEDDING_INPUT_COUNT}
                    if len(chunk) > _EMBEDDING_INPUT_COUNT
                    else {}
                ),
            },
            model,
        )
    except BaseException:
        return {}, None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _OMIT
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json"))
        except TypeError:
            return _json_value(model_dump())
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            child = _json_value(item)
            if child is not _OMIT:
                converted[key] = child
        return converted
    if isinstance(value, (list, tuple)):
        return [child for item in value if (child := _json_value(item)) is not _OMIT]
    return _OMIT


def _schema_name(schema: Any) -> str:
    return getattr(schema, "__name__", type(schema).__name__)


def _safe_configured_model(client: Any) -> str | None:
    try:
        config = getattr(client, "_config", None)
        return _string_or_none(getattr(config, "model", None))
    except BaseException:
        return None


def _strategy_context() -> dict[str, Any]:
    try:
        contextvars = importlib.import_module("structlog.contextvars")
        values = contextvars.get_contextvars()
        return values if isinstance(values, dict) else {}
    except Exception:
        return {}


def _request_id() -> str | None:
    try:
        request = importlib.import_module("everos.core.context.request")
        return _string_or_none(request.get_request_id())
    except Exception:
        return None


def _provider_base_urls() -> tuple[str, ...]:
    import os

    return tuple(
        value
        for name in (
            "EVEROS_LLM__BASE_URL",
            "EVEROS_MULTIMODAL__BASE_URL",
            "EVEROS_EMBEDDING__BASE_URL",
        )
        if (value := os.environ.get(name))
    )


def _provider_api_keys() -> tuple[str, ...]:
    import os

    return tuple(
        value
        for name in (
            "EVEROS_LLM__API_KEY",
            "EVEROS_MULTIMODAL__API_KEY",
            "EVEROS_EMBEDDING__API_KEY",
        )
        if (value := os.environ.get(name))
    )


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _safe_error(error: BaseException) -> str:
    try:
        return str(error)
    except BaseException:
        return type(error).__name__


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
