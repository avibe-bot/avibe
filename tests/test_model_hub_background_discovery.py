"""Background refresh of Hub Source model inventories at the service boundary."""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import timedelta

import pytest

from config.v2_config import ModelHubModelConfig, ModelHubSourceStateConfig
from core.handlers.model_hub.errors import ModelDiscoveryError
from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
from core.handlers.model_hub.service import UnavailableEngineAdapter
from tests.test_model_hub_resolution import FakeAdapter, _config, _service, _source
from tests.test_model_hub_retry_policy import Clock


MENU_MODEL = "claude-opus-4-6"
TICK = timedelta(minutes=5)


class ListingAdapter(FakeAdapter):
    """Records the credential of every listing it answers."""

    def __init__(self, discovered: tuple[str, ...] = (MENU_MODEL,)):
        super().__init__(discovered)
        self.listed: list[str] = []

    async def discover_models(self, vendor, protocol, base_url, credential_ref, *, start_engine=True):
        self.listed.append(credential_ref)
        self.started_engine = getattr(self, "started_engine", False) or start_engine
        return await super().discover_models(vendor, protocol, base_url, credential_ref)


def _hub_source(source_id: str, model_ids=(MENU_MODEL,), **kwargs):
    return _source(source_id, model_ids, credential_ref=f"cred_{source_id}", **kwargs)


def _listed(source, clock: Clock, *, hours_ago: float):
    source.last_discovered_at = (clock.now() - timedelta(hours=hours_ago)).isoformat()
    return source


def _background(tmp_path, sources, adapter=None):
    clock = Clock()
    service, store, adapter = _service(tmp_path, _config(sources), adapter or ListingAdapter())
    service.now = clock.now
    return service, store, adapter, clock


def _model_ids(store, index=0):
    return [model.id for model in store.load().sources[index].models]


def _refresh(service):
    asyncio.run(service._refresh_due_sources())


@pytest.mark.parametrize(
    ("listed_hours_ago", "refreshed"),
    [(None, True), (6 + 1 / 60, True), (1, False)],
    ids=["never-listed", "stale-at-startup", "fresh"],
)
def test_due_sources_gain_new_models_and_fresh_sources_are_left_alone(
    tmp_path, listed_hours_ago, refreshed,
):
    """MH-DISCOVERY-SCHEDULE-001: a Source not listed within the interval is refreshed in the background; a fresh one is not."""

    clock = Clock()
    source = _hub_source("src_sched001")
    if listed_hours_ago is not None:
        _listed(source, clock, hours_ago=listed_hours_ago)
    service, store, adapter, clock = _background(
        tmp_path, [source], ListingAdapter((MENU_MODEL, "claude-new-model")),
    )
    before = store.load().sources[0].last_discovered_at

    _refresh(service)

    persisted = store.load().sources[0]
    # A background listing never starts the engine.
    assert getattr(adapter, "started_engine", False) is False
    if refreshed:
        assert adapter.listed == ["cred_src_sched001"]
        assert _model_ids(store) == [MENU_MODEL, "claude-new-model"]
        assert persisted.last_discovered_at == clock.now().isoformat()
    else:
        assert adapter.listed == []
        assert _model_ids(store) == [MENU_MODEL]
        assert persisted.last_discovered_at == before


def test_background_failure_keeps_source_untouched_and_backs_off_until_a_success(tmp_path, caplog):
    source = _listed(_hub_source("src_backoff1"), Clock(), hours_ago=7)
    service, store, adapter, clock = _background(tmp_path, [source])
    adapter.discovery_error = ModelDiscoveryError("upstream echoed sk-live-secret")
    before = copy.deepcopy(store.load().to_payload())

    def attempts_after(minutes: int) -> int:
        clock.advance(minutes * 60)
        listed = len(adapter.listed)
        _refresh(service)
        return len(adapter.listed) - listed

    with caplog.at_level(logging.WARNING, logger="core.handlers.model_hub.service"):
        assert attempts_after(0) == 1
        for delay in (15, 30, 60, 120, 240, 360, 360):
            assert attempts_after(delay - 1) == 0
            assert attempts_after(1) == 1

    assert store.load().to_payload() == before
    assert service.list_events() == []
    failures = [record.getMessage() for record in caplog.records]
    assert len(failures) == 8
    assert all("src_backoff1" in message and "discovery_failed" in message for message in failures)
    assert not any("sk-live-secret" in message for message in failures)

    adapter.discovery_error = None
    adapter.discovered = (MENU_MODEL, "claude-new-model")
    assert attempts_after(360) == 1
    assert _model_ids(store) == [MENU_MODEL, "claude-new-model"]

    # A success resets the backoff: the next failure retries after the base delay.
    adapter.discovery_error = ModelDiscoveryError("down again")
    assert attempts_after(int(7.2 * 60)) == 1
    assert attempts_after(14) == 0
    assert attempts_after(1) == 1


def test_background_listing_without_an_engine_fails_as_engine_down(tmp_path, caplog):
    """MH-DISCOVERY-SCHEDULE-001: the fail-closed adapter answers a background listing as an engine failure."""

    service, store, _, _ = _background(tmp_path, [_hub_source("src_noengine")], UnavailableEngineAdapter())

    with caplog.at_level(logging.WARNING, logger="core.handlers.model_hub.service"):
        _refresh(service)

    assert [record.getMessage() for record in caplog.records] == [
        "Model Hub background discovery failed for Source src_noengine (engine_down); retrying in 15 minutes"
    ]
    assert _model_ids(store) == [MENU_MODEL]


@pytest.mark.parametrize(
    "state",
    [
        ModelHubSourceStateConfig(
            status="cooldown",
            retry_at="2099-01-01T00:00:00+00:00",
            detail_key="models.source.cooldown.rate_limited",
        ),
        ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.balance_exhausted",
        ),
        ModelHubSourceStateConfig(
            status="error",
            detail_key="models.source.error.unclassified",
        ),
    ],
    ids=lambda state: state.status,
)
def test_background_success_keeps_a_blocked_source_state(tmp_path, state):
    source = _listed(_hub_source("src_blocked1"), Clock(), hours_ago=7)
    source.state = copy.deepcopy(state)
    service, store, _, _ = _background(
        tmp_path, [source], ListingAdapter((MENU_MODEL, "claude-new-model")),
    )

    _refresh(service)

    assert _model_ids(store) == [MENU_MODEL, "claude-new-model"]
    assert store.load().sources[0].state == state
    assert service.list_events() == []


def test_background_refresh_never_removes_a_model_or_route_hop(tmp_path):
    source = _listed(_hub_source("src_addonly1", (MENU_MODEL, "claude-old-model")), Clock(), hours_ago=7)
    source.models.append(ModelHubModelConfig(id="claude-manual-model", provenance="manual"))
    service, store, adapter, _ = _background(
        tmp_path, [source], ListingAdapter(("claude-new-model",)),
    )
    hops = store.load().agents["claude"].routes[MENU_MODEL].hops

    _refresh(service)

    assert adapter.listed == ["cred_src_addonly1"]
    assert _model_ids(store) == [
        MENU_MODEL, "claude-old-model", "claude-new-model", "claude-manual-model",
    ]
    assert store.load().agents["claude"].routes[MENU_MODEL].hops == hops


@pytest.mark.parametrize("upstream_order", ["same", "reversed"])
def test_unchanged_listing_writes_nothing_and_still_counts_as_a_listing(tmp_path, upstream_order):
    inventory = (MENU_MODEL, "claude-other-model")
    service, store, adapter, clock = _background(
        tmp_path, [_hub_source("src_nochange", ())], ListingAdapter(inventory),
    )
    # The inventory exactly as a completed discovery leaves it.
    asyncio.run(service.refresh_source("src_nochange"))
    clock.advance(timedelta(hours=7).total_seconds())
    adapter.listed.clear()
    adapter.synced.clear()
    if upstream_order == "reversed":
        adapter.discovered = tuple(reversed(inventory))
    before = copy.deepcopy(store.load().to_payload())
    saves = []
    save = store.save
    store.save = lambda config: (saves.append(config), save(config))

    _refresh(service)
    # Past every jittered period counted from the persisted timestamp, but
    # within the shortest one counted from the unchanged listing.
    clock.advance(timedelta(hours=2).total_seconds())
    _refresh(service)

    assert adapter.listed == ["cred_src_nochange"]
    assert saves == []
    assert adapter.synced == []
    assert store.load().to_payload() == before


def _replace_credential(source):
    source.credential_ref = "cred_replaced"


def _retarget(source):
    source.base_url = "https://new-relay.example/v1"


def _switch_protocol(source):
    source.protocol = "openai_chat"


def _await_verification(source):
    source.verification_pending = "vp_" + "0" * 32


def _land_manual_refresh(source):
    source.last_discovered_at = "2026-07-29T12:00:01+00:00"


@pytest.mark.parametrize(
    "change",
    [_replace_credential, _retarget, _switch_protocol, _await_verification, _land_manual_refresh, None],
    ids=["credential", "base-url", "protocol", "verification", "manual-refresh", "deleted"],
)
def test_listing_for_a_source_changed_during_discovery_is_dropped(tmp_path, change):
    source = _listed(_hub_source("src_identity", vendor="custom"), Clock(), hours_ago=7)
    source.base_url = "https://old-relay.example/v1"
    service, store, adapter, _ = _background(
        tmp_path, [source], ListingAdapter((MENU_MODEL, "claude-new-model")),
    )

    async def run():
        adapter.discovery_started = asyncio.Event()
        adapter.discovery_block = asyncio.Event()
        refresh = asyncio.create_task(service._refresh_due_sources())
        await adapter.discovery_started.wait()
        changed = service._clone_config(store.load())
        if change is None:
            changed.sources.clear()
        else:
            change(changed.sources[0])
        store.config = changed
        adapter.discovery_block.set()
        await refresh
        return changed.to_payload()

    expected = asyncio.run(run())

    assert adapter.listed == ["cred_src_identity"]
    assert store.load().to_payload() == expected
    assert adapter.synced == []


@pytest.mark.parametrize(("enabled", "listed"), [(True, {"cred_src_apikey1", "cred_src_oauth01"}), (False, set())])
def test_schedule_covers_hub_sources_and_skips_native_and_waiting_credentials(tmp_path, enabled, listed):
    clock = Clock()
    api_key = _listed(_hub_source("src_apikey1"), clock, hours_ago=7)
    oauth = _listed(_hub_source("src_oauth01", ("gpt-5.5",), kind="subscription", vendor="openai"), clock, hours_ago=7)
    expired = _listed(_hub_source("src_expired", ("gpt-5.5",), kind="subscription", vendor="openai"), clock, hours_ago=7)
    expired.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.oauth_expired",
    )
    native = _source("src_native01", (MENU_MODEL,), kind="subscription", channel="native_cli", credential_ref=None)
    service, store, adapter, _ = _background(
        tmp_path, [api_key, oauth, expired, native], ListingAdapter((MENU_MODEL, "gpt-5.5")),
    )
    store.load().enabled = enabled

    _refresh(service)

    assert set(adapter.listed) == listed
    assert len(adapter.listed) == len(listed)


def test_stale_sources_catch_up_together_then_refresh_at_different_times(tmp_path):
    clock = Clock()
    source_ids = [f"src_jitter{index:02d}" for index in range(6)]
    credentials = sorted(f"cred_{source_id}" for source_id in source_ids)
    sources = [_listed(_hub_source(source_id), clock, hours_ago=6 + 1 / 60) for source_id in source_ids]
    service, _, adapter, clock = _background(tmp_path, sources)

    _refresh(service)

    assert sorted(adapter.listed) == credentials
    adapter.listed.clear()
    start = clock.now()
    refreshed_at = {}

    elapsed = timedelta(0)
    while elapsed <= timedelta(hours=7.2) + TICK:
        _refresh(service)
        for credential_ref in adapter.listed:
            refreshed_at.setdefault(credential_ref, clock.now() - start)
        adapter.listed.clear()
        clock.advance(TICK.total_seconds())
        elapsed += TICK

    assert sorted(refreshed_at) == credentials
    assert all(
        timedelta(hours=4.8) <= offset <= timedelta(hours=7.2) + TICK
        for offset in refreshed_at.values()
    )
    assert len(set(refreshed_at.values())) > 1


def test_listing_that_would_strand_a_menu_model_is_left_to_manual_refresh(tmp_path):
    clock = Clock()
    healthy = _listed(_hub_source("src_healthy1", ("claude-other-a",)), clock, hours_ago=1)
    blocked = _listed(_hub_source("src_blocked2", ("claude-other-b",)), clock, hours_ago=7)
    blocked.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.balance_exhausted",
    )
    service, store, adapter, _ = _background(
        tmp_path, [healthy, blocked], ListingAdapter(("claude-other-b", MENU_MODEL)),
    )
    # Without an exact Route the menu model passes through to the healthy key.
    # Listing it on the blocked Source alone would narrow it onto that Source.
    store.load().agents["claude"].routes.pop(MENU_MODEL)
    before = copy.deepcopy(store.load().to_payload())

    _refresh(service)

    assert adapter.listed == ["cred_src_blocked2"]
    assert store.load().to_payload() == before
    assert adapter.synced == []


@pytest.mark.parametrize("read", ["list_sources", "agent_model_candidates"])
def test_viewing_inventory_lists_sources_older_than_thirty_minutes_once(tmp_path, read):
    """MH-DISCOVERY-SCHEDULE-001: a viewer's read lists Sources older than 30 minutes, once, in the background."""

    clock = Clock()
    stale = _listed(_hub_source("src_viewed01"), clock, hours_ago=1)
    fresh = _listed(_hub_source("src_viewed02"), clock, hours_ago=0.25)
    # Also due for the schedule, which may list it while a viewer's pass runs.
    overdue = _listed(_hub_source("src_viewed03"), clock, hours_ago=7)
    class HeldListingAdapter(ListingAdapter):
        """Holds every listing open so repeated reads overlap it."""

        released: asyncio.Event

        async def discover_models(self, vendor, protocol, base_url, credential_ref, *, start_engine=True):
            self.listed.append(credential_ref)
            await self.released.wait()
            return await FakeAdapter.discover_models(self, vendor, protocol, base_url, credential_ref)

    service, store, adapter, clock = _background(
        tmp_path, [stale, fresh, overdue], HeldListingAdapter((MENU_MODEL, "claude-new-model")),
    )
    payload = {"backend": "claude"} if read == "agent_model_candidates" else {}

    async def run():
        adapter.released = asyncio.Event()
        # No schedule, no listing: a stopped runtime is never woken by a read.
        await dispatch_model_hub_rpc(service, read, payload)
        await asyncio.sleep(0.05)
        assert adapter.listed == []
        service.start_background_discovery()
        try:
            scheduled = asyncio.create_task(service._refresh_due_sources())
            for _ in range(3):
                await dispatch_model_hub_rpc(service, read, payload)
                await asyncio.sleep(0.02)
            adapter.released.set()
            await scheduled
            for _ in range(100):
                if _model_ids(store, 0) != [MENU_MODEL] and _model_ids(store, 2) != [MENU_MODEL]:
                    break
                await asyncio.sleep(0.01)
        finally:
            adapter.released.set()
            await asyncio.wait_for(service.stop(), 2)

    asyncio.run(run())

    assert sorted(adapter.listed) == ["cred_src_viewed01", "cred_src_viewed03"]
    assert _model_ids(store, 0) == [MENU_MODEL, "claude-new-model"]
    assert _model_ids(store, 1) == [MENU_MODEL]
    assert _model_ids(store, 2) == [MENU_MODEL, "claude-new-model"]


class _HeldAdapter(ListingAdapter):
    """Holds each listing open until released."""

    released: asyncio.Event

    async def discover_models(self, vendor, protocol, base_url, credential_ref, *, start_engine=True):
        self.listed.append(credential_ref)
        await self.released.wait()
        return await FakeAdapter.discover_models(self, vendor, protocol, base_url, credential_ref)


def test_stop_retires_the_background_schedule_without_waiting_for_a_listing(tmp_path):
    """MH-DISCOVERY-SCHEDULE-001: shutdown retires a listing that upstream is still holding."""

    service, _, adapter, _ = _background(tmp_path, [_hub_source("src_lifecyc1")], _HeldAdapter())

    async def run():
        adapter.released = asyncio.Event()
        service.start_background_discovery()
        service.request_inventory_refresh()
        for _ in range(100):
            if adapter.listed:
                break
            await asyncio.sleep(0.01)
        assert adapter.listed == ["cred_src_lifecyc1"]
        # The listing is never released: shutdown must not wait for upstream.
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(service.stop(), 30)
        elapsed = loop.time() - started
        return elapsed, [
            task for task in asyncio.all_tasks()
            if task.get_name() in {"model-hub-background-discovery", "model-hub-viewed-discovery"}
            and not task.done()
        ]

    elapsed, running = asyncio.run(run())
    # Well inside the controller's 10-second runtime-work shutdown grace.
    assert elapsed < 1
    assert running == []


def test_runtime_stopped_mid_pass_lists_no_further_source(tmp_path):
    """MH-DISCOVERY-SCHEDULE-001: a runtime stopped during a pass is not listed again by it."""

    first, second = _hub_source("src_midpass1"), _hub_source("src_midpass2")
    service, store, adapter, _ = _background(tmp_path, [first, second], _HeldAdapter())

    async def run():
        adapter.released = asyncio.Event()
        passing = asyncio.create_task(service._refresh_due_sources())
        for _ in range(100):
            if adapter.listed:
                break
            await asyncio.sleep(0.01)
        config = store.load()
        config.enabled = False
        store.save(config)
        adapter.released.set()
        await asyncio.wait_for(passing, 1)

    asyncio.run(run())

    assert len(adapter.listed) == 1
