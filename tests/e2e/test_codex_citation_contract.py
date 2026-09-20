"""The real installed Codex CLI's citation wire contract, end to end.

Every other citation test starts from a marker someone typed into a fixture.
This one starts from the real ``codex`` app-server binary: a loopback Responses
provider answers with text carrying OpenAI's citation markers, the real native
process relays it, and the notifications Avibe actually receives are captured.
Those captured notifications - not hand-written ones - are then replayed through
the product path (``CodexEventHandler`` -> ``BaseAgent.emit_result_message`` ->
``ConsolidatedMessageDispatcher`` -> ``persist_agent_message``) and read back out
of SQLite.

What the loopback can and cannot produce, stated plainly:

* it CAN produce the marker, because the marker is ordinary text in the model's
  answer. That is the half worth proving on real bytes - the delimiters are
  private-use codepoints (U+E200..U+E202), exactly the kind of thing a UTF-8
  round trip, a JSON escape, or a whitespace trim could quietly destroy.
* it CANNOT produce a genuine ``webSearch`` ``results[]``. Those come from the
  hosted search service behind the ``supports_standalone_web_search`` provider
  flag, not from the Responses endpoint this loopback serves - the shipped
  binary exposes no search path a local provider could answer. The search item
  replayed below is therefore shaped from the binary's own schema
  (``ref_id`` / ``title`` / ``url``, ref ids of the ``turn0view0`` form, as
  recorded in ``_tmp/codex-citation-verification-20260919.md``), and that is
  called out here rather than dressed up as captured traffic.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import types
from pathlib import Path

import pytest

from config.v2_config import ModelHubBackendModelConfig
from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.agents.base import BaseAgent
from modules.agents.codex.event_handler import CodexEventHandler
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

START, SEP, END = "\ue200", "\ue202", "\ue201"
GUIDE_URL = "https://developers.openai.com/api/docs/guides/tools-web-search"
PROBE_URL = "https://example.com/citation-probe-source"
UNRESOLVED = "(source unavailable)"

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

# Shaped from the binary's own schema, not captured - see the module docstring.
SEARCH_RESULTS = [
    {"ref_id": "turn0view0", "title": "Web search - OpenAI API", "url": GUIDE_URL},
    {"ref_id": "turn0view1", "title": "引用探针来源 — Café", "url": PROBE_URL},
]


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


@pytest.fixture
def cited_answer_upstream(monkeypatch):
    """Make the loopback model answer with real citation-marker text."""
    original = upstream._responses_stream_frames

    def frames(model):
        rebuilt = []
        for frame in original(model):
            head, _, data = frame.partition(b"data: ")
            event = head.decode("utf-8").removeprefix("event: ").strip() or None
            rebuilt.append(upstream._sse(event, _substitute(json.loads(data))))
        return rebuilt

    monkeypatch.setattr(upstream, "_responses_stream_frames", frames)


def _run_real_turn(fixture, gateway) -> list[tuple[str, dict]]:
    """Run one real native turn and return every notification it produced."""
    binary, runtime, catalog_path = fixture
    launch = ModelHubLaunch(
        backend="codex", channel="hub", requested_model=MODEL,
        runtime_model=MODEL, target_model="unrelated-target",
        gateway_base_url=gateway.url, gateway_token=TOKEN,
    )
    args, env = build_codex_hub_launch([], runtime.env, launch, model_catalog_path=catalog_path)

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
                    "input": [{"type": "text", "text": "Cite the web search guide."}],
                },
            )
            result = await asyncio.wait_for(completed.get(), timeout=30)
            assert result["turn"]["status"] == "completed", result
        finally:
            await transport.stop()
        return notifications

    return asyncio.run(probe())


def _captured(fixture) -> tuple[dict, dict]:
    """The real ``item/completed`` answer and ``turn/completed`` for one turn."""
    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        notifications = _run_real_turn(fixture, gateway)
    message = next(
        params
        for method, params in notifications
        if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage"
    )
    completion = next(params for method, params in notifications if method == "turn/completed")
    return message, completion


def test_real_codex_relays_citation_markers_verbatim(citation_runtime, cited_answer_upstream):
    """The private-use marker must reach Avibe's parse point byte for byte."""
    message, completion = _captured(citation_runtime)

    assert message["item"]["text"] == ANSWER
    assert f"{START}cite{SEP}turn0view0{END}" in message["item"]["text"]
    # The completion notification is what triggers resolution, and it must name
    # the same thread and turn: that scoping is the only reason a ref_id - which
    # is unique nowhere else - is safe to look up at all.
    assert message["threadId"] and message["turnId"]
    assert completion.get("threadId") == message["threadId"]
    assert completion["turn"]["id"] == message["turnId"]


def test_real_notifications_deliver_and_persist_resolved_citations(
    citation_runtime, cited_answer_upstream, monkeypatch, tmp_path,
):
    """Replay the captured real notifications through the real product path."""
    message, completion = _captured(citation_runtime)
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
        # The search result arrives in its own notification, before the answer.
        await handler._on_item_completed(
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "webSearch",
                    "query": "web search guide",
                    "results": SEARCH_RESULTS,
                },
            },
            request,
        )
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
    # Every sidecar entry names a link the stored answer actually carries: that
    # (href, text) pair is what the Web renderer matches on to draw a badge.
    for citation in citations:
        assert f"[{citation['label']}]({citation['url']})" in row["text"]
