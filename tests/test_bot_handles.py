"""Handle suggestions for new Bots (``docs/plans/bots.md`` D2)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import bot_handles
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers


@pytest.fixture()
def conn(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    with create_sqlite_engine().connect() as connection:
        yield connection


@pytest.mark.parametrize(
    ("display_name", "proposal", "expected"),
    [
        # An ASCII name is normalized; the model is not asked, and its answer would not count.
        ("Researcher", "ignored", ("researcher", "transliteration")),
        ("首席安全官", "cso", ("cso", "model")),
        ("首席安全官", "`CSO`", ("cso", "model")),
        ("首席安全官", "Chief Security Officer", ("shouxianquanguan", "transliteration")),
        ("首席安全官", None, ("shouxianquanguan", "transliteration")),
        ("小爱", "ops", ("ops-2", "model")),
    ],
)
def test_suggestions_follow_the_d2_order(conn, display_name, proposal, expected):
    assert bot_handles.suggest_handle(conn, display_name=display_name, proposal=proposal) == expected


def test_only_names_without_an_ascii_handle_need_the_model():
    assert not bot_handles.needs_model("Researcher")
    assert bot_handles.needs_model("首席安全官")
    assert bot_handles.needs_model("42")


@pytest.mark.parametrize(
    ("efforts", "expected"),
    [(("high", "low", "medium"), "low"), (("max", "minimal"), "minimal"), (("turbo",), "turbo"), ((), None)],
)
def test_lowest_effort(efforts, expected):
    assert bot_handles.lowest_effort(efforts) == expected


def _controller(runtime, model="main-model"):
    store = SimpleNamespace(get_main_bot_agent=lambda: SimpleNamespace(model=model))
    return SimpleNamespace(vibe_agent_store=store, model_hub_runtime=runtime)


def test_the_main_bots_model_answers_at_its_lowest_effort(monkeypatch):
    from core.agent_core.ai.provider import TextDelta
    from modules.agents import model_hub
    from modules.agents.vibey import models

    resolved, retired, sent = [], [], []

    class Runtime:
        async def resolve(self, backend, model, *, process_scope):
            resolved.append((backend, model, process_scope))
            return model_hub.ModelHubLaunch(
                backend="vibey", channel="hub", requested_model=model, target_model=model, runtime_model=model,
                source_id="src", source_identity="identity", gateway_base_url="http://127.0.0.1:1",
                gateway_token="token", protocol="anthropic", provider="anthropic",
                supports_reasoning=True, reasoning_efforts=("high", "low", "medium"),
            )

        def retire_process_scope(self, backend, scope):
            retired.append((backend, scope))

    class Adapter:
        async def stream(self, request, _cancel):
            sent.append(request)
            yield TextDelta(index=0, delta=" cs")
            yield TextDelta(index=0, delta="o\n")

    monkeypatch.setattr(models, "registry_providers", lambda **_kwargs: lambda _protocol: Adapter())
    handle = asyncio.run(
        bot_handles.propose_handle_with_model(_controller(Runtime()), display_name="首席安全官", role="Security")
    )
    assert handle == "cso"
    assert resolved[0][:2] == ("vibey", "main-model")
    assert retired == [("vibey", resolved[0][2])]
    request = sent[0]
    assert (request.reasoning_effort, request.tools) == ("low", ())
    assert "首席安全官" in request.messages[0].content[0].text and "Security" in request.messages[0].content[0].text


def test_no_route_or_no_answer_in_time_means_no_proposal():
    class Unroutable:
        async def resolve(self, *_args, **_kwargs):
            raise RuntimeError("mapping_target_unavailable")

        def retire_process_scope(self, *_args):
            pass

    class Silent(Unroutable):
        async def resolve(self, *_args, **_kwargs):
            await asyncio.sleep(10)

    assert asyncio.run(bot_handles.propose_handle_with_model(_controller(Unroutable()), display_name="小爱")) is None
    assert (
        asyncio.run(bot_handles.propose_handle_with_model(_controller(Silent()), display_name="小爱", budget_seconds=0.05))
        is None
    )
    assert asyncio.run(bot_handles.propose_handle_with_model(_controller(Unroutable(), model=None), display_name="x")) is None


def test_the_route_transliterates_when_the_controller_cannot_answer(conn, monkeypatch):
    from vibe import internal_client
    from vibe.ui_server import app

    calls = []

    async def unavailable(display_name, role, **_kwargs):
        calls.append((display_name, role))
        raise internal_client.InternalServerUnavailable("down")

    monkeypatch.setattr(internal_client, "propose_bot_handle", unavailable)
    client = app.test_client()
    response = client.post(
        "/api/bots/handle-suggestion", json={"display_name": "小爱", "role": "Helps"}, headers=csrf_headers(client)
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"ok": True, "handle": "xiaoai", "source": "transliteration"}
    assert calls == [("小爱", "Helps")]

    async def proposes(*_args, **_kwargs):
        return "aide"

    monkeypatch.setattr(internal_client, "propose_bot_handle", proposes)
    response = client.post("/api/bots/handle-suggestion", json={"display_name": "小爱"}, headers=csrf_headers(client))
    assert response.get_json() == {"ok": True, "handle": "aide", "source": "model"}
