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


def test_unscoped_chat_does_not_build_a_diagnostic_request(monkeypatch) -> None:
    conversions = 0

    class _Message:
        def model_dump(self, **_kwargs):
            nonlocal conversions
            conversions += 1
            return {"role": "user", "content": "ignored"}

    async def chat(_self, _messages, **_kwargs):
        return SimpleNamespace(content="ok", model="m", finish_reason=None, usage=None)

    monkeypatch.setattr(patches, "_active_handle", _Handle())
    monkeypatch.setattr(patches, "_strategy_context", lambda: {})
    monkeypatch.setattr(patches, "_request_id", lambda: None)

    result = asyncio.run(patches._chat_wrapper(chat)(SimpleNamespace(), [_Message()]))

    assert result.content == "ok"
    assert conversions == 0


def test_chat_and_embedding_snapshots_apply_collection_caps_before_submit(
    monkeypatch,
) -> None:
    converted: list[int] = []

    class _Message:
        def __init__(self, index: int) -> None:
            self.index = index

        def model_dump(self, **_kwargs):
            converted.append(self.index)
            return {"role": "user", "content": f"message-{self.index}"}

    async def chat(_self, _messages, **_kwargs):
        return SimpleNamespace(content="ok", model="m", finish_reason=None, usage=None)

    async def embed(_self, chunk):
        return [[1.0] for _ in chunk]

    handle = _Handle()
    monkeypatch.setattr(patches, "_active_handle", handle)
    monkeypatch.setattr(patches, "_request_id", lambda: "request-bounded")

    messages = [_Message(index) for index in range(100)]
    with patches.boundary_request():
        asyncio.run(patches._chat_wrapper(chat)(SimpleNamespace(), messages))
        asyncio.run(
            patches._embedding_wrapper(embed)(
                SimpleNamespace(_model="embedding", _dimensions=256),
                [f"input-{index}" for index in range(100)],
            )
        )

    assert converted == [0, 99]
    assert handle.calls[0].request["messages"] == [
        {"role": "user", "content": "message-0"},
        {"omitted_messages": 98},
        {"role": "user", "content": "message-99"},
    ]
    assert handle.calls[1].request["input_count"] == 100
    assert handle.calls[1].request["inputs"] == [
        f"input-{index}" for index in range(16)
    ]
    assert handle.calls[1].request["omitted_inputs"] == 84


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


def test_prepare_call_recorder_passes_configured_provider_keys_to_scrubber(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVEROS_LLM__API_KEY", "plain-llm-credential")
    monkeypatch.setenv("EVEROS_MULTIMODAL__API_KEY", "plain-llm-credential")
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "plain-embedding-credential")
    monkeypatch.setattr(patches, "_install_patches", lambda: None)
    previous = patches._active_handle

    try:
        handle = patches.prepare_call_recorder(tmp_path / "call-log.db")
        assert handle is not None
        assert handle._exact_redaction_values == (
            "plain-llm-credential",
            "plain-llm-credential",
            "plain-embedding-credential",
        )
    finally:
        patches._active_handle = previous


def test_everos_errors_are_scrubbed_before_persistence_across_key_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = "plain-old-provider-credential"
    new_key = "plain-new-provider-credential"
    persisted: list[str] = []
    monkeypatch.setattr(patches, "_persisted_error_base_urls", ())
    monkeypatch.setattr(patches, "_persisted_error_exact_values", (old_key,))

    async def update_status(_self, _run_id, _status, _finished_at, error):
        persisted.append(error)

    async def mark_failed(
        _self,
        _md_path,
        *,
        retryable,
        error,
        new_retry_count,
    ):
        assert retryable is True
        assert new_retry_count == 1
        persisted.append(error)

    run_update = patches._run_record_status_wrapper(update_status)
    md_failure = patches._md_change_failure_wrapper(mark_failed)
    asyncio.run(run_update(object(), "run-1", object(), object(), f"run failed with {old_key}"))
    asyncio.run(
        md_failure(
            object(),
            "profile.md",
            retryable=True,
            error=f"index failed with {old_key}",
            new_retry_count=1,
        )
    )

    monkeypatch.setattr(patches, "_persisted_error_exact_values", (new_key,))
    assert persisted == ["run failed with [REDACTED]", "index failed with [REDACTED]"]
    assert all(old_key not in error for error in persisted)


def test_persisted_errors_scrub_canonical_provider_url_echoes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patches, "_persisted_error_base_urls", ("HTTPS://LLM.Internal.Example/v1",))
    monkeypatch.setattr(patches, "_persisted_error_exact_values", ())

    assert patches._persisted_error("failed at https://llm.internal.example/v1/chat") == (
        "failed at [PROVIDER_BASE_URL]/chat"
    )


def test_pinned_everos_patch_contract(monkeypatch, tmp_path: Path) -> None:
    """Exercise the patched, public EverOS 1.2.1 call surfaces offline.

    This intentionally calls the real wheel implementations after replacing
    only their final provider transports.  It catches both signature drift and
    a patch which still imports but no longer reaches the production paths.
    """
    required = os.environ.get("AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT") == "1"
    if importlib.util.find_spec("everos") is None:
        if required:
            pytest.fail("managed EverOS runtime is required for this contract")
        pytest.skip("managed EverOS runtime is not installed")

    from everalgo.llm import ChatMessage
    from everalgo.llm.config import LLMConfig
    from everalgo.llm.providers.openai_compat import OpenAICompatClient
    from everalgo.parser import RawFile
    from everos.component import parser
    from everos.component.parser import _core as parser_core
    from everos.component.embedding.openai_provider import OpenAIEmbeddingProvider
    from everos.memory.cascade.handlers import atomic_fact, episode
    from everos.memory.cascade.handlers.atomic_fact import AtomicFactHandler
    from everos.memory.cascade.handlers.episode import EpisodeHandler
    from everos.memory.extract.pipeline import user_memory
    from everos.infra.ome._stores.run_record import RunRecordStore
    from everos.infra.persistence.sqlite.repos.md_change_state import (
        md_change_state_repo,
    )
    from pydantic import SecretStr
    from structlog.contextvars import bind_contextvars, reset_contextvars

    # Pin the public parser export and concrete methods that the patch wraps.
    assert list(inspect.signature(parser.aparse_file).parameters) == ["raw_file"]
    assert list(inspect.signature(user_memory._extract_with_retry).parameters) == [
        "extractor",
        "cell",
        "prompt",
        "memcell_id",
    ]
    assert list(inspect.signature(OpenAICompatClient.chat).parameters) == [
        "self",
        "messages",
        "model",
        "temperature",
        "max_tokens",
        "response_format",
        "extra",
    ]
    assert list(inspect.signature(OpenAIEmbeddingProvider._embed_chunk).parameters) == [
        "self",
        "chunk",
    ]
    for handler in (EpisodeHandler, AtomicFactHandler):
        assert list(inspect.signature(handler._build_row).parameters) == [
            "self",
            "owner_id",
            "owner_type",
            "app_id",
            "project_id",
            "md_path",
            "entry",
        ]
    assert list(inspect.signature(RunRecordStore._update_status).parameters) == [
        "self",
        "run_id",
        "status",
        "finished_at",
        "error",
    ]
    assert list(
        inspect.signature(type(md_change_state_repo).mark_failed).parameters
    ) == [
        "self",
        "md_path",
        "retryable",
        "error",
        "new_retry_count",
    ]

    patches.install_error_scrubbers()
    handle = patches.prepare_call_recorder(tmp_path / "call-log.db")
    assert handle is not None
    assert getattr(RunRecordStore._update_status, "__avibe_memory_call_patch__", False)
    assert getattr(
        type(md_change_state_repo).mark_failed,
        "__avibe_memory_call_patch__",
        False,
    )
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
    assert parser.aparse_file.__wrapped__ is parser_core.aparse_file

    captured = _Handle()
    monkeypatch.setattr(patches, "_active_handle", captured)
    monkeypatch.setattr(patches, "_request_id", lambda: "real-wheel-request")

    # Exercise the real package-export parser body and verify that its
    # multimodal provider call inherits the parse context.
    parse_context: list[object] = []

    async def parse_stub(raw_file, *, llm):
        assert isinstance(raw_file, RawFile)
        assert llm is not None
        parse_context.append(patches._current_context.get())
        return "parsed"

    import everalgo.parser
    import everos.component.llm

    monkeypatch.setattr(everalgo.parser, "aparse", parse_stub)
    monkeypatch.setattr(
        everos.component.llm, "get_multimodal_llm_client", lambda: object()
    )
    assert asyncio.run(parser.aparse_file(RawFile(content=b"x"))) == "parsed"
    assert parse_context[0].stage == "parse"
    assert parse_context[0].kind == "multimodal_llm"

    # The concrete episode extractor retains its memcell context through its
    # real retry implementation.
    extract_context: list[object] = []

    class Extractor:
        async def aextract(self, cell, *, sender_id, prompt):
            assert cell == "cell"
            assert sender_id is None
            assert prompt == "prompt"
            extract_context.append(patches._current_context.get())
            return "episode"

    assert asyncio.run(
        user_memory._extract_with_retry(Extractor(), "cell", "prompt", "mem-1")
    ) == "episode"
    assert extract_context[0].stage == "episode_extract"
    assert extract_context[0].memcell_id == "mem-1"

    # Build the actual typed cascade rows with only the embedding capability
    # substituted.  This is the closest safe execution of the production
    # cascade handlers without opening a provider connection.
    cascade_context: list[object] = []

    class Capability:
        async def embed_or_none(self, text):
            cascade_context.append(patches._current_context.get())
            return [float(len(text))] * 1024

    tokenizer = SimpleNamespace(tokenize=lambda text: text.split())
    structured = SimpleNamespace(
        inline={
            "session_id": "session-1",
            "timestamp": "2026-08-04T00:00:00+00:00",
            "parent_type": "memcell",
            "parent_id": "mem-1",
            "sender_ids": "[u-00000000000000000000000000000000]",
        },
        sections={
            "Content": "episode text",
            "Subject": "subject",
            "Fact": "fact text",
        },
    )
    entry = SimpleNamespace(
        entry_id="entry-1", structured=structured, content_sha256="sha"
    )
    monkeypatch.setattr(episode, "get_embedding_capability", lambda: Capability())
    monkeypatch.setattr(atomic_fact, "get_embedding_capability", lambda: Capability())
    episode_handler = object.__new__(EpisodeHandler)
    episode_handler._deps = SimpleNamespace(tokenizer=tokenizer)
    atomic_handler = object.__new__(AtomicFactHandler)
    atomic_handler._deps = SimpleNamespace(tokenizer=tokenizer)
    episode_row = asyncio.run(
        episode_handler._build_row(
            owner_id="u-00000000000000000000000000000000",
            owner_type="user",
            app_id="avibe",
            project_id="p-00000000000000000000000000000000",
            md_path="users/u/episodes/episode.md",
            entry=entry,
        )
    )
    fact_row = asyncio.run(
        atomic_handler._build_row(
            owner_id="u-00000000000000000000000000000000",
            owner_type="user",
            app_id="avibe",
            project_id="p-00000000000000000000000000000000",
            md_path="users/u/.atomic_facts/fact.md",
            entry=entry,
        )
    )
    assert episode_row.entry_id == fact_row.entry_id == "entry-1"
    assert {context.stage for context in cascade_context} == {"cascade"}
    assert {context.parent_id for context in cascade_context} == {"mem-1"}

    # Execute the real client/provider methods after replacing their final
    # network transports.  A boundary request is the only capture admission.
    llm = OpenAICompatClient(
        LLMConfig(
            model="real-wheel-model",
            api_key=SecretStr("test-key"),
            base_url="https://example.invalid/v1",
        )
    )

    async def chat_create(kwargs):
        assert kwargs["model"] == "requested-model"
        return SimpleNamespace(
            content="answer",
            model="real-wheel-model",
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    monkeypatch.setattr(llm, "_chat_create", chat_create)
    with patches.boundary_request():
        response = asyncio.run(
            llm.chat([ChatMessage(role="user", content="hello")], model="requested-model")
        )
    assert response.content == "answer"

    # OME binds strategy execution in structlog contextvars.  The real
    # patched transport must preserve that scope and let it override the HTTP
    # boundary context when it is present.
    tokens = bind_contextvars(strategy_name="reflect", run_id="run-1", attempt=2)
    try:
        with patches.boundary_request():
            assert asyncio.run(
                llm.chat(
                    [ChatMessage(role="user", content="strategy")],
                    model="requested-model",
                )
            ).content == "answer"
    finally:
        reset_contextvars(**tokens)

    embedding = OpenAIEmbeddingProvider(
        model="real-wheel-embedding",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        dim=2,
        dimensions=2,
    )

    async def create_embedding(**kwargs):
        assert kwargs["input"] == ["one"]
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[1.0, 2.0])],
            usage=SimpleNamespace(prompt_tokens=1),
        )

    embedding._client = SimpleNamespace(
        embeddings=SimpleNamespace(create=create_embedding)
    )
    with patches.boundary_request():
        assert asyncio.run(embedding._embed_chunk(["one"])) == [[1.0, 2.0]]

    assert [(call.kind, call.stage) for call in captured.calls] == [
        ("llm", "boundary"),
        ("llm", "strategy"),
        ("embedding", "boundary"),
    ]
    strategy_call = captured.calls[1]
    assert (
        strategy_call.strategy_name,
        strategy_call.run_id,
        strategy_call.attempt,
        strategy_call.request_id,
    ) == ("reflect", "run-1", 2, None)
