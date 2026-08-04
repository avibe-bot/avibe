from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory.everos_insight import patches


class _Handle:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def submit(self, call: object) -> None:
        self.calls.append(call)


def test_chat_patch_preserves_result_and_captures_boundary_call(monkeypatch) -> None:
    result = SimpleNamespace(
        content="answer",
        model="provider-model",
        finish_reason="stop",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
    )

    async def chat(_self, messages, **_kwargs):
        assert messages[0].content == "hello"
        return result

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "req-1")
    wrapped = patches._chat_wrapper(chat)
    message = SimpleNamespace(
        content="hello",
        model_dump=lambda **_kwargs: {"role": "user", "content": "hello"},
    )

    with patches.boundary_request():
        captured = asyncio.run(wrapped(SimpleNamespace(), [message], model="requested-model"))

    assert captured is result
    assert len(handle.calls) == 1
    call = handle.calls[0]
    assert call.kind == "llm"
    assert call.stage == "boundary"
    assert call.request_id == "req-1"
    assert call.model == "provider-model"
    assert call.request == {
        "messages": [{"role": "user", "content": "hello"}],
        "model": "requested-model",
    }
    assert call.response == {"content": "answer"}
    assert call.prompt_tokens == 12
    assert call.completion_tokens == 4


def test_chat_patch_preserves_exception_identity(monkeypatch) -> None:
    failure = RuntimeError("provider failed")

    async def chat(_self, _messages, **_kwargs):
        raise failure

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "req-2")
    wrapped = patches._chat_wrapper(chat)

    with patches.boundary_request(), pytest.raises(RuntimeError) as raised:
        asyncio.run(wrapped(SimpleNamespace(), [], model="m"))

    assert raised.value is failure
    assert len(handle.calls) == 1
    assert handle.calls[0].status == "error"


def test_chat_patch_cannot_replace_a_successful_provider_result(monkeypatch) -> None:
    class _Result:
        @property
        def usage(self):
            raise RuntimeError("diagnostic serialization failed")

    result = _Result()

    async def chat(_self, _messages, **_kwargs):
        return result

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "req-safe")

    with patches.boundary_request():
        captured = asyncio.run(patches._chat_wrapper(chat)(SimpleNamespace(), []))

    assert captured is result
    assert handle.calls == []


def test_specific_stage_wins_over_strategy_and_boundary(monkeypatch) -> None:
    async def chat(_self, _messages, **_kwargs):
        return SimpleNamespace(content="ok", model="m", finish_reason=None, usage=None)

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "req-3")
    monkeypatch.setattr(
        patches,
        "_strategy_context",
        lambda: {"strategy_name": "reflect", "run_id": "run-1", "attempt": 2},
    )
    wrapped = patches._chat_wrapper(chat)

    with patches.boundary_request(), patches._call_context(
        stage="parse", kind="multimodal_llm"
    ):
        asyncio.run(wrapped(SimpleNamespace(), []))

    call = handle.calls[0]
    assert call.stage == "parse"
    assert call.kind == "multimodal_llm"
    assert call.request_id is None
    assert call.run_id == "run-1"
    assert call.strategy_name == "reflect"


def test_strategy_capture_precedes_boundary_and_unscoped_calls_are_ignored(monkeypatch) -> None:
    async def embed(_self, chunk):
        return [[1.0, 2.0] for _ in chunk]

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "req-4")
    wrapped = patches._embedding_wrapper(embed)
    provider = SimpleNamespace(_model="embed", _dimensions=256)

    monkeypatch.setattr(
        patches,
        "_strategy_context",
        lambda: {"strategy_name": "reflect", "run_id": "run-2", "attempt": 1},
    )
    with patches.boundary_request():
        result = asyncio.run(wrapped(provider, ["one", "two"]))

    assert result == [[1.0, 2.0], [1.0, 2.0]]
    call = handle.calls[0]
    assert call.stage == "strategy"
    assert call.strategy_name == "reflect"
    assert call.run_id == "run-2"
    assert call.attempt == 1
    assert call.request == {
        "model": "embed",
        "dimensions": 256,
        "input_count": 2,
        "inputs": ["one", "two"],
    }
    assert call.response == {"vector_count": 2, "dimension": 2}

    monkeypatch.setattr(patches, "_strategy_context", lambda: {})
    asyncio.run(wrapped(provider, ["ignored"]))
    assert len(handle.calls) == 1

    with patches._call_context(stage="cascade"):
        asyncio.run(wrapped(provider, ["cascade input"]))
    assert handle.calls[-1].stage == "cascade"
    assert handle.calls[-1].kind == "embedding"


def test_stage_binding_wrappers_capture_episode_and_cascade_fields(monkeypatch) -> None:
    seen: list[object] = []

    async def episode(*_args, **_kwargs):
        seen.append(patches._current_context.get())
        return "episode"

    async def cascade(_self, **_kwargs):
        seen.append(patches._current_context.get())
        return "row"

    episode_wrapped = patches._episode_wrapper(episode)
    cascade_wrapped = patches._cascade_wrapper(cascade)
    entry = SimpleNamespace(
        entry_id="entry-1",
        structured=SimpleNamespace(
            inline={"parent_type": "memcell", "parent_id": "mem-1"}
        ),
    )

    assert asyncio.run(episode_wrapped(None, None, None, "mem-1")) == "episode"
    assert (
        asyncio.run(
            cascade_wrapped(
                None,
                owner_id="u-1",
                owner_type="user",
                app_id="avibe",
                project_id="p-1",
                md_path="users/u-1/episode.md",
                entry=entry,
            )
        )
        == "row"
    )

    assert seen[0].stage == "episode_extract"
    assert seen[0].memcell_id == "mem-1"
    assert seen[1].stage == "cascade"
    assert seen[1].owner_id == "u-1"
    assert seen[1].entry_id == "entry-1"
    assert seen[1].parent_type == "memcell"
    assert seen[1].parent_id == "mem-1"


def test_prepare_call_recorder_is_diagnostic_only_on_patch_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        patches, "_install_patches", lambda: (_ for _ in ()).throw(RuntimeError("bad wheel"))
    )

    assert patches.prepare_call_recorder(tmp_path / "call-log.db") is None


def test_pinned_everos_patch_contract(tmp_path: Path) -> None:
    required = os.environ.get("AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT") == "1"
    if importlib.util.find_spec("everos") is None:
        if required:
            pytest.fail("managed EverOS runtime is required for this contract")
        pytest.skip("managed EverOS runtime is not installed")

    from everalgo.llm.providers.openai_compat import OpenAICompatClient
    from everos.component import parser
    from everos.component.embedding.openai_provider import OpenAIEmbeddingProvider
    from everos.memory.cascade.handlers.atomic_fact import AtomicFactHandler
    from everos.memory.cascade.handlers.episode import EpisodeHandler
    from everos.memory.extract.pipeline import user_memory

    assert inspect.iscoroutinefunction(OpenAICompatClient.chat)
    assert inspect.iscoroutinefunction(OpenAIEmbeddingProvider._embed_chunk)
    assert inspect.iscoroutinefunction(parser.aparse_file)
    assert inspect.iscoroutinefunction(user_memory._extract_with_retry)
    assert inspect.iscoroutinefunction(EpisodeHandler._build_row)
    assert inspect.iscoroutinefunction(AtomicFactHandler._build_row)

    handle = patches.prepare_call_recorder(tmp_path / "call-log.db")
    assert handle is not None
    assert getattr(OpenAICompatClient.chat, "__avibe_memory_call_patch__", False)
    assert getattr(
        OpenAIEmbeddingProvider._embed_chunk,
        "__avibe_memory_call_patch__",
        False,
    )
    assert getattr(parser.aparse_file, "__avibe_memory_call_patch__", False)
    assert getattr(user_memory._extract_with_retry, "__avibe_memory_call_patch__", False)
    assert getattr(EpisodeHandler._build_row, "__avibe_memory_call_patch__", False)
    assert getattr(AtomicFactHandler._build_row, "__avibe_memory_call_patch__", False)
