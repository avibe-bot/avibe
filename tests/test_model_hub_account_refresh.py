"""A paused account and the user's refresh: the vendor's current quota decides, without a restart."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from config.v2_config import ModelHubSourceStateConfig
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.quota import SubscriptionQuotaError
from core.handlers.model_hub.retry import RECOVERY_EXHAUSTED_CODE
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import NOW, _outcome, _source
from tests.test_model_hub_retry_policy import clock_service

SOURCE_ID = "src_account01"
RESET_SECONDS = 5 * 3600
PAUSED = "models.source.cooldown.quota_exhausted"


def _window(kind: str, used_pct: float, *, resets_in: int = RESET_SECONDS) -> dict:
    return {
        "id": kind, "kind": kind, "label": kind, "used_pct": used_pct,
        "window_seconds": RESET_SECONDS, "resets_at": (NOW + timedelta(seconds=resets_in)).isoformat(),
        **({"scope_model": "Fable"} if kind == "model_weekly" else {}),
    }


def _capped(code: str = "usage_limit_reached"):
    return replace(
        _outcome(RawOutcomeKind.HTTP_ERROR, status=429, code=code, source_id=SOURCE_ID),
        retry_after=str(RESET_SECONDS), response_received_at=NOW,
    )


def _capped_account(tmp_path, vendor: str, protocol: str, *, code: str = "usage_limit_reached"):
    """An account whose first request met the vendor's cap; the vendor serves again afterwards."""

    account = replace(
        _source(SOURCE_ID, "Account", vendor=vendor, protocol=protocol), kind="subscription", billing="monthly",
    )
    return clock_service(tmp_path, sources=[account], outcomes=[
        _capped(code), _outcome(RawOutcomeKind.SUCCESS, source_id=SOURCE_ID, stream_started=True),
    ])


def _vendor_reports(service, windows) -> None:
    async def vendor_quota(_source_id, _vendor, _credential_ref):
        return {"plan": None, "windows": windows}

    service.adapter.subscription_quota = vendor_quota


async def _request(service):
    return await service.resolve_with_recovery(backend="codex", model_id="shared-model", request={})


async def _refused(service) -> None:
    with pytest.raises(ModelHubError) as ended:
        await _request(service)
    assert ended.value.code == RECOVERY_EXHAUSTED_CODE


def _state(service):
    state = service.store.load().sources[0].state
    return state.status, state.detail_key


@pytest.mark.parametrize("vendor,protocol", [("anthropic", "anthropic"), ("openai", "openai_responses")])
def test_refresh_resumes_a_paused_account_once_the_vendor_reports_quota(tmp_path, vendor, protocol):
    """MH-QUOTA-031: reset on the vendor's side, refresh, and the next request goes through."""

    async def run():
        service, _clock = _capped_account(tmp_path, vendor, protocol)
        reads = []

        async def vendor_quota(source_id, read_vendor, _credential_ref):
            reads.append((source_id, read_vendor))
            return {"plan": None, "windows": [_window("session", 3.0), _window("weekly", 41.0)]}

        service.adapter.subscription_quota = vendor_quota
        await _refused(service)
        assert _state(service) == ("cooldown", PAUSED)
        # The vendor has given the quota back, but nothing has told the Model Hub:
        # the account stays paused and no request reaches the vendor.
        await _refused(service)
        assert len(service.adapter.invocations) == 1

        # Opening the page reads quota without touching the pause; only the refresh the user presses does.
        await service.quota_summary()
        assert _state(service) == ("cooldown", PAUSED)
        summary = await service.quota_summary(force=True)

        assert reads == [(SOURCE_ID, vendor)]
        assert summary["sources"][0]["state"] == "ok"
        assert _state(service) == ("standby", None)
        assert service.list_sources()[0]["state"]["status"] == "standby"
        result = await _request(service)
        assert result.source_id == SOURCE_ID and result.outcome.kind is RawOutcomeKind.SUCCESS
        assert len(service.adapter.invocations) == 2
        recovered = service.events.list()[0]
        assert (recovered["kind"], recovered["agent"], recovered["to_source"]) == ("recover", "system", SOURCE_ID)

    asyncio.run(run())


@pytest.mark.parametrize("reading,resumed", [
    # The vendor still reports the shared window used up: the pause and its reset time stay.
    ([_window("session", 100.0), _window("weekly", 41.0)], False),
    ([_window("session", 12.0), _window("weekly", 100.0)], False),
    # A used-up window that names no reset time is still used up.
    ([{**_window("session", 100.0), "resets_at": None}], False),
    # A used-up window whose reset has passed has given its quota back.
    ([_window("session", 100.0, resets_in=-60), _window("weekly", 41.0)], True),
    # One model's own limit never pauses the whole account.
    ([_window("session", 12.0), _window("model_weekly", 100.0)], True),
    # Where the vendor's quota cannot be read, the next request asks the vendor.
    (SubscriptionQuotaError("unsupported"), True),
    (SubscriptionQuotaError("rate_limited", retry_after_seconds=600), True),
    (SubscriptionQuotaError("unavailable"), True),
    # A sign-in that has expired needs the sign-in, not a request.
    (SubscriptionQuotaError("auth_expired"), False),
], ids=["session-used-up", "week-used-up", "no-reset-time", "reset-passed", "model-limit", "unreadable-vendor", "read-limited", "read-failed", "signed-out"])
def test_refresh_keeps_the_pause_only_while_the_vendor_reports_it(tmp_path, reading, resumed):
    """MH-QUOTA-032: the vendor's current reading decides; an unreadable one lets the next request ask."""

    async def run():
        service, _clock = _capped_account(tmp_path, "anthropic", "anthropic")

        async def vendor_quota(_source_id, _vendor, _credential_ref):
            if isinstance(reading, Exception):
                raise reading
            return {"plan": None, "windows": reading}

        service.adapter.subscription_quota = vendor_quota
        await _refused(service)
        paused_until = service.store.load().sources[0].state.retry_at

        await service.quota_summary(force=True)

        if resumed:
            assert _state(service) == ("standby", None)
            assert (await _request(service)).outcome.kind is RawOutcomeKind.SUCCESS
            assert len(service.adapter.invocations) == 2
        else:
            state = service.store.load().sources[0].state
            assert (state.status, state.detail_key, state.retry_at) == ("cooldown", PAUSED, paused_until)
            await _refused(service)
            assert len(service.adapter.invocations) == 1

    asyncio.run(run())


def test_listing_models_leaves_a_paused_account_paused_where_requests_look(tmp_path):
    """MH-QUOTA-033: a model listing says nothing about quota, so the page and the requests agree."""

    async def run():
        service, _clock = _capped_account(tmp_path, "openai", "openai_responses")

        async def listed(_source, **_kwargs):
            return None

        service._discover = listed
        await _refused(service)

        await service.refresh_source(SOURCE_ID)

        # Before the fix the saved state read standby while every request was still refused.
        assert service.list_sources()[0]["state"]["status"] == "cooldown"
        assert _state(service) == ("cooldown", PAUSED)
        await _refused(service)
        assert len(service.adapter.invocations) == 1

    asyncio.run(run())


@pytest.mark.parametrize("shape", ["rate-limited", "saved-standby"])
def test_refresh_resumes_every_shape_of_a_quota_pause(tmp_path, shape):
    """MH-QUOTA-031: a rate pause, and a pause only the live deadline still holds, are lifted the same way."""

    async def run():
        service, _clock = _capped_account(
            tmp_path, "openai", "openai_responses",
            code="rate_limit_exceeded" if shape == "rate-limited" else "usage_limit_reached",
        )
        _vendor_reports(service, [_window("session", 3.0), _window("weekly", 41.0)])
        await _refused(service)
        if shape == "rate-limited":
            assert _state(service) == ("cooldown", "models.source.cooldown.rate_limited")
        else:
            # What a model listing left behind before it kept the pause, and what
            # an upgraded install can still hold: standby on disk, the deadline live.
            config = service.store.load()
            config.sources[0].state = ModelHubSourceStateConfig(status="standby")
            service.store.save(config)
            await _refused(service)
        assert len(service.adapter.invocations) == 1

        await service.quota_summary(force=True)

        assert _state(service) == ("standby", None)
        assert (await _request(service)).outcome.kind is RawOutcomeKind.SUCCESS
        assert len(service.adapter.invocations) == 2

    asyncio.run(run())


def test_a_refusal_from_before_the_refresh_cannot_pause_the_account_again(tmp_path):
    """MH-QUOTA-036: the refresh outranks every attempt admitted before it, as a verified response does."""

    async def run():
        service, _clock = _capped_account(tmp_path, "anthropic", "anthropic")
        _vendor_reports(service, [_window("session", 3.0), _window("weekly", 41.0)])
        await _refused(service)
        account = service.store.load().sources[0]
        # A request admitted while the account was paused, still in flight.
        in_flight = service._reserve_settlement_generation(SOURCE_ID)

        await service.quota_summary(force=True)
        # Its refusal, which the vendor gave before the reset, settles afterwards.
        await service._settle_fallback_source(
            account, ResolutionDecision("fallback", reason="quota_exhausted", cooldown_seconds=300),
            backend="codex", model_id="shared-model", settlement_generation=in_flight, outcome=_capped(),
        )

        assert _state(service) == ("standby", None)
        assert (await _request(service)).outcome.kind is RawOutcomeKind.SUCCESS

    asyncio.run(run())
