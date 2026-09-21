"""The real installed Codex CLI's citation wire contract, end to end.

Every other citation test starts from a marker someone typed into a fixture.
This one starts from the real ``codex`` app-server binary: a loopback provider
serves both halves of a cited answer - the web search the native process runs,
and the model text that cites it - and the notifications Avibe actually receives
are captured. Those captured notifications - not hand-written ones - are then
replayed through the product path (``CodexEventHandler`` ->
``BaseAgent.emit_result_message`` -> ``ConsolidatedMessageDispatcher`` ->
``persist_agent_message``) and read back out of SQLite.

Both halves are reproducible against a loopback provider, which is worth stating
because it was once assumed only the first was:

* the marker is ordinary text in the model's answer. That half is worth proving
  on real bytes because the delimiters are private-use codepoints
  (U+E200..U+E202), exactly the kind of thing a UTF-8 round trip, a JSON escape,
  or a whitespace trim could quietly destroy.
* ``results[]`` comes from the binary's standalone search tool, which posts to
  ``<provider base_url>/alpha/search`` - a path this provider answers itself.
  Activating it takes ``model_providers.<p>.supports_standalone_web_search``
  plus the under-development ``features.standalone_web_search``; with both set
  the binary offers the ``web`` namespace, calls ``web.run``, and reports this
  provider's own results back as a genuine search item. So the search item
  replayed below is a captured native notification, not a schema fixture.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import types
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from config.v2_config import ModelHubBackendModelConfig
from core.citations import _TOKEN_OPEN as TOKEN_OPEN
from core.citations import body_digest
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.agents.base import BaseAgent
from modules.agents.codex.event_handler import CodexEventHandler
from modules.agents.codex.search_history import is_web_search_item
from modules.agents.codex.transport import CodexTransport
from modules.agents.model_hub import ModelHubLaunch, build_codex_hub_launch
from modules.im import MessageContext
from storage import messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.settings_service import upsert_scope
from tests.e2e.drivers import mock_llm_upstream as upstream
from tests.e2e.test_model_hub_catalog_consumer import (
    codex_catalog_runtime,  # noqa: F401 -- fixture dependency
    rejected_external_proxy,  # noqa: F401 -- fixture dependency
)
from tests.scenario_harness.message_delivery import MessageDeliveryController
from tests.test_codex_event_handler import _StubAgent
from vibe.backend_model_catalog import _codex_hub_catalog_bytes

pytestmark = pytest.mark.e2e_model_hub

TOKEN = "isolated-citation-contract-fixture"
MODEL = "citation-route-alias"
SESSION_ID = "sescitation01"

START, SEP, END = "", "", ""
GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
PROBE_URL = "https://example.com/citation-probe-source"
UNRESOLVED = "(source unavailable)"

SEARCH_QUERY = "native web search guide"
# The standalone search tool joins this to the provider base_url the Hub launch
# builds (``<gateway>/v1``), so the loopback gateway can answer it.
SEARCH_PATH = "/v1/alpha/search"
# ``codex_api::search::SearchResponse``: prose for the model, plus the citable
# results the native process reports back as the search item's ``results[]``.
SEARCH_RESPONSE = {
    "output": "Two sources on native web search.",
    "results": [
        {"ref_id": "turn0view0", "title": "Web search - OpenAI API", "url": GUIDE_URL},
        {"ref_id": "turn0view1", "title": "引用探针来源 — Café", "url": PROBE_URL},
    ],
}

# One answer exercising every boundary at once: a resolvable ref, a multi-ref
# marker whose second ref is unknown, the grammar quoted inside a fenced code
# example, and a marker the stream cut off before its terminator.
ANSWER = (
    f"Native web search is documented.{START}cite{SEP}turn0view0{END}\n"
    f"Two sources, one unknown.{START}cite{SEP}turn0view1{SEP}turn9view9{END}\n\n"
    "The marker grammar itself:\n\n"
    f"```\n{START}cite{SEP}turn0view0{END}\n```\n\n"
    f"Truncated tail {START}cite{SEP}turn0view0"
)


@pytest.fixture
def citation_runtime(codex_catalog_runtime, record_property):
    binary, runtime, raw_catalog = codex_catalog_runtime
    version = subprocess.run(
        [binary, "--version"], cwd=runtime.home, env=runtime.env,
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    record_property("codex_version", version)
    Path(runtime.env["CODEX_HOME"], "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        # The search tool has to be offered before the model can call it.
        "[tools]\nweb_search = true\n"
    )
    catalog_path = runtime.home / "citation-models.json"
    catalog_path.write_bytes(_codex_hub_catalog_bytes(
        raw_catalog,
        [ModelHubBackendModelConfig(id=MODEL, context_window=128_000).to_payload()],
    ))
    return binary, runtime, catalog_path


def _substitute(node):
    """Swap the loopback's canned answer for the cited one, wherever it appears."""
    if isinstance(node, str):
        return ANSWER if node == "mock response" else node
    if isinstance(node, list):
        return [_substitute(item) for item in node]
    if isinstance(node, dict):
        return {key: _substitute(value) for key, value in node.items()}
    return node


def _search_call_frames(model: str) -> list[bytes]:
    """The first model response: one call into the binary's ``web`` namespace."""
    call = {
        "type": "function_call",
        "id": "fc_search_001",
        "call_id": "call_search_001",
        "name": "run",
        "namespace": "web",
        "arguments": json.dumps({"search_query": [{"q": SEARCH_QUERY}]}),
        "status": "completed",
    }
    response = {"id": "resp_search_001", "object": "response", "model": model}
    return [
        upstream._sse("response.created", {
            "type": "response.created", "sequence_number": 0,
            "response": {**response, "status": "in_progress"},
        }),
        upstream._sse("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": 1, "output_index": 0, "item": call,
        }),
        upstream._sse("response.completed", {
            "type": "response.completed", "sequence_number": 2,
            "response": {
                **response, "status": "completed", "output": [call],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        }),
    ]


@pytest.fixture
def searched_answer_upstream(monkeypatch) -> list[object]:
    """Serve one real web search, then the answer citing it; record the search.

    Two loopback behaviours, both needed before the native process will produce a
    search item of its own: the first model response calls ``web.run``, and the
    provider answers the standalone search request the binary then posts to its
    own base URL.
    """
    original_frames = upstream._responses_stream_frames
    original_post = upstream.MockLLMUpstreamHandler.do_POST
    responses = itertools.count(1)
    searched: list[object] = []

    def frames(model):
        if next(responses) == 1:
            return _search_call_frames(model)
        rebuilt = []
        for frame in original_frames(model):
            head, _, data = frame.partition(b"data: ")
            event = head.decode("utf-8").removeprefix("event: ").strip() or None
            rebuilt.append(upstream._sse(event, _substitute(json.loads(data))))
        return rebuilt

    def do_POST(handler):  # noqa: N802 -- BaseHTTPRequestHandler's own name
        if urlsplit(handler.path).path != SEARCH_PATH:
            original_post(handler)
            return
        searched.append(handler._read_json_body())
        handler._write_json(HTTPStatus.OK, SEARCH_RESPONSE)

    monkeypatch.setattr(upstream, "_responses_stream_frames", frames)
    monkeypatch.setattr(upstream.MockLLMUpstreamHandler, "do_POST", do_POST)
    return searched


def _run_real_turn(fixture, gateway) -> list[tuple[str, dict]]:
    """Run one real native turn and return every notification it produced."""
    binary, runtime, catalog_path = fixture
    launch = ModelHubLaunch(
        backend="codex", channel="hub", requested_model=MODEL,
        runtime_model=MODEL, target_model="unrelated-target",
        gateway_base_url=gateway.url, gateway_token=TOKEN,
    )
    args, env = build_codex_hub_launch([], runtime.env, launch, model_catalog_path=catalog_path)
    provider = next(
        value.split('"')[1] for value in args if value.startswith('model_provider="')
    )
    args = args + [
        # Standalone search is what makes ``results[]`` real: the tool posts to
        # this provider's own endpoint instead of a hosted service. It is still
        # under development, so the provider flag alone does not enable it.
        "-c", f"model_providers.{provider}.supports_standalone_web_search=true",
        "-c", "features.standalone_web_search=true",
        "-c", "suppress_unstable_features_warning=true",
    ]

    async def probe():
        transport = CodexTransport(
            binary=binary, cwd=str(runtime.home), runtime_args=args, runtime_env=env,
        )
        notifications: list[tuple[str, dict]] = []
        completed: asyncio.Queue = asyncio.Queue()

        async def notify(method, params):
            notifications.append((method, params))
            if method == "turn/completed":
                await completed.put(params)

        transport.on_notification(notify)
        try:
            await transport.start()
            thread = await transport.send_request(
                "thread/start",
                {"model": MODEL, "cwd": transport._cwd, "approvalPolicy": "never"},
            )
            await transport.send_request(
                "turn/start",
                {
                    "threadId": thread["thread"]["id"],
                    "input": [{"type": "text", "text": "Search the web and cite the guide."}],
                },
            )
            result = await asyncio.wait_for(completed.get(), timeout=60)
            assert result["turn"]["status"] == "completed", result
        finally:
            await transport.stop()
        return notifications

    return asyncio.run(probe())


def _captured(fixture) -> tuple[dict, dict, dict]:
    """The real search, answer, and ``turn/completed`` notifications for one turn."""
    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        notifications = _run_real_turn(fixture, gateway)

    def completed(match):
        return next(
            params
            for method, params in notifications
            if method == "item/completed" and match(params.get("item") or {})
        )

    # Recognized by the production predicate, so the shape the binary happens to
    # name its search item is the shape the product path accepts.
    search = completed(lambda item: is_web_search_item(item) and item.get("results"))
    message = completed(lambda item: item.get("type") == "agentMessage")
    completion = next(params for method, params in notifications if method == "turn/completed")
    return search, message, completion


def test_real_codex_relays_citation_markers_verbatim(citation_runtime, searched_answer_upstream):
    """The private-use marker must reach Avibe's parse point byte for byte."""
    search, message, completion = _captured(citation_runtime)

    assert message["item"]["text"] == ANSWER
    assert f"{START}cite{SEP}turn0view0{END}" in message["item"]["text"]
    # The sources are the binary's own: it called the search tool, the tool
    # posted the model's query to this provider, and the results came back as one
    # native item - untouched, including a non-ASCII title.
    assert searched_answer_upstream, "the binary never posted a standalone search"
    assert SEARCH_QUERY in json.dumps(searched_answer_upstream[0], ensure_ascii=False)
    assert [result["ref_id"] for result in search["item"]["results"]] == [
        "turn0view0",
        "turn0view1",
    ]
    assert search["item"]["results"][1]["title"] == "引用探针来源 — Café"
    # The completion notification is what triggers resolution, and it must name
    # the same thread and turn: that scoping is the only reason a ref_id - which
    # is unique nowhere else - is safe to look up at all.
    assert message["threadId"] and message["turnId"]
    assert search["threadId"] == message["threadId"]
    assert completion.get("threadId") == message["threadId"]
    assert completion["turn"]["id"] == message["turnId"]


def test_real_notifications_deliver_and_persist_resolved_citations(
    citation_runtime, searched_answer_upstream, monkeypatch, tmp_path,
):
    """Replay the captured real notifications through the real product path."""
    search, message, completion = _captured(citation_runtime)
    thread_id, turn_id = message["threadId"], message["turnId"]

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = "2026-09-21T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="slack", scope_type="channel", native_id="C-citations", now=now
        )
        conn.execute(agent_sessions.insert().values(
            id=SESSION_ID, scope_id=scope_id, agent_backend="codex", agent_variant="default",
            session_anchor=f"anchor_{SESSION_ID}", native_session_id=thread_id, status="active",
            metadata_json="{}", created_at=now, updated_at=now, last_active_at=now,
        ))

    # Real dispatcher behind the controller surface, exactly as the live wiring
    # has it: the agent emits onto the controller, which delegates to dispatch.
    controller = MessageDeliveryController(platform="slack")
    controller.config.show_duration = False
    dispatcher = ConsolidatedMessageDispatcher(controller)
    controller.emit_agent_message = dispatcher.emit_agent_message

    agent = _StubAgent()
    agent.controller = controller
    agent.config = controller.config
    agent._calculate_duration_ms = lambda started_at: 0
    # The real result emit, so the sidecar crosses the real agent boundary.
    agent.emit_result_message = types.MethodType(BaseAgent.emit_result_message, agent)

    handler = CodexEventHandler(agent)
    context = MessageContext(
        user_id="U1", channel_id="C-citations", platform="slack",
        platform_specific={"agent_session_id": SESSION_ID},
    )
    request = types.SimpleNamespace(
        base_session_id=SESSION_ID,
        session_key="slack::channel::C-citations",
        working_path=str(tmp_path),
        context=context,
        started_at=0,
        output=None,
    )
    agent._turn_registry.register_turn(turn_id, request)

    async def replay():
        # The search arrives in its own notification, ahead of the answer that
        # cites it - as captured, ids and all.
        await handler._on_item_completed(search, request)
        await handler._on_item_completed(message, request)
        await handler._on_turn_completed(completion, request)

    asyncio.run(replay())

    delivered = "\n".join(controller.im_probe.rendered_texts())
    assert f"[developers.openai.com]({GUIDE_URL})" in delivered
    assert f"[example.com]({PROBE_URL})" in delivered
    # The unknown ref is labelled, not deleted, and never guessed into a URL.
    assert UNRESOLVED in delivered
    assert "turn9view9" not in delivered
    # A marker shown as a code example, and one the stream cut off, stay literal.
    assert f"```\n{START}cite{SEP}turn0view0{END}\n```" in delivered
    assert delivered.endswith(f"Truncated tail {START}cite{SEP}turn0view0")

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn, session_id=SESSION_ID, types=("result",)
        )
    row = transcript["messages"][-1]
    citations = (row["content"] or {})["citations"]

    assert [(c["index"], c["ref_id"], c["url"]) for c in citations] == [
        (1, "turn0view0", GUIDE_URL),
        (2, "turn0view1", PROBE_URL),
    ]
    assert citations[1]["title"] == "引用探针来源 — Café"
    # Every sidecar row describes the stored body and nothing else: one digest
    # of that exact text, and spans that land on the links the backend wrote,
    # counted in UTF-16 code units because that is what the renderer counts
    # (ui/src/lib/citations.ts). It verifies the digest before it paints
    # anything, so both numbers here are ones it has to reach independently.
    body = row["text"]
    assert {c["body_sha256"] for c in citations} == {body_digest(body)}
    units = body.encode("utf-16-le", "surrogatepass")
    for citation in citations:
        assert citation["spans"]
        for start, end in citation["spans"]:
            # These labels need no escaping, so the link reads exactly as spelled.
            assert units[start * 2 : end * 2].decode("utf-16-le", "surrogatepass") == (
                f"[{citation['label']}]({citation['url']})"
            )

    # The identity the message travelled under is internal. A reader may see the
    # model's own marker characters - quoted in a code example, or cut off
    # mid-stream, both above - but never a token this delivery minted.
    assert TOKEN_OPEN not in delivered
    assert TOKEN_OPEN not in body
