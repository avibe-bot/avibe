"""A user-initiated subscription card use must survive interruption without replay."""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import datetime, timezone

import pytest

from core.handlers.model_hub.card_fences import CardFences, FenceUnavailable
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.quota import QuotaSourceRef


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def test_restart_keeps_unconfirmed_until_a_new_user_read(tmp_path):
    """MH-QUOTA-045: An interrupted use is never replayed after restart."""
    path = tmp_path / "fences.json"
    fences = CardFences(path)
    lock = fences.claim("src_example", "fable", NOW)
    with pytest.raises(FenceUnavailable):
        fences.claim("src_example", "weekly", NOW)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    disk = json.loads(path.read_text())["locks"]["src_example"]
    assert set(disk) == {"request_id", "kind", "since", "reason"}
    assert disk["reason"] == "in_flight" and disk["request_id"] == lock
    restarted = CardFences(path)
    assert restarted.public("src_example")["reason"] == "unconfirmed"
    assert "request_id" not in restarted.public("src_example")
    with pytest.raises(FenceUnavailable):
        restarted.claim("src_example", "weekly", NOW)
    token = restarted.refresh_token("src_example")
    restarted.confirm_read("src_example", None)
    assert restarted.public("src_example")["reason"] == "unconfirmed"
    restarted.confirm_read("src_example", token)
    assert restarted.public("src_example") is None
    assert CardFences(path).public("src_example") is None


async def test_two_presses_share_one_vendor_use_and_a_timeout_is_never_retried(tmp_path, monkeypatch):
    """MH-QUOTA-046: Concurrent pages and a timeout never initiate another vendor request."""
    from tests.test_model_hub_api import _service

    service, _, adapter = _service(tmp_path)
    service.card_fences = CardFences(tmp_path / "claims.json")
    source = QuotaSourceRef("src_example", "anthropic", "private-marker", "Example", None)
    monkeypatch.setattr(service, "_quota_sources", lambda: [source])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def key(*_):
        return "a" * 64

    adapter.reset_card_account_key = key

    async def use(source_id, vendor, ref, kind, request_id, expected_key):
        calls.append(request_id)
        entered.set()
        await release.wait()
        raise TimeoutError("private-marker")

    adapter.use_reset_card = use

    async def quota(*_):
        return {"plan": None, "windows": [], "reset_cards": {"state": "ok", "cards": [], "lock": None}}

    adapter.subscription_quota = quota
    first = asyncio.create_task(service.use_reset_card(source.source_id, {"kind": "fable"}))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(ModelHubError) as refused:
        await service.use_reset_card(source.source_id, {"kind": "fable"})
    assert refused.value.code == "reset_card_locked"
    assert (await service.quota_summary())["sources"][0]["reset_cards"]["lock"]["reason"] == "in_flight"
    release.set()
    result = await first
    assert result["use"] == dict(kind="fable", outcome="unconfirmed", reason=None, cleared=[])
    assert "private-marker" not in json.dumps(result)
    assert (await service.quota_summary())["sources"][0]["reset_cards"]["lock"]["reason"] == "unconfirmed"
    assert (await service.quota_summary(force=True))["sources"][0]["reset_cards"]["lock"] is None
    assert len(calls) == 1


def test_corruption_never_reenables_card_use_or_overwrites_evidence(tmp_path):
    path = tmp_path / "fences.json"
    path.write_text('{"private-marker":')
    fences = CardFences(path)
    assert fences.public("src_example")["reason"] == "unconfirmed"
    with pytest.raises(FenceUnavailable) as raised:
        fences.claim("src_example", "session", NOW)
    assert "private-marker" not in str(raised.value)
    assert path.read_text() == '{"private-marker":'


async def test_one_engine_post_picks_matching_kind_and_uses_fresh_quota(tmp_path, monkeypatch, caplog):
    """MH-QUOTA-047: Choose a non-next card by kind/expiry, consume once, reread counts and resume quota."""
    from tests.test_model_hub_api import _service, ModelHubSourceConfig, ModelHubSourceStateConfig
    from tests.test_model_hub_quota import _adapter, _METADATA, CLAUDE_LEGACY, _PROFILE_URL

    org = "01234567-0000-4000-8000-000000000000"
    adapter, engine = _adapter({}, {**_METADATA, "cred_a": {**_METADATA["cred_a"], "source_id": "src_claude0001"}})
    service, store, _ = _service(tmp_path)
    monkeypatch.setattr(adapter, "subscription_account_label", lambda *_: None)
    service.adapter = adapter
    service.card_fences = CardFences(tmp_path / "claims.json")
    source = ModelHubSourceConfig(
        id="src_claude0001",
        kind="subscription",
        vendor="anthropic",
        protocol="anthropic",
        display_name="Fixture",
        supply_channel="hub",
        billing="monthly",
        credential_ref="cred_a",
        models=[],
        state=ModelHubSourceStateConfig(status="cooldown", detail_key="models.source.cooldown.quota_exhausted"),
    )
    store.config.sources.append(source)
    original = engine.management_request
    posts = []
    grant = dict(resets_left=1, usable_now=True, use_requires_limit=False, clears=["five_hour"])

    def handler(method, path, *, payload=None, **kwargs):
        if path == "/auth-files":
            return original(method, path, payload=payload, **kwargs)
        if payload["method"] == "POST":
            data = json.loads(payload["data"])
            posts.append(data)
            assert payload["url"].endswith(f"/organizations/{org}/reset_rate_limits")
            assert data["grant_id"] == "private-earlier"
            return {
                "status_code": 200,
                "body": json.dumps({"result": "reset", "cleared": ["five_hour", "private-unknown"]}),
            }
        if payload["url"] == _PROFILE_URL:
            body = {"organization": {"uuid": org}}
        elif "?" in payload["url"]:
            grants = [
                {**grant, "type": "weekly", "id": "private-next"},
                {**grant, "type": "session", "id": "private-later", "ends_at": "2090-01-01T00:00:00Z"},
            ]
            if not posts:
                grants.append({**grant, "type": "session", "id": "private-earlier", "ends_at": "2080-01-01T00:00:00Z"})
            body = {
                **CLAUDE_LEGACY,
                "cedar_ember": {"eligible": True, "at_limit": True, "grants": grants, "next_grant_id": "private-next"},
            }
        else:
            body = {**CLAUDE_LEGACY, "five_hour": {"utilization": 10 if posts else 100}}
        return {"status_code": 200, "body": json.dumps(body)}

    monkeypatch.setattr(engine, "management_request", handler)
    assert (await service.quota_summary())["sources"][0]["windows"][0]["used_pct"] == 100
    result = await service.use_reset_card(source.id, {"kind": "session"})
    assert result["use"] == dict(kind="session", outcome="used", reason=None, cleared=["five_hour"])
    assert len(posts) == 1 and posts[0]["program"] == "cedar_ember"
    assert result["quota"]["sources"][0]["windows"][0]["used_pct"] == 10
    assert store.load().sources[0].state.status == "standby"
    surface = json.dumps(result) + caplog.text + (tmp_path / "claims.json").read_text()
    for needle in (org, "private-earlier", "private-next", "private-later", "private-unknown", posts[0]["request_id"]):
        assert needle not in surface


async def test_general_model_hub_rpc_does_not_offer_card_use():
    """MH-QUOTA-048: General operation dispatch cannot reach an irreversible card action."""
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc

    with pytest.raises(ModelHubError) as error:
        await dispatch_model_hub_rpc(object(), "use_reset_card", {"kind": "account"})
    assert error.value.status == 404
    from vibe.cli import build_parser
    import argparse

    def paths(parser, prefix=()):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield (*prefix, name)
                    yield from paths(child, (*prefix, name))

    assert not any(any("reset-card" in word for word in path) for path in paths(build_parser()))


@pytest.mark.parametrize(
    ("reply", "remaining", "state", "outcome", "reason"),
    [
        ({"statement": "not_limited", "before": 1}, 1, "ok", "not_used", "not_limited"),
        ({"statement": "cooldown", "before": 1}, 1, "ok", "not_used", "cooldown"),
        ({"statement": "refused", "before": 1}, 1, "ok", "not_used", "refused"),
        ({"statement": "unavailable", "before": 1}, 1, "ok", "not_used", "unavailable"),
        ({"statement": "refused", "before": 1}, 0, "ok", "unconfirmed", None),
        ({"statement": "refused", "before": 1}, 0, "unread", "unconfirmed", None),
        ({"statement": "ack", "before": 1}, 1, "ok", "unconfirmed", None),
        ({"statement": "unknown"}, 0, "ok", "unconfirmed", None),
    ],
)
def test_decision_requires_a_recognized_answer_consistent_with_the_new_count(reply, remaining, state, outcome, reason):
    from core.handlers.model_hub.card_action import _decision

    row = _decision("account", reply, {"state": state, "cards": [{"kind": "account", "count": remaining}]})
    assert (row["outcome"], row["reason"]) == (outcome, reason)


async def test_non_next_grant_refusal_is_returned_once_without_substitution():
    """MH-QUOTA-049: A not-next refusal never retries the vendor with another grant."""
    from tests.test_model_hub_quota import _adapter, _METADATA, _PROFILE_URL
    from vibe.model_hub_runtime.card_use import CLAUDE

    org = "00000000-0000-4000-8000-000000000000"
    grants = [
        {"id": "private-session", "type": "session", "resets_left": 1, "usable_now": True, "clears": ["five_hour"]},
        {"id": "private-weekly", "type": "weekly", "resets_left": 1, "usable_now": True, "clears": ["seven_day"]},
    ]

    def ok(body):
        return {"status_code": 200, "body": json.dumps(body)}

    adapter, engine = _adapter(
        {},
        _METADATA,
        {
            _PROFILE_URL: ok({"organization": {"uuid": org}}),
            CLAUDE + "/oauth/usage?cedar_ember=1&skip_spend=1": ok(
                {
                    "cedar_ember": {
                        "at_limit": True,
                        "eligible": True,
                        "grants": grants,
                        "next_grant_id": "private-weekly",
                    }
                }
            ),
            CLAUDE + "/organizations/" + org + "/reset_rate_limits": ok(
                {"result": "unavailable", "reason": "not_next_grant"}
            ),
        },
    )
    key = await adapter.reset_card_account_key("src_claude", "anthropic", "cred_a")
    answer = await adapter.use_reset_card("src_claude", "anthropic", "cred_a", "session", "synthetic-request", key)
    assert answer == {"statement": "refused", "before": 1, "cleared": []}
    posts = [p for _, path, p in engine.calls if path == "/api-call" and p["method"] == "POST"]
    assert [json.loads(p["data"])["grant_id"] for p in posts] == ["private-session"]


async def test_chatgpt_sends_only_the_earliest_available_credit_and_one_request_id():
    """MH-QUOTA-050: ChatGPT receives the chosen credit and a single request id, never the entire inventory."""
    from tests.test_model_hub_quota import _adapter, _METADATA
    from vibe.model_hub_runtime.card_use import CHATGPT

    def ok(body):
        return {"status_code": 200, "body": json.dumps(body)}

    credit = {
        "status": "available",
        "reset_type": "codex_rate_limits",
        "id": "private-later",
        "expires_at": "2090-01-01T00:00:00Z",
    }
    adapter, engine = _adapter(
        {},
        _METADATA,
        {
            CHATGPT + "/usage": ok({"rate_limit_reset_credits": {"available_count": 2}}),
            CHATGPT + "/rate-limit-reset-credits": ok(
                {"credits": [credit, {**credit, "id": "private-early", "expires_at": "2080-01-01T00:00:00Z"}]}
            ),
            CHATGPT + "/rate-limit-reset-credits/consume": ok(
                {"code": "success", "windows_reset": 2, "credit": {"redeemed_at": "2026-11-20T00:00:00Z"}}
            ),
        },
    )
    key = await adapter.reset_card_account_key("src_codex", "openai", "cred_b")
    answer = await adapter.use_reset_card("src_codex", "openai", "cred_b", "account", "synthetic-request", key)
    assert answer == {"statement": "ack", "before": 2, "cleared": []}
    posts = [p for _, path, p in engine.calls if path == "/api-call" and p["method"] == "POST"]
    assert [json.loads(p["data"]) for p in posts] == [
        {"credit_id": "private-early", "redeem_request_id": "synthetic-request"}
    ]
    assert "private-" not in json.dumps(answer)


async def test_refresh_started_before_unknown_cannot_unlock_the_later_claim(tmp_path, monkeypatch):
    """MH-QUOTA-051: A force read already at the vendor does not reconcile a subsequent unknown generation."""
    from tests.test_model_hub_api import _service

    service, _, adapter = _service(tmp_path)
    service.card_fences = CardFences(tmp_path / "fences.json")
    source = QuotaSourceRef("src_example", "openai", "cred_fixture", "Test", None)
    monkeypatch.setattr(service, "_quota_sources", lambda: [source])

    async def key(*_):
        return "a" * 64

    adapter.reset_card_account_key = key
    started, release = asyncio.Event(), asyncio.Event()

    async def read(*_):
        started.set()
        await release.wait()
        return {"plan": None, "windows": [], "reset_cards": {"state": "ok", "lock": None, "cards": []}}

    adapter.subscription_quota = read
    refresh = asyncio.create_task(service.quota_summary(force=True))
    await asyncio.wait_for(started.wait(), 2)
    claim = service.card_fences.claim("a" * 64, "account", NOW)
    service.card_fences.finish("a" * 64, claim, unknown=True)
    release.set()
    assert (await refresh)["sources"][0]["reset_cards"]["lock"]["reason"] == "unconfirmed"
    assert (await service.quota_summary(force=True))["sources"][0]["reset_cards"]["lock"] is None
