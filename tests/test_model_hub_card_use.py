"""Using a reset card: every outcome that must not happen, pinned at the boundary that owns it.

A card is the user's consumable and using one cannot be undone. Nothing here
reaches a vendor, an engine or a running service: the vendor is a fake at the
adapter or engine-client boundary, and every fence lives under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft7Validator

from core import private_fs
from core.handlers.model_hub import card_fences
from core.handlers.model_hub.card_fences import CardFences, FenceUnavailable
from core.handlers.model_hub.quota import QuotaSourceRef, SubscriptionQuotaError
from core.handlers.model_hub.service import ModelHubError

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
OTHER_ACCOUNT = "b" * 64
CLAUDE = QuotaSourceRef("src_claude0001", "anthropic", "cred_fixture", "Fixture", None)
CHATGPT = QuotaSourceRef("src_chatgpt001", "openai", "cred_other", "Fixture", None)
UNCONFIRMED = {"outcome": "unconfirmed", "reason": None, "cleared": []}


class _Account:
    """One vendor account as the adapter boundary shows it: its cards, and every use request made of it."""

    def __init__(self, cards, key=ACCOUNT):
        self.key = key
        self.cards = dict(cards)
        self.state = "ok"
        self.reading_key = key
        self.uses: list[str] = []
        self.reads: list[str] = []
        self.identity_reads = 0
        self.held = asyncio.Event()
        self.held.set()
        self.read_failure: Exception | None = None
        self.reply = lambda kind: {"statement": "ack", "before": self.cards[kind], "cleared": []}

    async def account_key(self, source_id, vendor, credential_ref):
        self.identity_reads += 1
        return self.key

    async def use(self, source_id, vendor, credential_ref, kind, request_id, expected_key):
        self.uses.append(request_id)
        reply = self.reply(kind)
        return await reply if asyncio.iscoroutine(reply) else reply

    async def quota(self, source_id, vendor, credential_ref):
        self.reads.append(source_id)
        await self.held.wait()
        if self.read_failure is not None:
            raise self.read_failure
        cards = [
            dict(kind=kind, count=count, expires_at=None, stated=False, usable=True, blocked=None, blocked_until=None)
            for kind, count in sorted(self.cards.items())
            if count
        ]
        inventory = {"state": "ok", "lock": None, "cards": cards} if self.state == "ok" else _unread()
        return {"plan": None, "windows": [], "reset_cards": inventory, "reset_card_account": self.reading_key}


def _unread():
    return {"state": "unread", "lock": None, "cards": []}


def _hub(tmp_path, monkeypatch, account, sources, *, fences=None):
    from tests.test_model_hub_api import _service

    service, _store, adapter = _service(tmp_path)
    service.card_fences = fences or CardFences(tmp_path / "fences")
    monkeypatch.setattr(service, "_quota_sources", lambda: list(sources))
    adapter.reset_card_account_key = account.account_key
    adapter.use_reset_card = account.use
    adapter.subscription_quota = account.quota
    return service


def _lock(summary, source_id=None):
    rows = [row for row in summary["sources"] if source_id in (None, row["source_id"])]
    return rows[0]["reset_cards"]["lock"]


def _spend(account, kind, *, statement="ack"):
    """A vendor that answers ``statement``, spending a card first when it acknowledges."""

    def reply(_kind):
        before = account.cards[kind]
        if statement == "ack":
            account.cards[kind] -= 1
        return {"statement": statement, "before": before, "cleared": []}

    return reply


async def _leave_unknown(service, account, source, kind):
    """A use the vendor never answers: the account is left locked with an unknown outcome."""

    async def lost(_kind):
        raise TimeoutError("private-marker")

    account.reply = lost
    result = await service.use_reset_card(source.source_id, {"kind": kind})
    assert result["use"] == {"kind": kind, **UNCONFIRMED}
    return result


# ── A second vendor request ─────────────────────────────────────────────────


async def test_two_presses_on_one_source_send_one_use_and_a_timeout_is_never_retried(tmp_path, monkeypatch):
    """MH-QUOTA-046: A press that arrives while one is handled is refused, and a use the vendor never answers stays one request: unknown, locked, not sent again."""

    account = _Account({"fable": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    entered, answer = asyncio.Event(), asyncio.Event()

    async def lost(_kind):
        entered.set()
        await answer.wait()
        raise TimeoutError("private-marker")

    account.reply = lost
    first = asyncio.create_task(service.use_reset_card(CLAUDE.source_id, {"kind": "fable"}))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(ModelHubError) as second_page:
        await service.use_reset_card(CLAUDE.source_id, {"kind": "fable"})
    assert (second_page.value.status, second_page.value.code) == (409, "reset_card_locked")
    assert _lock(await service.quota_summary())["reason"] == "in_flight"

    answer.set()
    result = await first
    assert result["use"] == {"kind": "fable", **UNCONFIRMED}
    # What the page gets back already shows the settled state, not a use in flight.
    assert _lock(result["quota"])["reason"] == "unconfirmed"
    with pytest.raises(ModelHubError) as after:
        await service.use_reset_card(CLAUDE.source_id, {"kind": "fable"})
    assert after.value.code == "reset_card_locked"
    assert len(account.uses) == 1
    assert "private-marker" not in json.dumps(result)


@pytest.mark.parametrize("named", ["after-the-first-settled", "while-the-first-is-in-flight"])
async def test_overlapping_presses_through_two_sources_of_one_account_send_one_use(tmp_path, monkeypatch, named):
    """MH-QUOTA-052: Two Sources can share an account; a press that overlapped another on it sends nothing, however late its account could be named."""

    account = _Account({"account": 2})
    first = QuotaSourceRef("src_alias00001", "openai", "ref_one", "Fixture", None)
    second = QuotaSourceRef("src_alias00002", "codex", "ref_two", "Fixture", None)
    service = _hub(tmp_path, monkeypatch, account, [first, second])
    using, answer, asked, release_name = (asyncio.Event() for _ in range(4))

    async def account_key(source_id, vendor, credential_ref):
        if source_id == second.source_id:
            asked.set()
            await release_name.wait()
        return account.key

    async def refused(_kind):
        using.set()
        await answer.wait()
        return {"statement": "refused", "before": 2, "cleared": []}

    service.adapter.reset_card_account_key = account_key
    account.reply = refused
    press_one = asyncio.create_task(service.use_reset_card(first.source_id, {"kind": "account"}))
    await asyncio.wait_for(using.wait(), 2)
    press_two = asyncio.create_task(service.use_reset_card(second.source_id, {"kind": "account"}))
    await asyncio.wait_for(asked.wait(), 2)
    if named == "after-the-first-settled":
        # The first press is refused by the vendor and releases its claim: both
        # cards are still held and usable when the second press learns its account.
        answer.set()
        assert (await press_one)["use"]["outcome"] == "not_used"
    release_name.set()
    with pytest.raises(ModelHubError) as overlapped:
        await press_two
    assert (overlapped.value.status, overlapped.value.code) == (409, "reset_card_locked")
    answer.set()
    await press_one
    assert len(account.uses) == 1

    # A press that overlaps nothing is its own use again.
    account.reply = _spend(account, "account")
    assert (await service.use_reset_card(second.source_id, {"kind": "account"}))["use"]["outcome"] == "used"
    assert len(account.uses) == 2


async def test_a_use_found_in_flight_after_a_restart_is_unknown_and_is_not_sent_again(tmp_path, monkeypatch):
    """MH-QUOTA-045: A claim outlives the process: found in flight at startup it is an unknown outcome that only the user's refresh settles."""

    directory = tmp_path / "fences"
    request_id = CardFences(directory).claim(ACCOUNT, "fable", NOW)
    # The process stops here. Whether the request went out, nobody can say.
    stored = {"request_id": request_id, "kind": "fable", "since": "2026-11-20T00:00:00Z", "reason": "in_flight"}
    assert json.loads((directory / "fences.json").read_text()) == {"version": 1, "locks": {ACCOUNT: stored}}
    assert private_fs.is_private(directory, directory.lstat().st_mode)
    assert private_fs.is_private(directory / "fences.json", (directory / "fences.json").lstat().st_mode)

    account = _Account({"fable": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE], fences=CardFences(directory))
    summary = await service.quota_summary()
    assert _lock(summary) == {"kind": "fable", "since": "2026-11-20T00:00:00Z", "reason": "unconfirmed"}
    from tests.test_model_hub_quota import QUOTA_SCHEMA

    Draft7Validator(QUOTA_SCHEMA).validate(summary)
    with pytest.raises(ModelHubError) as refused:
        await service.use_reset_card(CLAUDE.source_id, {"kind": "fable"})
    assert refused.value.code == "reset_card_locked" and account.uses == []

    assert _lock(await service.quota_summary(force=True)) is None
    assert CardFences(directory).view().locks == {}


# ── "Used" or "not used" said wrongly ───────────────────────────────────────


@pytest.mark.parametrize(
    ("statement", "before", "after", "reading", "outcome", "reason"),
    [
        ("ack", 2, 1, "ok", "used", None),
        ("ack", 1, 0, "ok", "used", None),
        ("not_limited", 1, 1, "ok", "not_used", "not_limited"),
        ("cooldown", 1, 1, "ok", "not_used", "cooldown"),
        ("refused", 2, 2, "ok", "not_used", "refused"),
        ("unavailable", 1, 1, "ok", "not_used", "unavailable"),
        # The vendor's word and the count disagree.
        ("ack", 1, 1, "ok", "unconfirmed", None),
        ("refused", 2, 1, "ok", "unconfirmed", None),
        ("not_limited", 1, 0, "ok", "unconfirmed", None),
        # No count the vendor's own report established, under this account, after the request.
        ("ack", 1, 0, "unread", "unconfirmed", None),
        ("refused", 1, 1, "unread", "unconfirmed", None),
        ("ack", 1, 0, "another-account", "unconfirmed", None),
        ("refused", 1, 1, "another-account", "unconfirmed", None),
        ("ack", 1, 0, "failed", "unconfirmed", None),
        ("refused", 1, 1, "failed", "unconfirmed", None),
        # No statement the vendor made.
        ("unknown", 1, 0, "ok", "unconfirmed", None),
        ("something-new", 1, 1, "ok", "unconfirmed", None),
        (None, 1, 0, "ok", "unconfirmed", None),
    ],
)
async def test_an_outcome_is_told_only_when_the_vendor_and_a_fresh_count_agree(
    tmp_path, monkeypatch, statement, before, after, reading, outcome, reason
):
    """MH-QUOTA-053: "Used" needs an acknowledgement and a lower count, "not used" a stated refusal and the same count, both from a recognised reading of the same account made after the request; anything else is unknown and stays locked."""

    account = _Account({"session": before})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    await service.quota_summary()
    reads_before_use = len(account.reads)

    def reply(_kind):
        account.cards["session"] = after
        account.state = "unread" if reading == "unread" else "ok"
        account.reading_key = OTHER_ACCOUNT if reading == "another-account" else ACCOUNT
        account.read_failure = SubscriptionQuotaError("unavailable") if reading == "failed" else None
        if statement is None:
            return {}
        return {"statement": statement, "before": before, "cleared": ["five_hour", "private-unknown"]}

    account.reply = reply
    result = await service.use_reset_card(CLAUDE.source_id, {"kind": "session"})

    cleared = ["five_hour"] if outcome == "used" else []
    assert result["use"] == {"kind": "session", "outcome": outcome, "reason": reason, "cleared": cleared}
    # Read again by itself after the request, not served from the reading taken before it.
    assert len(account.reads) == reads_before_use + 1 and len(account.uses) == 1
    locked = service.card_fences.view().locks
    assert (ACCOUNT in locked) is (outcome == "unconfirmed")
    if outcome == "unconfirmed":
        assert locked[ACCOUNT]["reason"] == "unconfirmed"


@pytest.mark.parametrize(
    ("vendor", "usage", "cards", "state"),
    [
        ("anthropic", "claude", {}, "unread"),
        ("anthropic", "claude", {"unrelated": []}, "unread"),
        ("anthropic", "claude", {"cedar_ember": None}, "ok"),
        ("anthropic", "claude", "claude", "ok"),
        ("openai", {"plan_type": "plus"}, None, "unread"),
        ("openai", "codex", None, "ok"),
    ],
)
async def test_only_a_recognised_report_says_an_account_holds_no_cards(vendor, usage, cards, state):
    """MH-QUOTA-054: A 200 that says nothing about cards is unread, never "none"; only the vendor's own report without cards is an empty inventory."""

    from tests.test_model_hub_quota import _METADATA, CLAUDE_LEGACY, CODEX_REPORT, _adapter
    from vibe.model_hub_runtime.card_use import CLAUDE as CLAUDE_API

    reports = {"claude": CLAUDE_LEGACY, "codex": CODEX_REPORT}

    def answer(body):
        return {"status_code": 200, "body": json.dumps(reports.get(body, body) if isinstance(body, str) else body)}

    by_url = {f"{CLAUDE_API}/oauth/usage?cedar_ember=1&skip_spend=1": answer(cards)} if cards is not None else {}
    adapter, _engine = _adapter(answer(usage), _METADATA, by_url)
    source = ("src_claude", "anthropic", "cred_a") if vendor == "anthropic" else ("src_codex", "openai", "cred_b")
    parsed = await adapter.subscription_quota(*source)
    assert parsed["reset_cards"] == {"state": state, "lock": None, "cards": []}
    # The windows of the same report are kept either way.
    assert isinstance(parsed["windows"], list)


def _use_engine(vendor, answer):
    """A fake engine holding one usable card of the vendor's kind; ``answer`` is its reply to the use request."""

    from tests.test_model_hub_quota import _METADATA, _PROFILE_URL, _adapter
    from vibe.model_hub_runtime.card_use import CHATGPT as CHATGPT_API
    from vibe.model_hub_runtime.card_use import CLAUDE as CLAUDE_API

    def ok(body):
        return {"status_code": 200, "body": json.dumps(body)}

    reply = answer
    if vendor == "anthropic":
        organization = "00000000-0000-4000-8000-000000000000"
        grant = {"id": "private-grant", "type": "session", "resets_left": 1, "usable_now": True, "clears": ["five_hour"]}
        program = {"at_limit": True, "eligible": True, "grants": [grant], "next_grant_id": "private-grant"}
        by_url = {
            _PROFILE_URL: ok({"organization": {"uuid": organization}}),
            f"{CLAUDE_API}/oauth/usage?cedar_ember=1&skip_spend=1": ok({"cedar_ember": program}),
            f"{CLAUDE_API}/organizations/{organization}/reset_rate_limits": reply,
        }
        source, kind = ("src_claude", "anthropic", "cred_a"), "session"
    else:
        credit = {"status": "available", "reset_type": "codex_rate_limits", "id": "private-credit"}
        by_url = {
            f"{CHATGPT_API}/usage": ok({"plan_type": "pro", "rate_limit_reset_credits": {"available_count": 1}}),
            f"{CHATGPT_API}/rate-limit-reset-credits": ok({"credits": [credit]}),
            f"{CHATGPT_API}/rate-limit-reset-credits/consume": reply,
        }
        source, kind = ("src_codex", "openai", "cred_b"), "account"
    adapter, engine = _adapter({}, _METADATA, by_url)
    return adapter, engine, source, kind


def _vendor_writes(engine):
    return [payload for _, path, payload in engine.calls if path == "/api-call" and payload["method"] == "POST"]


_MOMENT = "2026-11-20T00:00:00Z"


@pytest.mark.parametrize(
    ("vendor", "body", "statement", "cleared"),
    [
        # What the vendor is known to say, and what each means.
        ("anthropic", {"result": "reset", "cleared": ["five_hour", "private-unknown"]}, "ack", ["five_hour"]),
        ("anthropic", {"result": "reset", "reason": "", "cooldown_until": _MOMENT}, "ack", []),
        ("anthropic", {"result": "not_limited"}, "not_limited", []),
        ("anthropic", {"result": "cooldown", "cooldown_until": _MOMENT}, "cooldown", []),
        ("anthropic", {"result": "ineligible", "reason": "tenure"}, "refused", []),
        ("anthropic", {"result": "ineligible", "reason": None}, "refused", []),
        ("anthropic", {"result": "unavailable", "reason": "not_next_grant"}, "refused", []),
        ("openai", {"code": "reset", "windows_reset": 2, "credit": {"redeemed_at": _MOMENT}}, "ack", []),
        ("openai", {"code": "reset"}, "ack", []),
        ("openai", {"code": "nothing_to_reset", "windows_reset": 0}, "not_limited", []),
        ("openai", {"code": "no_credit"}, "unavailable", []),
        # A result that may have happened, or that this client does not know.
        ("anthropic", {"result": "unavailable"}, "unknown", None),
        ("anthropic", {"result": "already_used"}, "unknown", None),
        ("openai", {"code": "already_redeemed", "credit": {"redeemed_at": _MOMENT}}, "unknown", None),
        ("anthropic", {"result": "unavailable", "reason": "stamp_indeterminate"}, "unknown", None),
        ("anthropic", {"result": "reset", "reason": "reset_unconfirmed"}, "unknown", None),
        ("anthropic", {"result": "ineligible", "reason": "something_new"}, "unknown", None),
        ("anthropic", {"result": "something_new"}, "unknown", None),
        ("anthropic", {}, "unknown", None),
        ("openai", {"code": "ok", "windows_reset": 1}, "unknown", None),
        ("openai", {}, "unknown", None),
        # Fields of the wrong type.
        ("anthropic", {"result": "ineligible", "reason": False}, "unknown", None),
        ("anthropic", {"result": "ineligible", "reason": ["tenure"]}, "unknown", None),
        ("anthropic", {"result": ["reset"]}, "unknown", None),
        ("anthropic", {"result": "reset", "cleared": "five_hour"}, "unknown", None),
        ("anthropic", {"result": "reset", "cleared": [7]}, "unknown", None),
        ("anthropic", {"result": "cooldown", "cooldown_until": "soon"}, "unknown", None),
        ("openai", {"code": 1}, "unknown", None),
        ("openai", {"code": "reset", "windows_reset": True}, "unknown", None),
        ("openai", {"code": "reset", "windows_reset": "2"}, "unknown", None),
        ("openai", {"code": "reset", "credit": "private-credit"}, "unknown", None),
        ("openai", {"code": "reset", "credit": {"redeemed_at": "not a time"}}, "unknown", None),
        # Evidence that contradicts the result.
        ("anthropic", {"result": "reset", "reason": "not_next_grant"}, "unknown", None),
        ("anthropic", {"result": "cooldown", "cleared": ["five_hour"]}, "unknown", None),
        ("anthropic", {"result": "unavailable", "reason": "no_grant", "cleared": ["seven_day"]}, "unknown", None),
        ("openai", {"code": "reset", "windows_reset": 0}, "unknown", None),
        ("openai", {"code": "no_credit", "windows_reset": 1}, "unknown", None),
        ("openai", {"code": "no_credit", "credit": {"redeemed_at": _MOMENT}}, "unknown", None),
        ("openai", {"code": "nothing_to_reset", "credit": {"redeemed_at": _MOMENT}}, "unknown", None),
        # An error envelope, whatever else it carries.
        ("anthropic", {"result": "reset", "error": {"type": "private-marker"}}, "unknown", None),
    ],
)
async def test_a_vendor_answer_is_a_statement_only_when_recognised_and_consistent(vendor, body, statement, cleared):
    """MH-QUOTA-055: Field types are checked, only known result and reason pairs are statements, and an answer that contradicts itself is unknown; whatever it is, the request was sent once."""

    adapter, engine, source, kind = _use_engine(vendor, {"status_code": 200, "body": json.dumps(body)})
    key = await adapter.reset_card_account_key(*source)
    answer = await adapter.use_reset_card(*source, kind, "synthetic-request", key)
    expected = {"statement": "unknown"} if statement == "unknown" else dict(statement=statement, before=1, cleared=cleared)
    assert answer == expected
    assert len(_vendor_writes(engine)) == 1
    assert "private-" not in json.dumps(answer)


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
@pytest.mark.parametrize("lost", ["transport", "401", "403", "429", "500", "not-json", "not-an-object"])
async def test_a_use_request_without_a_readable_200_is_unknown_and_sent_once(vendor, lost):
    """MH-QUOTA-056: No answer, a timeout, any status but 200 and an unreadable body leave the outcome unknown; none of them is a refusal and none is retried."""

    from vibe.model_hub_runtime.client import EngineClientError

    replies = {
        "transport": EngineClientError("private-marker"),
        "not-json": {"status_code": 200, "body": "<html>private-marker"},
        "not-an-object": {"status_code": 200, "body": "[]"},
    }
    reply = replies.get(lost) or {"status_code": int(lost), "body": json.dumps({"result": "ineligible"})}
    adapter, engine, source, kind = _use_engine(vendor, reply)
    key = await adapter.reset_card_account_key(*source)
    assert await adapter.use_reset_card(*source, kind, "synthetic-request", key) == {"statement": "unknown"}
    assert len(_vendor_writes(engine)) == 1


async def test_one_engine_post_picks_matching_kind_and_uses_fresh_quota(tmp_path, monkeypatch, caplog):
    """MH-QUOTA-047: The card is chosen by kind and earliest expiry, not the vendor's next one; one request is sent; the limits returned are read afterwards; a paused account resumes; nothing of the vendor's reaches any surface."""

    from tests.test_model_hub_api import ModelHubSourceConfig, ModelHubSourceStateConfig, _service
    from tests.test_model_hub_quota import _METADATA, _PROFILE_URL, CLAUDE_LEGACY, _adapter

    organization = "01234567-0000-4000-8000-000000000000"
    adapter, engine = _adapter({}, {**_METADATA, "cred_a": {**_METADATA["cred_a"], "source_id": "src_claude0001"}})
    service, store, _ = _service(tmp_path)
    monkeypatch.setattr(adapter, "subscription_account_label", lambda *_: None)
    service.adapter = adapter
    service.card_fences = CardFences(tmp_path / "fences")
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
    engine_inventory = engine.management_request
    posts = []
    grant = dict(resets_left=1, usable_now=True, use_requires_limit=False, clears=["five_hour"])

    def vendor(method, path, *, payload=None, **kwargs):
        if path == "/auth-files":
            return engine_inventory(method, path, payload=payload, **kwargs)
        if payload["method"] == "POST":
            data = json.loads(payload["data"])
            posts.append(data)
            assert payload["url"].endswith(f"/organizations/{organization}/reset_rate_limits")
            assert data["grant_id"] == "private-earlier"
            return {
                "status_code": 200,
                "body": json.dumps({"result": "reset", "cleared": ["five_hour", "private-unknown"]}),
            }
        if payload["url"] == _PROFILE_URL:
            body = {"organization": {"uuid": organization}}
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

    monkeypatch.setattr(engine, "management_request", vendor)
    assert (await service.quota_summary())["sources"][0]["windows"][0]["used_pct"] == 100
    with caplog.at_level("INFO"):
        result = await service.use_reset_card(source.id, {"kind": "session"})
    assert result["use"] == dict(kind="session", outcome="used", reason=None, cleared=["five_hour"])
    assert len(posts) == 1 and posts[0]["program"] == "cedar_ember"
    # The request id is a UUID as written, the shape other clients of both vendors send.
    assert re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", posts[0]["request_id"])
    assert result["quota"]["sources"][0]["windows"][0]["used_pct"] == 10
    assert result["quota"]["sources"][0]["reset_cards"]["lock"] is None
    assert store.load().sources[0].state.status == "standby"
    assert "Model Hub reset card use: source=src_claude0001 kind=session outcome=used" in caplog.text

    from vibe.model_hub_runtime.card_use import binding_key

    surfaces = json.dumps(result) + caplog.text + (tmp_path / "fences" / "fences.json").read_text()
    private = (organization, "private-earlier", "private-next", "private-later", "private-unknown")
    for needle in (*private, posts[0]["request_id"], binding_key("anthropic", organization), "$TOKEN$"):
        assert needle not in surfaces
    assert json.loads((tmp_path / "fences" / "fences.json").read_text()) == {"version": 1, "locks": {}}


async def test_non_next_grant_refusal_is_returned_once_without_substitution():
    """MH-QUOTA-049: The grant of the kind the user chose is sent even when it is not the vendor's next one, and its refusal is a clean "refused": never another grant, never a second request."""

    grants = [
        {"id": "private-session", "type": "session", "resets_left": 1, "usable_now": True, "clears": ["five_hour"]},
        {"id": "private-weekly", "type": "weekly", "resets_left": 1, "usable_now": True, "clears": ["seven_day"]},
    ]
    from tests.test_model_hub_quota import _METADATA, _PROFILE_URL, _adapter
    from vibe.model_hub_runtime.card_use import CLAUDE as CLAUDE_API

    organization = "00000000-0000-4000-8000-000000000000"

    def ok(body):
        return {"status_code": 200, "body": json.dumps(body)}

    program = {"at_limit": True, "eligible": True, "grants": grants, "next_grant_id": "private-weekly"}
    adapter, engine = _adapter(
        {},
        _METADATA,
        {
            _PROFILE_URL: ok({"organization": {"uuid": organization}}),
            f"{CLAUDE_API}/oauth/usage?cedar_ember=1&skip_spend=1": ok({"cedar_ember": program}),
            f"{CLAUDE_API}/organizations/{organization}/reset_rate_limits": ok(
                {"result": "unavailable", "reason": "not_next_grant"}
            ),
        },
    )
    key = await adapter.reset_card_account_key("src_claude", "anthropic", "cred_a")
    answer = await adapter.use_reset_card("src_claude", "anthropic", "cred_a", "session", "synthetic-request", key)
    assert answer == {"statement": "refused", "before": 1, "cleared": []}
    assert [json.loads(post["data"])["grant_id"] for post in _vendor_writes(engine)] == ["private-session"]


async def test_chatgpt_sends_only_the_earliest_available_credit_and_one_request_id():
    """MH-QUOTA-050: ChatGPT is sent the available credit that expires first with one request id, never the inventory."""

    from tests.test_model_hub_quota import _METADATA, _adapter
    from vibe.model_hub_runtime.card_use import CHATGPT as CHATGPT_API

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
            f"{CHATGPT_API}/usage": ok({"plan_type": "pro", "rate_limit_reset_credits": {"available_count": 2}}),
            f"{CHATGPT_API}/rate-limit-reset-credits": ok(
                {"credits": [credit, {**credit, "id": "private-early", "expires_at": "2080-01-01T00:00:00Z"}]}
            ),
            f"{CHATGPT_API}/rate-limit-reset-credits/consume": ok(
                {"code": "reset", "windows_reset": 2, "credit": {"redeemed_at": "2026-11-20T00:00:00Z"}}
            ),
        },
    )
    key = await adapter.reset_card_account_key("src_codex", "openai", "cred_b")
    answer = await adapter.use_reset_card("src_codex", "openai", "cred_b", "account", "synthetic-request", key)
    assert answer == {"statement": "ack", "before": 2, "cleared": []}
    assert [json.loads(post["data"]) for post in _vendor_writes(engine)] == [
        {"credit_id": "private-early", "redeem_request_id": "synthetic-request"}
    ]
    assert "private-" not in json.dumps(answer)


@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
async def test_a_grant_that_now_belongs_to_another_account_is_not_used(vendor):
    """MH-QUOTA-057: The claim is on one account: if the Source's grant is another account's by the time of the request, nothing is sent."""

    adapter, engine, source, kind = _use_engine(vendor, {"status_code": 200, "body": "{}"})
    answer = await adapter.use_reset_card(*source, kind, "synthetic-request", OTHER_ACCOUNT)
    assert answer == {"statement": "no_card"}
    assert _vendor_writes(engine) == []


# ── An unknown outcome that goes away by itself, or shows as a generic error ─


async def test_only_the_users_reading_begun_after_the_unknown_outcome_lifts_the_lock(tmp_path, monkeypatch):
    """MH-QUOTA-051: Neither the use's own re-read nor a refresh already at the vendor answers an unknown outcome; the first reading the user asks for afterwards does, and repeated refreshes share it."""

    from core.handlers.model_hub import quota

    account = _Account({"account": 1})
    first = QuotaSourceRef("src_alias00001", "openai", "ref_one", "Fixture", None)
    second = QuotaSourceRef("src_alias00002", "codex", "ref_two", "Fixture", None)
    service = _hub(tmp_path, monkeypatch, account, [first, second])
    monkeypatch.setattr(quota, "QUOTA_READ_DEADLINE_SECONDS", 0.05)
    at_vendor, gate = asyncio.Event(), asyncio.Event()
    read = account.quota

    async def held_for_second(source_id, vendor, credential_ref):
        if source_id == second.source_id:
            at_vendor.set()
            await gate.wait()
        return await read(source_id, vendor, credential_ref)

    service.adapter.subscription_quota = held_for_second
    # A refresh the user asked for is at the vendor for the second Source...
    early = asyncio.create_task(service.quota_summary(force=True))
    await asyncio.wait_for(at_vendor.wait(), 2)
    # ...when a use through the first Source goes unanswered.
    await _leave_unknown(service, account, first, "account")
    await early
    gate.set()
    await asyncio.sleep(0.01)
    reads = len(account.reads)

    # Both Sources now hold a recognised reading of the account that the user asked
    # for: the use's own re-read, and the refresh that was already under way. Neither
    # began after the outcome became unknown. The readings that would are held.
    account.held.clear()
    summary = await service.quota_summary(force=True)
    assert {row["reset_cards"]["lock"]["reason"] for row in summary["sources"]} == {"unconfirmed"}
    assert len(account.reads) == reads + 2

    account.held.set()
    await asyncio.sleep(0.01)
    # The readings are there now, but a read nobody forced settles nothing.
    summary = await service.quota_summary()
    assert {row["reset_cards"]["lock"]["reason"] for row in summary["sources"]} == {"unconfirmed"}
    # The refresh that asked for them is over; the next one finds them and asks no vendor again.
    summary = await service.quota_summary(force=True)
    assert [row["reset_cards"]["lock"] for row in summary["sources"]] == [None, None]
    assert len(account.reads) == reads + 2


async def test_an_unknown_outcome_is_not_lifted_by_a_background_unread_or_foreign_reading(tmp_path, monkeypatch):
    """MH-QUOTA-058: A reading nobody asked for, an inventory that could not be read and a reading of another account leave an unknown outcome locked."""

    from core.handlers.model_hub import quota

    monkeypatch.setattr(quota, "QUOTA_READ_DEADLINE_SECONDS", 0.05)
    account = _Account({"session": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    clock = [service.now()]
    service.now = lambda: clock[0]
    await _leave_unknown(service, account, CLAUDE, "session")

    def locked():
        return service.card_fences.view().locks.get(ACCOUNT, {}).get("reason")

    # The periodic re-read of an open page: after the lock, recognised, the same account, but not asked for.
    clock[0] += timedelta(minutes=6)
    reads = len(account.reads)
    assert _lock(await service.quota_summary())["reason"] == "unconfirmed"
    assert len(account.reads) == reads + 1 and locked() == "unconfirmed"
    # The user's refresh is still at the vendor when its deadline passes: the one
    # reading at hand is that background one, and it does not answer.
    account.held.clear()
    assert _lock(await service.quota_summary(force=True))["reason"] == "unconfirmed"
    assert locked() == "unconfirmed"

    # The refresh comes back without an inventory.
    account.state = "unread"
    account.held.set()
    await asyncio.sleep(0.01)
    assert _lock(await service.quota_summary(force=True))["reason"] == "unconfirmed"
    assert locked() == "unconfirmed"

    account.state, account.reading_key = "ok", OTHER_ACCOUNT
    clock[0] += timedelta(minutes=1)
    await service.quota_summary(force=True)
    assert locked() == "unconfirmed"

    account.reading_key = ACCOUNT
    clock[0] += timedelta(minutes=1)
    assert _lock(await service.quota_summary(force=True)) is None and locked() is None


async def test_refreshes_of_one_unknown_use_share_a_reading_and_wait_out_a_rate_limit(tmp_path, monkeypatch):
    """MH-QUOTA-059: Concurrent refreshes of an unknown use make one vendor reading, and after a vendor 429 no further reading is made until its cooldown ends; the lock stays meanwhile."""

    account = _Account({"session": 2})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    clock = [service.now()]
    service.now = lambda: clock[0]
    await _leave_unknown(service, account, CLAUDE, "session")
    reads = len(account.reads)

    account.held.clear()
    refreshes = [asyncio.create_task(service.quota_summary(force=True)) for _ in range(3)]
    await asyncio.sleep(0.01)
    account.held.set()
    assert [_lock(summary) for summary in await asyncio.gather(*refreshes)] == [None, None, None]
    assert len(account.reads) == reads + 1

    # The refresh of a second unknown use is itself rate limited: no more readings until the cooldown ends.
    await _leave_unknown(service, account, CLAUDE, "session")
    reads = len(account.reads)
    account.read_failure = SubscriptionQuotaError("rate_limited", retry_after_seconds=600)
    assert _lock(await service.quota_summary(force=True))["reason"] == "unconfirmed"
    account.read_failure = None
    for _ in range(3):
        assert _lock(await service.quota_summary(force=True))["reason"] == "unconfirmed"
    assert len(account.reads) == reads + 1
    clock[0] += timedelta(minutes=11)
    assert _lock(await service.quota_summary(force=True)) is None
    assert len(account.reads) == reads + 2

    # The same when it is the use's own re-read that the vendor rate limits.
    async def lost_and_limited(_kind):
        account.read_failure = SubscriptionQuotaError("rate_limited", retry_after_seconds=600)
        raise TimeoutError("private-marker")

    account.reply = lost_and_limited
    assert (await service.use_reset_card(CLAUDE.source_id, {"kind": "session"}))["use"]["outcome"] == "unconfirmed"
    account.read_failure = None
    reads = len(account.reads)
    for _ in range(3):
        assert _lock(await service.quota_summary(force=True))["reason"] == "unconfirmed"
    assert len(account.reads) == reads
    clock[0] += timedelta(minutes=11)
    assert _lock(await service.quota_summary(force=True)) is None
    assert len(account.reads) == reads + 1


@pytest.mark.parametrize("answer", ["used", "not_used", "nothing-sent", "lost"])
async def test_a_settlement_that_cannot_be_written_is_the_unconfirmed_result_with_the_lock_held(
    tmp_path, monkeypatch, answer
):
    """MH-QUOTA-060: If the disk fails after the claim, the press still ends as the closed "unconfirmed" result with the account locked, not as an error; the request is not repeated, and the claim on disk survives a restart."""

    account = _Account({"session": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    write_durable = card_fences.write_durable

    def disk_full(path, data):
        raise OSError("private-marker")

    def reply(_kind):
        # The claim is on disk; from here nothing more can be written.
        monkeypatch.setattr(card_fences, "write_durable", disk_full)
        if answer == "nothing-sent":
            return {"statement": "no_card"}
        if answer == "lost":
            raise TimeoutError("private-marker")
        return _spend(account, "session", statement="ack" if answer == "used" else "refused")(_kind)

    account.reply = reply
    result = await service.use_reset_card(CLAUDE.source_id, {"kind": "session"})
    assert result["use"] == {"kind": "session", **UNCONFIRMED}
    assert _lock(result["quota"])["reason"] == "unconfirmed"
    assert len(account.uses) == 1 and "private-marker" not in json.dumps(result)
    assert CardFences(tmp_path / "fences").view().locks[ACCOUNT]["reason"] == "unconfirmed"

    # Once the disk writes again, the user's refresh settles it like any unknown outcome.
    monkeypatch.setattr(card_fences, "write_durable", write_durable)
    assert _lock(await service.quota_summary(force=True)) is None
    assert CardFences(tmp_path / "fences").view().locks == {}


def _controller_client(monkeypatch, handler):
    """The Web process's Model Hub client, with the controller socket replaced by ``handler``."""

    from types import SimpleNamespace

    from vibe import internal_client, model_hub_client

    requests: list[httpx.Request] = []

    def transport(request):
        requests.append(request)
        return handler(request)

    async def endpoint(_socket_path):
        return SimpleNamespace(base_url="http://controller", headers={}, descriptor=None)

    monkeypatch.setattr(internal_client, "_resolve_endpoint_async", endpoint)
    monkeypatch.setattr(internal_client, "_async_transport", lambda _endpoint: httpx.MockTransport(transport))
    return model_hub_client.ModelHubRemoteService(), requests


def _uses(requests):
    return [request for request in requests if request.url.path == "/internal/model-hub-ui/reset-card"]


@pytest.mark.parametrize("lost", ["connection", "timeout", "controller-error", "not-json", "no-result"])
async def test_a_use_whose_answer_is_lost_between_the_processes_is_unconfirmed_not_an_error(monkeypatch, lost):
    """MH-QUOTA-061: Once the controller was handed the use, a lost or unusable answer is the closed "unconfirmed" result with the summary read again, and the request is not handed over twice."""

    summary = {"refresh_interval_seconds": 300, "sources": []}

    def controller(request):
        if request.url.path != "/internal/model-hub-ui/reset-card":
            return httpx.Response(200, json={"ok": True, "result": summary})
        if lost == "connection":
            raise httpx.ReadError("private-marker", request=request)
        if lost == "timeout":
            raise httpx.ReadTimeout("private-marker", request=request)
        if lost == "controller-error":
            return httpx.Response(500, text="private-marker")
        if lost == "not-json":
            return httpx.Response(200, text="private-marker")
        return httpx.Response(200, json={"ok": True, "result": None})

    client, requests = _controller_client(monkeypatch, controller)
    result = await client.use_reset_card("src_claude0001", {"kind": "weekly"})
    assert result == {"quota": summary, "use": {"kind": "weekly", **UNCONFIRMED}}
    assert len(_uses(requests)) == 1


@pytest.mark.parametrize(
    ("answer", "status", "code"),
    [
        ("unreached", 503, "engine_down"),
        ("locked", 409, "reset_card_locked"),
        ("unavailable", 409, "reset_card_unavailable"),
        ("invalid", 400, "invalid_parameter"),
        ("disabled", 404, "feature_disabled"),
    ],
)
async def test_a_use_the_controller_refused_or_never_received_keeps_its_own_answer(monkeypatch, answer, status, code):
    """MH-QUOTA-061: A refusal the controller stated, and a request that never reached it, are reported as what they are: no card can have been spent."""

    def controller(request):
        if answer == "unreached":
            raise httpx.ConnectError("private-marker", request=request)
        return httpx.Response(status, json={"ok": False, "error": code})

    client, requests = _controller_client(monkeypatch, controller)
    with pytest.raises(ModelHubError) as refused:
        await client.use_reset_card("src_claude0001", {"kind": "weekly"})
    assert (refused.value.status, refused.value.code) == (status, code)
    assert len(requests) == 1


# ── A caller that must not reach the use, and a body that must not reach the loop ─


async def test_general_model_hub_rpc_does_not_offer_card_use():
    """MH-QUOTA-048: Neither the operation dispatcher the `vibe` CLI reaches nor any CLI command offers a card use, and nothing an Agent reads or calls names one."""

    import argparse

    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    from vibe.cli import build_parser

    service = AsyncMock()
    for operation in ("use_reset_card", "reset_card_use", "reset-card", "use_reset_cards"):
        with pytest.raises(ModelHubError) as unknown:
            await dispatch_model_hub_rpc(service, operation, {"source_id": "src_claude0001", "kind": "account"})
        assert unknown.value.status == 404
    service.use_reset_card.assert_not_called()

    def commands(parser, prefix=()):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield (*prefix, name)
                    yield from commands(child, (*prefix, name))

    assert not any("reset" in word and "card" in word for path in commands(build_parser()) for word in path)

    # Nothing an Agent reads or can call names it either: its Skills, its tools and its backends.
    named = [
        path
        for root in ("skills", ".agents/skills", "core/agent_core", "modules/agents")
        for path in Path(root).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt"}
        and re.search(r"reset[-_ ]cards?", path.read_text(encoding="utf-8", errors="ignore"), re.IGNORECASE)
    ]
    assert named == []


@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "session", "padding": "x" * 100_000},
        {"kind": "x" * 100_000},
        {"kind": "everything"},
        {"kind": ["session"]},
        {},
        ["session"],
        "session",
        None,
    ],
)
async def test_the_web_process_hands_the_controller_only_a_source_and_a_kind(monkeypatch, selection):
    """MH-QUOTA-062: A use body that is not exactly one known kind is refused in the Web process; nothing of it is sent to the controller."""

    client, requests = _controller_client(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(ModelHubError) as refused:
        await client.use_reset_card("src_claude0001", selection)
    assert (refused.value.status, refused.value.code) == (400, "invalid_parameter")
    assert requests == []


def _controller_route(monkeypatch, service):
    from unittest.mock import MagicMock

    from core import internal_server

    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    controller = MagicMock()
    controller.model_hub_service = service
    transport = httpx.ASGITransport(app=internal_server.create_app(controller))
    return httpx.AsyncClient(transport=transport, base_url="http://controller")


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"source_id": "src_claude0001", "selection": {"kind": "session", "padding": "x" * 100_000}}),
        json.dumps({"source_id": "s" * 100_000, "selection": {"kind": "session"}}),
        json.dumps({"source_id": "src_claude0001", "selection": {"kind": "session"}, "extra": 1}),
        json.dumps({"source_id": "src_claude0001"}),
        json.dumps([{"source_id": "src_claude0001", "selection": {"kind": "session"}}]),
        "{" * 900,
    ],
)
async def test_the_controller_reads_a_use_request_bounded_and_exact(monkeypatch, body):
    """MH-QUOTA-062: The controller reads a use request up to a small limit and takes only the exact shape; anything else never reaches the service."""

    service = AsyncMock()
    async with _controller_route(monkeypatch, service) as client:
        response = await client.post(
            "/internal/model-hub-ui/reset-card", content=body, headers={"Content-Type": "application/json"}
        )
    assert (response.status_code, response.json()) == (400, {"ok": False, "error": "invalid_parameter"})
    service.use_reset_card.assert_not_called()


async def test_a_use_travels_from_the_web_client_to_the_service_once_and_comes_back_closed(tmp_path, monkeypatch):
    """MH-QUOTA-062: The private route carries one use to the controller's service and its closed answer back; the vendor is not called on the controller's loop and `_mutation_lock` is free while it is."""

    from core import internal_server
    from vibe import internal_client, model_hub_client

    account = _Account({"weekly": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    controller = MagicMock()
    controller.model_hub_service = service
    app = internal_server.create_app(controller)

    async def endpoint(_socket_path):
        return SimpleNamespace(base_url="http://controller", headers={}, descriptor=None)

    monkeypatch.setattr(internal_client, "_resolve_endpoint_async", endpoint)
    monkeypatch.setattr(internal_client, "_async_transport", lambda _endpoint: httpx.ASGITransport(app=app))

    loop_thread = threading.get_ident()
    disk_threads: set[int] = set()
    write_durable, read = card_fences.write_durable, card_fences._read

    def recorded_write(path, data):
        disk_threads.add(threading.get_ident())
        return write_durable(path, data)

    def recorded_read(path):
        disk_threads.add(threading.get_ident())
        return read(path)

    monkeypatch.setattr(card_fences, "write_durable", recorded_write)
    monkeypatch.setattr(card_fences, "_read", recorded_read)
    at_vendor, answer = asyncio.Event(), asyncio.Event()

    async def slow_vendor(kind):
        at_vendor.set()
        await answer.wait()
        return _spend(account, kind)(kind)

    account.reply = slow_vendor
    press = asyncio.create_task(model_hub_client.ModelHubRemoteService().use_reset_card(CLAUDE.source_id, {"kind": "weekly"}))
    await asyncio.wait_for(at_vendor.wait(), 2)
    # While the vendor has the request, other Model Hub changes proceed.
    assert not service._mutation_lock.locked()

    async def change_something_else():
        async with service._mutation_lock:
            pass

    await asyncio.wait_for(change_something_else(), 1)
    answer.set()
    result = await press
    assert result["use"] == dict(kind="weekly", outcome="used", reason=None, cleared=[])
    assert len(account.uses) == 1 and _lock(result["quota"]) is None
    assert disk_threads and loop_thread not in disk_threads


async def test_reading_the_summary_asks_no_vendor_who_an_account_is_and_shows_an_unnamed_lock_as_locked(
    tmp_path, monkeypatch
):
    """MH-QUOTA-063: A summary puts locks on rows from what it already read: it makes no identity call, and while a row's account is not known, or the fence cannot be read, the row shows locked."""

    directory = tmp_path / "fences"
    CardFences(directory).claim(ACCOUNT, "weekly", NOW)
    account = _Account({"weekly": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE, CHATGPT], fences=CardFences(directory))
    reader = account.quota

    async def per_source(source_id, vendor, credential_ref):
        parsed = await reader(source_id, vendor, credential_ref)
        return {**parsed, "reset_card_account": ACCOUNT if source_id == CLAUDE.source_id else OTHER_ACCOUNT}

    service.adapter.subscription_quota = per_source
    clock = [service.now()]
    service.now = lambda: clock[0]

    # Nothing could be read yet: neither row's account is known, so both show locked.
    account.read_failure = SubscriptionQuotaError("unavailable")
    rows = (await service.quota_summary())["sources"]
    assert [(row["reset_cards"]["state"], row["reset_cards"]["lock"]["reason"]) for row in rows] == [
        ("unread", "unconfirmed"),
        ("unread", "unconfirmed"),
    ]
    assert [row["reset_cards"]["lock"]["kind"] for row in rows] == ["all", "account"]

    # Once read, the lock is on the account that holds it and nowhere else.
    account.read_failure = None
    clock[0] += timedelta(minutes=6)
    rows = (await service.quota_summary())["sources"]
    assert rows[0]["reset_cards"]["lock"] == {"kind": "weekly", "since": "2026-11-20T00:00:00Z", "reason": "unconfirmed"}
    assert rows[1]["reset_cards"]["lock"] is None
    assert account.identity_reads == 0
    assert ACCOUNT not in json.dumps(rows) and OTHER_ACCOUNT not in json.dumps(rows)

    # A fence that cannot be read locks every account's cards, and a press sends nothing.
    (directory / "fences.json").write_text('{"version":1,"locks":{},"locks":{}}')
    (tmp_path / "again").mkdir()
    damaged = _hub(tmp_path / "again", monkeypatch, account, [CLAUDE, CHATGPT], fences=CardFences(directory))
    damaged.adapter.subscription_quota = per_source
    rows = (await damaged.quota_summary())["sources"]
    assert [row["reset_cards"]["lock"]["reason"] for row in rows] == ["unconfirmed", "unconfirmed"]
    with pytest.raises(ModelHubError) as refused:
        await damaged.use_reset_card(CHATGPT.source_id, {"kind": "account"})
    assert refused.value.code == "reset_card_locked" and account.uses == []


# ── A state file that fails open ────────────────────────────────────────────


def test_a_claim_that_is_not_confirmed_on_disk_sends_nothing(tmp_path, monkeypatch):
    """MH-QUOTA-064: A claim counts only once its row and its directory entry are flushed; when that cannot be confirmed the claim is refused, and what may have reached the disk only ever locks."""

    from vibe.model_hub_runtime import journal

    directory = tmp_path / "fences"
    published: list[object] = []
    replace, flush = journal._replace, journal.flush_directory

    def recorded_replace(source, target):
        replace(source, target)
        published.append(target)

    def unconfirmed_flush(path):
        if published:
            raise OSError("private-marker")
        flush(path)

    monkeypatch.setattr(journal, "_replace", recorded_replace)
    monkeypatch.setattr(journal, "flush_directory", unconfirmed_flush)
    fences = CardFences(directory)
    with pytest.raises(FenceUnavailable) as refused:
        fences.claim(ACCOUNT, "session", NOW)
    assert "private-marker" not in str(refused.value)
    # The row was renamed into place before the flush failed. This process does not
    # count it, and a later process reads it as an unknown outcome, never as free.
    assert published and fences.view().locks == {}
    assert CardFences(directory).view().locks[ACCOUNT]["reason"] == "unconfirmed"


async def test_a_press_whose_claim_cannot_be_made_durable_reaches_no_vendor(tmp_path, monkeypatch):
    """MH-QUOTA-064: When the claim cannot be confirmed on disk the press is refused before the adapter's use is called."""

    from vibe.model_hub_runtime import journal

    account = _Account({"session": 1})
    service = _hub(tmp_path, monkeypatch, account, [CLAUDE])

    def no_flush(_path):
        raise OSError("private-marker")

    monkeypatch.setattr(journal, "flush_directory", no_flush)
    with pytest.raises(ModelHubError) as refused:
        await service.use_reset_card(CLAUDE.source_id, {"kind": "session"})
    assert (refused.value.status, refused.value.code) == (409, "reset_card_locked")
    assert account.uses == []


_REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_ROW = {"request_id": _REQUEST_ID, "kind": "account", "since": "2026-11-20T00:00:00Z", "reason": "in_flight"}


def _document(locks=None, **changes):
    return json.dumps({"version": 1, "locks": {ACCOUNT: _ROW} if locks is None else locks, **changes})


def _write_private(path, text):
    private_fs.ensure_private_directory(path.parent)
    path.write_text(text)
    private_fs.make_private_file(path)


def _document_cases():
    return {
        "truncated": '{"version":1,"locks":{"' + ACCOUNT + '":',
        "not-an-object": "[]",
        "not-utf8": b"\xff\xfe\x00",
        "repeated-top-level-key": '{"version":1,"locks":{},"locks":{}}',
        "repeated-account": '{"version":1,"locks":{"%s":%s,"%s":%s}}' % (ACCOUNT, json.dumps(_ROW), ACCOUNT, json.dumps(_ROW)),
        "repeated-row-key": '{"version":1,"locks":{"%s":{"request_id":"%s","kind":"account","kind":"fable","since":"2026-11-20T00:00:00Z","reason":"unconfirmed"}}}'
        % (ACCOUNT, _REQUEST_ID),
        "short-account": _document({ACCOUNT[:-1]: _ROW}),
        "uppercase-account": _document({"A" * 64: _ROW}),
        "boolean-version": _document(version=True),
        "newer-version": _document(version=2),
        "unknown-field": _document(extra=0),
        "missing-locks": json.dumps({"version": 1}),
        "locks-not-an-object": json.dumps({"version": 1, "locks": [_ROW]}),
        "row-not-an-object": _document({ACCOUNT: "unconfirmed"}),
        "unknown-row-field": _document({ACCOUNT: {**_ROW, "grant": "private-marker"}}),
        "missing-row-field": _document({ACCOUNT: {key: value for key, value in _ROW.items() if key != "since"}}),
        "unknown-kind": _document({ACCOUNT: {**_ROW, "kind": "other"}}),
        "unknown-reason": _document({ACCOUNT: {**_ROW, "reason": "settled"}}),
        "malformed-request-id": _document({ACCOUNT: {**_ROW, "request_id": "c" * 32}}),
        "non-string-field": _document({ACCOUNT: {**_ROW, "since": 0}}),
        "time-without-zone": _document({ACCOUNT: {**_ROW, "since": "2026-11-20T00:00:00"}}),
        "time-not-canonical": _document({ACCOUNT: {**_ROW, "since": "2026-11-20T08:00:00+08:00"}}),
        "oversized": _document() + " " * (64 * 1024),
        "nested-too-deep": "[" * 20_000,
    }


@pytest.mark.parametrize("damage", sorted(_document_cases()))
def test_a_fence_document_that_is_not_exactly_the_known_shape_locks_every_account(tmp_path, damage):
    """MH-QUOTA-065: Corruption, an unknown or repeated field, a key or value outside its grammar and an oversized document leave the whole store locked and untouched."""

    content = _document_cases()[damage]
    path = tmp_path / "fences" / "fences.json"
    _write_private(path, "")
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    fences = CardFences(path.parent)
    assert fences.view().damaged
    for account in (ACCOUNT, OTHER_ACCOUNT):
        with pytest.raises(FenceUnavailable) as refused:
            fences.claim(account, "session", NOW)
        assert "private-marker" not in str(refused.value)
    assert fences.unknown_floor() is None
    fences.release_after_readings([(ACCOUNT, 10**9)])
    assert path.read_bytes() == (content if isinstance(content, bytes) else content.encode())


def _unsafe_entries():
    cases = ["wide-file", "wide-directory", "directory-in-place-of-file", "hard-link"]
    if hasattr(os, "symlink"):
        cases += ["link-to-a-valid-file", "dangling-link", "linked-directory"]
    if hasattr(os, "mkfifo"):
        cases.append("fifo")
    return cases


@pytest.mark.parametrize("entry", _unsafe_entries())
def test_a_fence_entry_that_is_not_this_users_private_file_locks_every_account(tmp_path, entry):
    """MH-QUOTA-065: A link, a special file, a second name or permissions beyond this user are never read as "no claims" and never followed or replaced."""

    if os.name == "nt" and entry in {"wide-file", "wide-directory"}:
        pytest.skip("permission bits are not Windows access rules; private_fs owns the DACL check there")
    directory = tmp_path / "fences"
    path = directory / "fences.json"
    elsewhere = tmp_path / "elsewhere.json"
    _write_private(elsewhere, json.dumps({"version": 1, "locks": {}}))
    if entry == "linked-directory":
        real = tmp_path / "real"
        _write_private(real / "fences.json", json.dumps({"version": 1, "locks": {}}))
        try:
            directory.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("links cannot be created here")
    else:
        private_fs.ensure_private_directory(directory)
    try:
        if entry == "wide-file":
            path.write_text(json.dumps({"version": 1, "locks": {}}))
            os.chmod(path, 0o644)
        elif entry == "wide-directory":
            _write_private(path, json.dumps({"version": 1, "locks": {}}))
            os.chmod(directory, 0o755)
        elif entry == "directory-in-place-of-file":
            path.mkdir()
        elif entry == "hard-link":
            os.link(elsewhere, path)
        elif entry == "link-to-a-valid-file":
            path.symlink_to(elsewhere)
        elif entry == "dangling-link":
            path.symlink_to(tmp_path / "missing.json")
        elif entry == "fifo":
            os.mkfifo(path, 0o600)
    except OSError:
        pytest.skip("this entry cannot be created here")
    before = elsewhere.read_bytes()

    outcome: list[object] = []

    def use_store():
        fences = CardFences(directory)
        try:
            fences.claim(ACCOUNT, "session", NOW)
        except FenceUnavailable:
            outcome.append(fences.view().damaged)

    # A special file must not hold the caller either.
    worker = threading.Thread(target=use_store, daemon=True)
    worker.start()
    worker.join(5)
    assert outcome == [True]
    assert elsewhere.read_bytes() == before and not (tmp_path / "missing.json").exists()


def test_a_store_that_was_never_written_is_empty_and_a_claim_creates_it_private(tmp_path):
    """MH-QUOTA-065: Only a file that was never created reads as "no claims"; the first claim creates a private directory and file."""

    directory = tmp_path / "state" / "fences"
    fences = CardFences(directory)
    assert not fences.view().locked and not directory.exists()
    request_id = fences.claim(ACCOUNT, "weekly", NOW)
    with pytest.raises(FenceUnavailable):
        fences.claim(ACCOUNT, "session", NOW)
    assert private_fs.is_private(directory, directory.lstat().st_mode)
    assert private_fs.is_private(fences.path, fences.path.lstat().st_mode)
    assert sorted(entry.name for entry in directory.iterdir()) == ["fences.json"]
    assert fences.settle(ACCOUNT, request_id, unknown=False, mark=1) is True
    assert json.loads(fences.path.read_text()) == {"version": 1, "locks": {}}
