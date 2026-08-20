from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import itertools
import threading
import time
from typing import Any

import pytest

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from tests.ui_server_test_helpers import csrf_headers
from storage import remote_access_authorization_service
from storage.lock import MigrationLockTimeout
from vibe import remote_access, ui_server
from vibe.authorization import context_from_session_payload
from vibe.sse_broker import broker
from vibe.ui_compat import g
from vibe.ui_server import app


@pytest.fixture(autouse=True)
def _clear_authorization_refresh_process_state():
    with remote_access._AUTHORIZATION_REFRESH_LOCK:
        remote_access._AUTHORIZATION_REFRESH_FAILURES.clear()
        remote_access._AUTHORIZATION_REFRESH_RESULTS.clear()
        remote_access._AUTHORIZATION_REFRESH_FLIGHTS.clear()
    with remote_access._AUTHORIZATION_BACKGROUND_REFRESH_LOCK:
        remote_access._AUTHORIZATION_BACKGROUND_REFRESHES.clear()
    yield
    with remote_access._AUTHORIZATION_REFRESH_LOCK:
        remote_access._AUTHORIZATION_REFRESH_FAILURES.clear()
        remote_access._AUTHORIZATION_REFRESH_RESULTS.clear()
        remote_access._AUTHORIZATION_REFRESH_FLIGHTS.clear()


def _paired_config(tmp_path, *, revision: int = 41) -> V2Config:
    import os

    os.environ["AVIBE_HOME"] = str(tmp_path)
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.test"
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.instance_kind = "organization"
    cloud.instance_secret = "instance-secret"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    config.save()
    remote_access._clear_authorization_revision_cache()
    remote_access._replace_authorization_revision(config, revision)
    return config
    # Binding-row seeding is the CALLER's job: some tests need an absent
    # row (upgrade path), others need a ready row (concurrent resolvers).


def _organization_claims(
    config: V2Config,
    *,
    revision: int = 41,
    role: str = "editor",
) -> dict[str, Any]:
    return {
        "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
        "vibe_instance_role": role,
        "vibe_instance_access_source": "organization_group",
        "vibe_instance_authorization_revision": revision,
        "vibe_organization_id": "org_123",
        "vibe_organization_member_id": "member_123",
        "vibe_organization_role": "member",
        "vibe_group_ids": ["group_research"],
        "vibe_membership_version": "membership-v1",
    }


def _organization_cookie(
    config: V2Config,
    *,
    revision: int = 41,
    subject: str = "user-1",
) -> str:
    return remote_access.make_session_cookie(
        config,
        f"{subject}@example.com",
        subject,
        session_claims=_organization_claims(config, revision=revision),
    )


def _authorization_context_response(
    config: V2Config,
    request_payload: dict[str, Any],
    *,
    revision: int,
    instance_kind: str = "organization",
) -> dict[str, Any]:
    return {
        "sub": request_payload["sub"],
        "email": request_payload["email"],
        "instance_kind": instance_kind,
        **_organization_claims(config, revision=revision),
    }


def test_personal_session_slides_past_claim_and_original_identity_deadlines(
    monkeypatch,
    tmp_path,
):
    """Scenario: AUTH-SETUP-402."""

    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    base = int(time.time())
    monkeypatch.setattr(remote_access.time, "time", lambda: base)
    cookie = remote_access.make_session_cookie(
        config,
        "owner@example.com",
        "personal-owner",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "owner",
            "vibe_instance_access_source": "owner",
            "vibe_instance_authorization_revision": 41,
        },
    )

    monkeypatch.setattr(
        remote_access.time,
        "time",
        lambda: base + remote_access.PERSONAL_SESSION_RENEW_AFTER_SECONDS + 1,
    )
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    first = remote_access.resolve_current_authorization(config, identity)
    assert first.current is True
    renewed_cookie = remote_access.renew_session_cookie(config, first.payload)
    renewed_identity = remote_access.parse_session_identity(config, renewed_cookie)
    assert renewed_identity is not None
    assert renewed_identity["claims_issued_at"] == base

    monkeypatch.setattr(
        remote_access.time,
        "time",
        lambda: base + remote_access.PERSONAL_SESSION_TTL_SECONDS + 60,
    )
    after_original_expiry = remote_access.parse_session_identity(config, renewed_cookie)
    assert after_original_expiry is not None
    result = remote_access.resolve_current_authorization(config, after_original_expiry)
    assert result.current is True
    assert result.policy == "personal"
    assert result.payload["claims_issued_at"] == base


def test_validated_remote_payload_projects_paired_instance_kind(tmp_path):
    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    payload = remote_access._validated_authorization_payload(
        config,
        {
            "sub": "personal-user",
            "email": "personal@example.com",
            "instance_id": "inst_123",
        },
        {
            "id": "authorization-1",
            "claims": {**_organization_claims(config), "vibe_instance_kind": "personal"},
        },
    )

    assert payload is not None
    assert context_from_session_payload(payload).is_personal_instance


def test_cached_authorization_from_previous_instance_kind_refreshes_before_personal_bypass(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    calls = []

    def refresh(_config, _method, _suffix, payload, **kwargs):
        calls.append(payload)
        return _authorization_context_response(
            config,
            payload,
            revision=41,
            instance_kind="personal",
        )

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.current is True
    assert result.refreshed is True
    assert result.policy == "personal"
    assert len(calls) == 1
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=int(time.time()),
    )
    assert record is not None
    assert record["claims"]["vibe_instance_kind"] == "personal"


def test_partial_pairing_cannot_restore_cached_personal_authorization(tmp_path):
    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = ""
    config.save()

    payload = remote_access._validated_authorization_payload(
        config,
        {
            "sub": "personal-user",
            "email": "personal@example.com",
            "instance_id": "inst_123",
        },
        {
            "id": "authorization-1",
            "claims": {**_organization_claims(config), "vibe_instance_kind": "organization"},
        },
    )

    assert payload is None
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    resolution = remote_access.resolve_current_authorization(
        config,
        identity,
        allow_refresh=False,
    )
    assert resolution.state == "unavailable"
    assert resolution.reason == "pairing_unavailable"


def test_partial_pairing_preserves_exact_show_page_email_grant(tmp_path):
    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = ""
    config.save()
    cookie = remote_access.make_session_cookie(
        config,
        "viewer@example.com",
        "viewer-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "show_page_email",
            "vibe_show_page_id": "show-1",
            "vibe_instance_authorization_revision": 41,
        },
    )
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None

    resolution = remote_access.resolve_current_authorization(
        config,
        identity,
        allow_refresh=False,
    )

    assert resolution.current is True
    assert resolution.payload is not None
    assert resolution.payload["vibe_show_page_id"] == "show-1"


def test_organization_to_personal_reclassification_revalidates_before_the_bypass(
    monkeypatch,
    tmp_path,
):
    """A reclassified pairing must re-earn every instance-scoped authorization."""

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    reference = identity["authorization_ref"]
    now = int(time.time())
    admitted = remote_access_authorization_service.load_reference_record(
        reference=reference,
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert admitted is not None
    assert admitted["authorization_state"] == "current"

    assert remote_access._persist_instance_kind("inst_123", "personal", reconcile=True) is True

    reclassified = V2Config.load()
    assert reclassified.remote_access.vibe_cloud.instance_kind == "personal"
    invalidated = remote_access_authorization_service.load_reference_record(
        reference=reference,
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert invalidated is not None
    assert invalidated["authorization_state"] == "stale"

    # The cached Organization claims must not be re-projected as Personal.
    blocked = remote_access.resolve_current_authorization(
        reclassified,
        identity,
        allow_refresh=False,
    )
    assert blocked.current is False
    assert blocked.reason == "authorization_context_missing"

    calls = []

    def refresh(_config, _method, _suffix, payload, **kwargs):
        calls.append(payload)
        return _authorization_context_response(
            config,
            payload,
            revision=41,
            instance_kind="personal",
        )

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)

    result = remote_access.resolve_current_authorization(reclassified, identity)

    assert result.current is True
    assert result.refreshed is True
    assert result.policy == "personal"
    # Synchronous revalidation, not a background hint that would let the
    # Personal bypass answer this very request from the stale row.
    assert len(calls) == 1
    revalidated = remote_access_authorization_service.load_reference_record(
        reference=reference,
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert revalidated is not None
    assert revalidated["authorization_state"] == "current"
    assert revalidated["claims"]["vibe_instance_kind"] == "personal"


def test_instance_kind_persistence_failure_leaves_no_kind_tagged_current_row(
    monkeypatch,
    tmp_path,
):
    """A failed kind write must not leave a usable Personal projection behind."""

    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = ""
    config.save()
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    reference = identity["authorization_ref"]
    now = int(time.time())
    # Collapse the failure backoff so the retry below exercises the network
    # path instead of the short-circuit that protects the backend.
    monkeypatch.setattr(remote_access, "AUTHORIZATION_REFRESH_FAILURE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(
        remote_access,
        "current_authorization_revision",
        lambda *args, **kwargs: 42,
    )
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda _config, _method, _suffix, payload, **kwargs: _authorization_context_response(
            config,
            payload,
            revision=42,
            instance_kind="personal",
        ),
    )
    real_save_config = remote_access.api.save_config
    attempted: list[dict[str, Any]] = []

    def failing_save_config(payload, **kwargs):
        attempted.append(payload)
        raise OSError("read-only config")

    monkeypatch.setattr(remote_access.api, "save_config", failing_save_config)

    blocked = remote_access.resolve_current_authorization(config, identity)

    assert blocked.state == "unavailable"
    assert blocked.reason == "instance_kind_persistence_failed"
    assert attempted
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == ""
    stored = remote_access_authorization_service.load_reference_record(
        reference=reference,
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert stored is not None
    assert "vibe_instance_kind" not in stored["claims"]
    kindless_payload = remote_access._validated_authorization_payload(
        config,
        identity,
        stored,
    )
    assert kindless_payload is not None
    assert context_from_session_payload(kindless_payload).is_personal_instance is False

    monkeypatch.setattr(remote_access.api, "save_config", real_save_config)

    retried = remote_access.resolve_current_authorization(config, identity)

    assert retried.current is True
    assert retried.policy == "personal"
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "personal"
    repaired = remote_access_authorization_service.load_reference_record(
        reference=reference,
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert repaired is not None
    assert repaired["claims"]["vibe_instance_kind"] == "personal"


def test_refresh_from_a_previous_binding_generation_is_discarded(monkeypatch, tmp_path):
    """An in-flight response that outlived its binding must not be cached."""

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert record is not None
    binding_before = remote_access_authorization_service.load_instance_binding_state()
    assert binding_before is not None

    def repair_while_in_flight(_config, _method, _suffix, payload, **kwargs):
        # A re-pair lands while this authorization response is in flight.
        remote_access._transition_instance_binding(
            instance_id="inst_repair",
            instance_kind="personal",
        )
        return _authorization_context_response(config, payload, revision=41)

    monkeypatch.setattr(remote_access, "_device_json_request", repair_while_in_flight)

    result = remote_access._refresh_authorization_context(
        config,
        identity,
        record,
        now=now,
        force=True,
    )

    assert result.state == "unavailable"
    assert result.reason == "instance_binding_changed"
    binding_after = remote_access_authorization_service.load_instance_binding_state()
    assert binding_after is not None
    assert binding_after["generation"] > binding_before["generation"]
    assert binding_after["instance_id"] == "inst_repair"
    with remote_access._AUTHORIZATION_REFRESH_LOCK:
        assert remote_access._AUTHORIZATION_REFRESH_RESULTS == {}
        assert remote_access._AUTHORIZATION_REFRESH_FAILURES == {}
    # The discarded generation must not be answerable from cache either.
    assert (
        remote_access.resolve_current_authorization(
            config,
            identity,
            allow_refresh=False,
        ).current
        is False
    )


def test_stale_cross_process_kind_response_cannot_reverse_durable_generation(
    monkeypatch,
    tmp_path,
):
    """A UI-process in-flight Personal response must not undo a controller reclass.

    The controller and UI server are separate processes; the in-memory
    binding epoch cannot fence this. The durable generation in state_meta
    is the compare-and-swap token both processes share.
    """

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="personal",
    )
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    cookie = remote_access.make_session_cookie(
        config,
        "user-1@example.com",
        "user-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "vibe_instance_authorization_revision": 41,
        },
    )
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    observed_generation = remote_access_authorization_service.current_instance_binding_generation()
    # Controller heartbeat in another process: Personal -> Organization.
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    config.remote_access.vibe_cloud.instance_kind = "organization"
    config.save()
    newer = remote_access_authorization_service.load_instance_binding_state()
    assert newer is not None
    assert newer["generation"] > observed_generation
    assert newer["instance_kind"] == "organization"

    # Stale UI-process in-flight epoch is unchanged; durable generation is not.
    monkeypatch.setattr(remote_access, "_authorization_binding_epoch", lambda: 0)
    persisted = remote_access._persist_instance_kind(
        "inst_123",
        "personal",
        expected_binding_generation=observed_generation,
        expected_binding_epoch=0,
    )

    assert persisted is False
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"
    after = remote_access_authorization_service.load_instance_binding_state()
    assert after is not None
    assert after["generation"] == newer["generation"]
    assert after["instance_kind"] == "organization"


def test_stale_write_is_refused_under_cross_process_config_lock(tmp_path):
    """Two threads contend on config_file_lock; the stale writer must abort.

    This is the real cross-process lock boundary (the same lock V2Config.save
    uses), not the in-process epoch.
    """

    from config.v2_config import config_file_lock

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="personal",
    )
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    observed = remote_access_authorization_service.current_instance_binding_generation()
    # flock is process-level, so two threads in one process cannot actually
    # block each other on config_file_lock. Model the cross-process race as:
    # snapshot generation, then a controller advance under that lock, then a
    # stale persist that must CAS-fail against the durable generation.
    with config_file_lock():
        remote_access._transition_instance_binding(
            instance_id="inst_123",
            instance_kind="organization",
        )
        config.remote_access.vibe_cloud.instance_kind = "organization"
        config.save()
    controller_generation = (
        remote_access_authorization_service.current_instance_binding_generation()
    )
    stale_ok = remote_access._persist_instance_kind(
        "inst_123",
        "personal",
        expected_binding_generation=observed,
    )

    assert stale_ok is False
    assert controller_generation > observed
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"
    after = remote_access_authorization_service.load_instance_binding_state()
    assert after is not None
    assert after["instance_kind"] == "organization"
    assert after["generation"] == controller_generation


def test_stale_denied_response_does_not_recreate_revoked_row(monkeypatch, tmp_path):
    """A 403 captured before a transition must not recreate a revoked row."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    started = remote_access_authorization_service.begin_instance_binding_transition(
        instance_id="inst_123",
        instance_kind="organization",
    )
    assert remote_access_authorization_service.complete_instance_binding_transition(
        instance_id="inst_123",
        instance_kind="organization",
        generation=started["generation"],
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert record is not None
    observed = remote_access_authorization_service.current_instance_binding_generation()
    real_generation = remote_access_authorization_service.current_instance_binding_generation
    calls = {"n": 0}

    def deny_after_transition(_config, _method, _suffix, payload, **kwargs):
        remote_access._transition_instance_binding(
            instance_id="inst_123",
            instance_kind="personal",
        )
        raise remote_access.BackendRequestError(403, {"error": "access_denied"})

    def generation(*, ensure: bool = True) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return observed
        return real_generation(ensure=ensure)

    monkeypatch.setattr(remote_access, "_device_json_request", deny_after_transition)
    monkeypatch.setattr(
        remote_access_authorization_service,
        "current_instance_binding_generation",
        generation,
    )

    result = remote_access._fetch_authorization_context(
        config,
        identity,
        record,
        now=now,
        observed_revision=41,
    )

    assert result.state == "unavailable"
    assert result.reason == "instance_binding_changed"
    stored = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    if stored is not None:
        assert stored.get("authorization_state") != "revoked"


def test_exact_show_page_grants_survive_a_kind_transition_but_not_a_repair(tmp_path):
    """Show Page grants are their own scope, bound to the instance that issued them."""

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    now = int(time.time())
    show_page_claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": "viewer",
        "vibe_instance_access_source": "show_page_email",
        "vibe_show_page_id": "show-1",
        "vibe_instance_authorization_revision": 41,
    }
    show_page_cookie = remote_access.make_session_cookie(
        config,
        "viewer@example.com",
        "viewer-1",
        session_claims=show_page_claims,
    )
    show_page_identity = remote_access.parse_session_identity(config, show_page_cookie)
    assert show_page_identity is not None
    instance_identity = remote_access.parse_session_identity(
        config,
        _organization_cookie(config),
    )
    assert instance_identity is not None

    assert remote_access._persist_instance_kind("inst_123", "personal", reconcile=True) is True

    reclassified = V2Config.load()
    show_page = remote_access_authorization_service.load_scoped(
        instance_id="inst_123",
        subject="viewer-1",
        scope_kind="show_page",
        scope_ref="show-1",
    )
    instance = remote_access_authorization_service.load_scoped(
        instance_id="inst_123",
        subject="user-1",
        scope_kind="instance",
        scope_ref="inst_123",
    )
    assert show_page is not None
    assert show_page["authorization_state"] == "current"
    assert show_page["claims"]["vibe_show_page_id"] == "show-1"
    assert instance is not None
    assert instance["authorization_state"] == "stale"

    resolved = remote_access.resolve_current_authorization(
        reclassified,
        show_page_identity,
        allow_refresh=False,
    )
    assert resolved.current is True
    assert resolved.payload is not None
    assert resolved.payload["vibe_show_page_id"] == "show-1"

    # Mid-transition the exact grant still reads, while the instance-scoped
    # cache stays fail-closed.
    reconciling = remote_access_authorization_service.begin_instance_binding_transition(
        instance_id="inst_repair",
        instance_kind="personal",
    )
    assert (
        reconciling["state"]
        == remote_access_authorization_service.INSTANCE_BINDING_STATE_RECONCILING
    )
    assert (
        remote_access.binding_is_ready(reclassified, show_page_identity)
        is True
    )
    assert (
        remote_access.binding_is_ready(reclassified, instance_identity)
        is False
    )

    # Re-pairing to a different instance is not a reclassification: those
    # grants were issued by the previous instance and must not survive it.
    remote_access._transition_instance_binding(
        instance_id="inst_repair",
        instance_kind="personal",
        previous_instance_id="inst_123",
    )
    repaired = remote_access_authorization_service.load_scoped(
        instance_id="inst_123",
        subject="viewer-1",
        scope_kind="show_page",
        scope_ref="show-1",
    )
    assert repaired is not None
    assert repaired["authorization_state"] == "stale"


def test_personal_revision_hint_refreshes_in_background_without_blocking(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def unavailable(*args, **kwargs):
        entered.set()
        release.wait(timeout=1)
        finished.set()
        raise remote_access.BackendRequestError(503, {"error": "unavailable"})

    monkeypatch.setattr(remote_access, "_device_json_request", unavailable)

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.current is True
    assert result.policy == "personal"
    assert entered.wait(timeout=1)
    assert finished.is_set() is False
    release.set()
    assert finished.wait(timeout=1)


def test_organization_scheduled_refresh_does_not_block_when_revision_matches(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    base = int(time.time())
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    refresh_at = base + remote_access.ORGANIZATION_AUTHORIZATION_REFRESH_SECONDS + 1
    assert remote_access_authorization_service.mark_matching_revision_checked(
        instance_id="inst_123",
        authorization_revision=41,
        checked_at=refresh_at,
    ) == 1
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 41)
    scheduled = []
    monkeypatch.setattr(
        remote_access,
        "_schedule_authorization_refresh",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )
    monkeypatch.setattr(
        remote_access,
        "_refresh_authorization_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("routine matching-revision refresh blocked the request")
        ),
    )

    result = remote_access.resolve_current_authorization(
        config,
        identity,
        now=refresh_at,
    )

    assert result.current is True
    assert result.policy == "organization"
    assert result.reason == "authorization_refresh_scheduled"
    assert len(scheduled) == 1


def test_organization_refresh_grace_expiry_and_recovery(monkeypatch, tmp_path):
    """Scenario: AUTH-SETUP-403."""

    config = _paired_config(tmp_path)
    base = int(time.time())
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    checked_at = base + remote_access.ORGANIZATION_AUTHORIZATION_REFRESH_SECONDS
    assert remote_access_authorization_service.mark_matching_revision_checked(
        instance_id="inst_123",
        authorization_revision=41,
        checked_at=checked_at,
    ) == 1
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(503, {"error": "unavailable"})
        ),
    )

    within_grace = remote_access.resolve_current_authorization(
        config,
        identity,
        now=checked_at + 1,
    )
    assert within_grace.current is True
    assert within_grace.reason == "authorization_grace"

    remote_access._AUTHORIZATION_REFRESH_FAILURES.clear()
    after_grace = remote_access.resolve_current_authorization(
        config,
        identity,
        now=checked_at + remote_access.ORGANIZATION_AUTHORIZATION_OUTAGE_GRACE_SECONDS + 1,
    )
    assert after_grace.state == "unavailable"
    assert after_grace.reason == "authorization_grace_expired"

    remote_access._AUTHORIZATION_REFRESH_FAILURES.clear()
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda _config, _method, _suffix, payload, **kwargs: _authorization_context_response(
            config,
            payload,
            revision=42,
        ),
    )
    recovered = remote_access.resolve_current_authorization(
        config,
        identity,
        now=checked_at + remote_access.ORGANIZATION_AUTHORIZATION_OUTAGE_GRACE_SECONDS + 2,
    )
    assert recovered.current is True
    assert recovered.refreshed is True
    assert recovered.payload["vibe_instance_authorization_revision"] == 42


def test_unknown_instance_kind_requires_durable_backfill_before_access(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = ""
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda _config, _method, _suffix, payload, **kwargs: _authorization_context_response(
            config,
            payload,
            revision=42,
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "_persist_instance_kind",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only config")),
    )

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.state == "unavailable"
    assert result.reason == "instance_kind_persistence_failed"
    assert config.remote_access.vibe_cloud.instance_kind == ""


def test_scoped_authorization_promotes_legacy_rows_and_isolates_show_pages(tmp_path):
    now = int(time.time())
    remote_access_authorization_service.store(
        reference="legacy-reference-12345678",
        instance_id="inst_123",
        subject="user-1",
        claims={"vibe_instance_id": "inst_123"},
        expires_at=now + 60,
        created_at=now,
    )

    instance_reference = remote_access_authorization_service.upsert_scoped(
        reference="legacy-reference-12345678",
        instance_id="inst_123",
        subject="user-1",
        email="user@example.com",
        scope_kind="instance",
        scope_ref="inst_123",
        authorization_state="current",
        claims={"scope": "instance"},
        last_checked_at=now + 1,
        updated_at=now + 1,
    )
    show_reference = remote_access_authorization_service.upsert_scoped(
        reference=None,
        instance_id="inst_123",
        subject="user-1",
        email="user@example.com",
        scope_kind="show_page",
        scope_ref="show-1",
        authorization_state="current",
        claims={"scope": "show_page"},
        last_checked_at=now + 2,
        updated_at=now + 2,
    )

    instance = remote_access_authorization_service.load_scoped(
        instance_id="inst_123",
        subject="user-1",
        scope_kind="instance",
        scope_ref="inst_123",
    )
    show_page = remote_access_authorization_service.load_scoped(
        instance_id="inst_123",
        subject="user-1",
        scope_kind="show_page",
        scope_ref="show-1",
    )
    assert instance_reference == "legacy-reference-12345678"
    assert show_reference != instance_reference
    assert instance is not None and instance["claims"] == {"scope": "instance"}
    assert instance["expires_at"] is None
    assert show_page is not None and show_page["claims"] == {"scope": "show_page"}


def test_referenced_identity_does_not_guess_scope_when_record_is_missing(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    cookie = remote_access.make_session_cookie(
        config,
        "viewer@example.com",
        "viewer-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "show_page_email",
            "vibe_show_page_id": "show-1",
            "vibe_instance_authorization_revision": 41,
        },
    )
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    assert isinstance(identity.get("authorization_ref"), str)
    assert remote_access_authorization_service.delete_for_instance("inst_123") == 1
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing referenced records must not guess a refresh scope")
        ),
    )

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.state == "invalid_identity"
    assert result.reason == "authorization_record_missing"


def test_authorization_record_read_failure_is_unavailable(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    identity = remote_access.parse_session_identity(config, _organization_cookie(config))
    assert identity is not None
    monkeypatch.setattr(
        remote_access_authorization_service,
        "load_reference_record",
        lambda **kwargs: (_ for _ in ()).throw(OSError("database unavailable")),
    )
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("storage failure must not trigger an unscoped refresh")
        ),
    )

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.state == "unavailable"
    assert result.reason == "authorization_record_unavailable"


def test_revision_poll_touches_every_matching_current_context_only(tmp_path):
    now = int(time.time())
    rows = (
        ("user-match", 7, "current"),
        ("user-other", 8, "current"),
        ("user-revoked", 7, "revoked"),
    )
    for subject, revision, state in rows:
        remote_access_authorization_service.upsert_scoped(
            reference=None,
            instance_id="inst_123",
            subject=subject,
            email=f"{subject}@example.com",
            scope_kind="instance",
            scope_ref="inst_123",
            authorization_state=state,
            claims={"vibe_instance_authorization_revision": revision},
            last_checked_at=now,
            updated_at=now,
        )

    assert remote_access_authorization_service.mark_matching_revision_checked(
        instance_id="inst_123",
        authorization_revision=7,
        checked_at=now + 30,
    ) == 1
    checked = {
        subject: remote_access_authorization_service.load_scoped(
            instance_id="inst_123",
            subject=subject,
            scope_kind="instance",
            scope_ref="inst_123",
        )["last_checked_at"]
        for subject, _revision, _state in rows
    }
    assert checked == {
        "user-match": now + 30,
        "user-other": now,
        "user-revoked": now,
    }


def test_concurrent_resolvers_share_one_authorization_context_refresh(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    calls = 0
    calls_lock = threading.Lock()

    def refresh(_config, _method, _suffix, payload, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return _authorization_context_response(config, payload, revision=42)

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: remote_access.resolve_current_authorization(config, identity),
            range(2),
        ))

    assert calls == 1
    assert all(result.current and result.refreshed for result in results)


def test_authorization_context_refreshes_for_different_subjects_run_concurrently(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    identities = []
    for subject in ("user-1", "user-2"):
        identity = remote_access.parse_session_identity(
            config,
            _organization_cookie(config, subject=subject),
        )
        assert identity is not None
        identities.append(identity)
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    entered = threading.Barrier(2)
    refreshed_subjects = []
    calls_lock = threading.Lock()

    def refresh(_config, _method, _suffix, payload, **kwargs):
        with calls_lock:
            refreshed_subjects.append(payload["sub"])
        entered.wait(timeout=2)
        return _authorization_context_response(config, payload, revision=42)

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda identity: remote_access.resolve_current_authorization(config, identity),
            identities,
        ))

    assert set(refreshed_subjects) == {"user-1", "user-2"}
    assert all(result.current and result.refreshed for result in results)


def test_concurrent_revocation_refresh_is_reused_and_persisted(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    identity = remote_access.parse_session_identity(config, _organization_cookie(config))
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    calls = 0
    calls_lock = threading.Lock()

    def revoke(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        raise remote_access.BackendRequestError(403, {"error": "access_denied"})

    monkeypatch.setattr(remote_access, "_device_json_request", revoke)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: remote_access.resolve_current_authorization(config, identity),
            range(2),
        ))

    assert calls == 1
    assert all(result.state == "revoked" for result in results)
    with remote_access._AUTHORIZATION_REFRESH_LOCK:
        remote_access._AUTHORIZATION_REFRESH_RESULTS.clear()

    persisted = remote_access.resolve_current_authorization(config, identity)

    assert persisted.state == "revoked"
    assert calls == 1


def test_refresh_backoff_starts_when_the_network_request_finishes(
    monkeypatch,
    tmp_path,
):
    config = _paired_config(tmp_path)
    identity = remote_access.parse_session_identity(config, _organization_cookie(config))
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    monotonic_values = itertools.chain(
        (100.0, 110.0, 110.05, 110.1, 110.15),
        itertools.repeat(110.2),
    )
    monkeypatch.setattr(remote_access.time, "monotonic", lambda: next(monotonic_values))
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise remote_access.BackendRequestError(503, {"error": "unavailable"})

    monkeypatch.setattr(remote_access, "_device_json_request", unavailable)

    remote_access.resolve_current_authorization(config, identity)
    remote_access.resolve_current_authorization(config, identity)

    assert calls == 1


def test_revoked_authorization_recovers_after_revision_change(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    identity = remote_access.parse_session_identity(config, _organization_cookie(config))
    assert identity is not None
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 42)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(403, {"error": "access_denied"})
        ),
    )
    assert remote_access.resolve_current_authorization(config, identity).state == "revoked"

    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *args, **kwargs: 43)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda _config, _method, _suffix, payload, **kwargs: _authorization_context_response(
            config,
            payload,
            revision=43,
        ),
    )

    recovered = remote_access.resolve_current_authorization(config, identity)

    assert recovered.current is True
    assert recovered.refreshed is True
    assert recovered.payload["vibe_instance_authorization_revision"] == 43


def test_explicit_login_can_revalidate_revoked_authorization(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    identity = remote_access.parse_session_identity(config, _organization_cookie(config))
    assert identity is not None
    observed_revision = 42
    monkeypatch.setattr(
        remote_access,
        "current_authorization_revision",
        lambda *args, **kwargs: observed_revision,
    )
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(403, {"error": "access_denied"})
        ),
    )
    assert remote_access.resolve_current_authorization(config, identity).state == "revoked"

    refresh_calls = 0

    def refresh(_config, _method, _suffix, payload, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        return _authorization_context_response(config, payload, revision=42)

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)
    assert remote_access.resolve_current_authorization(config, identity).state == "revoked"

    recovered = remote_access.resolve_current_authorization(
        config,
        identity,
        refresh_revoked=True,
    )

    assert recovered.current is True
    assert recovered.refreshed is True
    assert refresh_calls == 1


def test_remote_login_forces_revoked_authorization_revalidation(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    refresh_flags = []

    def resolve(_config, identity, **kwargs):
        refresh_flags.append(kwargs.get("refresh_revoked"))
        return remote_access.AuthorizationResolution("current", payload=identity)

    monkeypatch.setattr(remote_access, "resolve_current_authorization", resolve)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        cookie,
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/auth/login?next=/inbox",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/inbox"
    assert refresh_flags[-1] is True
    assert refresh_flags.count(True) == 1


def test_browser_logout_invalidates_http_sse_and_websocket(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert identity is not None
    assert payload is not None
    assert remote_access.revoke_browser_session(identity) is True

    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    assert client.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    ).get_json()["authenticated"] is False

    monkeypatch.setattr(ui_server, "_AUTHORIZATION_REVISION_RECHECK_SECONDS", 0.001)

    async def exercise() -> None:
        with app.test_request_context(
            "/api/events",
            base_url="https://alex.avibe.bot",
            headers={"Cookie": f"{remote_access.SESSION_COOKIE_NAME}={cookie}"},
        ):
            g.authorization_context = context_from_session_payload(payload)
            g.remote_session_identity = identity
            g.remote_session_payload = payload
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                terminal = await asyncio.wait_for(iterator.__anext__(), timeout=1)
                assert '"state":"invalid_identity"' in terminal
                with pytest.raises(StopAsyncIteration):
                    await iterator.__anext__()
            finally:
                await iterator.aclose()
        outcome = await asyncio.wait_for(
            ui_server._wait_for_remote_session_authorization_loss(
                config,
                identity,
                payload,
                session_cookie=cookie,
                request_host="alex.avibe.bot",
            ),
            timeout=1,
        )
        assert outcome == "invalid_identity"

    asyncio.run(exercise())


def test_legacy_browser_logout_derives_a_stable_revocation_identity(tmp_path):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    legacy_payload = remote_access.parse_session_identity(config, cookie)
    assert legacy_payload is not None
    legacy_payload.pop("browser_session_id")
    legacy_cookie = remote_access._encode_session_cookie(
        config.remote_access.vibe_cloud.session_secret,
        legacy_payload,
    )
    accepted_identity = remote_access.parse_session_identity(config, legacy_cookie)
    assert accepted_identity is not None
    assert accepted_identity["browser_session_id"] == remote_access.parse_session_identity(
        config,
        legacy_cookie,
    )["browser_session_id"]

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        legacy_cookie,
        domain="alex.avibe.bot",
    )
    response = client.post(
        "/auth/logout",
        base_url="https://alex.avibe.bot",
        json={},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
    )

    assert response.status_code == 200
    assert remote_access.parse_session_identity(config, legacy_cookie) is None
    assert remote_access.session_identity_is_current(accepted_identity) is False


@pytest.mark.parametrize(
    ("state", "status_code", "error"),
    [
        ("revoked", 403, "remote_access_revoked"),
        ("unavailable", 503, "remote_access_authorization_unavailable"),
    ],
)
def test_remote_authorization_failure_serves_spa_shell_but_rejects_api(
    monkeypatch,
    tmp_path,
    state,
    status_code,
    error,
):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    monkeypatch.setattr(
        remote_access,
        "resolve_current_authorization",
        lambda *args, **kwargs: remote_access.AuthorizationResolution(state),
    )
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        cookie,
        domain="alex.avibe.bot",
    )

    shell = client.get(
        "/",
        base_url="https://alex.avibe.bot",
        headers={"Accept": "text/html"},
    )
    protected = client.get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        headers={"Accept": "application/json"},
    )

    assert shell.status_code == 200
    assert shell.headers["content-type"].startswith("text/html")
    assert protected.status_code == status_code
    assert protected.get_json()["error"] == error


def _delayed_authorization_revision_sync_writer(
    home: str,
    entered: Any,
    release: Any,
    result_queue: Any,
) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import V2Config as _V2Config
    from vibe import remote_access as _remote_access

    config = _V2Config.load()
    state_path = _remote_access._authorization_revision_state_path()
    original_read_json = _remote_access.runtime.read_json
    paused = False

    def delayed_read_json(path):
        nonlocal paused

        payload = original_read_json(path)
        if path == state_path and not paused:
            paused = True
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("delayed revision writer was never released")
        return payload

    _remote_access.runtime.read_json = delayed_read_json
    try:
        result_queue.put(("ok", _remote_access._replace_authorization_revision(config, 42)))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))


def _authorization_revision_acknowledgement_writer(
    home: str,
    attempted: Any,
    done: Any,
    result_queue: Any,
) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import V2Config as _V2Config
    from vibe import remote_access as _remote_access

    config = _V2Config.load()
    attempted.set()
    try:
        result_queue.put(("ok", _remote_access.acknowledge_authorization_revision(config, 43)))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))
    finally:
        done.set()


def test_authorization_revision_device_contract_is_monotonic(monkeypatch, tmp_path):
    """I1057-AC2/AC3: one paired-device watermark drives every hostname."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    calls = []

    def request(cfg, method, suffix, payload=None, *, timeout=8.0):
        calls.append((cfg, method, suffix, payload, timeout))
        return {"authorization_revision": 42}

    monkeypatch.setattr(remote_access, "_device_json_request", request)

    assert remote_access.sync_authorization_revision_once(config) == {
        "ok": True,
        "authorization_revision": 42,
    }
    assert calls == [(config, "GET", "authorization-revision", None, 8.0)]
    assert remote_access.current_authorization_revision(config) == 42

    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: {"authorization_revision": 41},
    )
    assert remote_access.sync_authorization_revision_once(config) == {
        "ok": False,
        "error": "authorization_revision_regressed",
    }
    assert remote_access.current_authorization_revision(config) == 42


def test_authorization_revision_writes_are_monotonic_across_processes(
    monkeypatch,
    tmp_path,
):
    """A delayed synchronization write cannot replace a newer acknowledgement."""

    import multiprocessing as mp

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config(tmp_path, revision=41)
    ctx = mp.get_context("spawn")
    sync_entered = ctx.Event()
    release_sync = ctx.Event()
    acknowledgement_attempted = ctx.Event()
    acknowledgement_done = ctx.Event()
    sync_result = ctx.Queue()
    acknowledgement_result = ctx.Queue()
    sync_writer = ctx.Process(
        target=_delayed_authorization_revision_sync_writer,
        args=(str(tmp_path), sync_entered, release_sync, sync_result),
    )
    acknowledgement_writer = ctx.Process(
        target=_authorization_revision_acknowledgement_writer,
        args=(
            str(tmp_path),
            acknowledgement_attempted,
            acknowledgement_done,
            acknowledgement_result,
        ),
    )

    sync_writer.start()
    assert sync_entered.wait(timeout=15), "sync writer never entered its transaction"
    acknowledgement_writer.start()
    assert acknowledgement_attempted.wait(timeout=15), "acknowledgement writer never attempted"
    assert not acknowledgement_done.wait(timeout=1), (
        "acknowledgement writer bypassed the synchronization transaction"
    )
    release_sync.set()
    sync_writer.join(timeout=15)
    acknowledgement_writer.join(timeout=15)

    assert sync_writer.exitcode == 0, f"sync writer failed: {sync_writer.exitcode}"
    assert acknowledgement_writer.exitcode == 0, (
        f"acknowledgement writer failed: {acknowledgement_writer.exitcode}"
    )
    assert sync_result.get(timeout=5) == ("ok", 42)
    assert acknowledgement_result.get(timeout=5) == ("ok", 43)
    persisted = remote_access.runtime.read_json(
        remote_access._authorization_revision_state_path()
    )
    assert persisted["authorization_revision"] == 43


def test_authorization_revision_cache_observes_another_process_watermark(
    monkeypatch,
    tmp_path,
):
    """A controller cache must observe a mutation persisted by the UI server."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path, revision=41)
    claims = _organization_claims(config, revision=41)
    source_updated_at = time.time()

    assert remote_access.session_authorization_revision_state(
        config,
        claims,
        now=source_updated_at,
    ) == "current"

    remote_access.runtime.write_json(
        remote_access._authorization_revision_state_path(),  # noqa: SLF001
        {
            "schema_version": 1,
            "instance_id": "inst_123",
            "authorization_revision": 42,
            "source_updated_at": source_updated_at,
        },
    )

    assert remote_access.session_authorization_revision_state(
        config,
        claims,
        now=source_updated_at,
    ) == "mismatch"


@pytest.mark.parametrize(
    ("changed_field", "replacement"),
    (
        ("backend_url", "https://other-backend.test"),
        ("instance_id", "inst_new"),
        ("instance_secret", "other-secret"),
    ),
)
def test_authorization_revision_acknowledgement_rejects_a_stale_pairing(
    monkeypatch,
    tmp_path,
    changed_field,
    replacement,
):
    """A mutation from instance A cannot replace the watermark after pairing B."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path, revision=41)
    current = V2Config.load()
    setattr(current.remote_access.vibe_cloud, changed_field, replacement)
    current.save()

    with pytest.raises(
        remote_access.AuthorizationRevisionPairingChangedError,
        match="authorization_revision_pairing_changed",
    ):
        remote_access.acknowledge_authorization_revision(config, 42)

    persisted = remote_access.runtime.read_json(
        remote_access._authorization_revision_state_path()
    )
    assert persisted["instance_id"] == "inst_123"
    assert persisted["authorization_revision"] == 41


def test_authorization_revision_acknowledgement_rechecks_pairing_at_locked_write(
    monkeypatch,
    tmp_path,
):
    """A pairing switch at the watermark lock cannot let the old write through."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path, revision=41)
    original_lock = remote_access._authorization_revision_file_lock  # noqa: SLF001
    switched = False

    def switch_before_locked_read(path):
        nonlocal switched
        if not switched:
            switched = True
            current = V2Config.load()
            current.remote_access.vibe_cloud.instance_id = "inst_new"
            current.save()
        return original_lock(path)

    monkeypatch.setattr(
        remote_access,
        "_authorization_revision_file_lock",
        switch_before_locked_read,
    )

    with pytest.raises(
        remote_access.AuthorizationRevisionPairingChangedError,
        match="authorization_revision_pairing_changed",
    ):
        remote_access.acknowledge_authorization_revision(config, 42)

    persisted = remote_access.runtime.read_json(
        remote_access._authorization_revision_state_path()
    )
    assert persisted["instance_id"] == "inst_123"
    assert persisted["authorization_revision"] == 41


@pytest.mark.parametrize("persistence_failure", ("write_error", "lock_timeout"))
def test_authorization_revision_sync_rejects_an_unpersisted_watermark(
    monkeypatch,
    tmp_path,
    persistence_failure,
):
    config = _paired_config(tmp_path)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *_args, **_kwargs: {"authorization_revision": 42},
    )
    if persistence_failure == "write_error":
        monkeypatch.setattr(
            remote_access.runtime,
            "write_json",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only state")),
        )
    else:
        def timed_out_lock(_path):
            raise MigrationLockTimeout("watermark lock timed out")

        monkeypatch.setattr(
            remote_access,
            "_authorization_revision_file_lock",
            timed_out_lock,
        )

    assert remote_access.sync_authorization_revision_once(config) == {
        "ok": False,
        "error": "authorization_revision_sync_failed",
    }
    assert remote_access.current_authorization_revision(config) == 41


@pytest.mark.parametrize(
    "malformed",
    (
        b"\xff",
        b"{not-json",
        b'{"schema_version":1,"instance_id":"inst_123","authorization_revision":"bad","source_updated_at":0}',
    ),
)
def test_authorization_revision_read_treats_malformed_content_as_absent(
    monkeypatch,
    tmp_path,
    malformed,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    remote_access._authorization_revision_state_path().write_bytes(malformed)  # noqa: SLF001
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001

    assert remote_access.current_authorization_revision(config) is None


@pytest.mark.parametrize(
    "malformed",
    (
        b"\xff",
        b"{not-json",
        b'{"schema_version":1,"instance_id":"inst_123","authorization_revision":"bad","source_updated_at":0}',
    ),
)
def test_authorization_acknowledgement_keeps_memory_revision_when_rewrite_fails(
    monkeypatch,
    tmp_path,
    malformed,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    state_path = remote_access._authorization_revision_state_path()  # noqa: SLF001
    state_path.write_bytes(malformed)
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001
    published = []
    monkeypatch.setattr(
        remote_access.runtime,
        "write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only state")),
    )
    monkeypatch.setattr(
        broker,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )

    assert remote_access.acknowledge_authorization_revision(config, 42) == 42
    assert remote_access.current_authorization_revision(config) == 42
    assert published == [
        ("authorization.changed", {"instance_authorization_revision": 42})
    ]


def test_authorization_revision_sync_keeps_strict_write_after_malformed_read(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    remote_access._authorization_revision_state_path().write_bytes(b"\xff")  # noqa: SLF001
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001
    monkeypatch.setattr(
        remote_access.runtime,
        "write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only state")),
    )

    with pytest.raises(OSError, match="read-only state"):
        remote_access._replace_authorization_revision(config, 42)  # noqa: SLF001


def test_paired_session_requires_signed_current_revision(monkeypatch, tmp_path):
    """I1057-AC4: unsigned, missing, and stale authorization versions fail closed."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    missing = _organization_claims(config)
    missing.pop("vibe_instance_authorization_revision")

    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="invalid_authorization_revision",
    ):
        remote_access.make_session_cookie(
            config,
            "member@example.com",
            "user-1",
            session_claims=missing,
        )

    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="stale_authorization_revision",
    ):
        _organization_cookie(config, revision=40)


@pytest.mark.parametrize(
    "hosted_change",
    [
        "role_downgrade",
        "group_membership_removal",
        "group_archival",
        "member_removal",
        "access_binding_removal",
    ],
)
def test_revision_advance_revokes_active_editor_http_session(
    monkeypatch,
    tmp_path,
    hosted_change,
):
    """I1057-AC1/AC2: narrowing invalidates a remote session with chat enabled."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None
    assert context_from_session_payload(payload).can_chat is True

    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    headers = csrf_headers(client, "https://alex.avibe.bot")
    before = client.post(
        "/api/sessions",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )
    assert before.status_code == 400

    assert hosted_change
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(403, {"error": "access_denied"})
        ),
    )
    remote_access._replace_authorization_revision(config, 42)
    after = client.post(
        "/api/sessions",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )

    assert after.status_code == 403
    assert after.get_json()["error"] == "remote_access_revoked"


def test_default_and_custom_hostname_sessions_share_revision(monkeypatch, tmp_path):
    """I1057-AC3: host-scoped cookies cannot retain divergent authorization."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    default_client = app.test_client()
    custom_client = app.test_client()
    default_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="default-user"),
        domain="alex.avibe.bot",
    )
    custom_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _organization_cookie(config, subject="custom-user"),
        domain="max.fileguard.io",
    )

    assert default_client.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    ).get_json()["authenticated"] is True
    assert custom_client.get(
        "/api/session",
        base_url="https://max.fileguard.io",
    ).get_json()["authenticated"] is True

    refreshed_subjects = []

    def refresh(_config, method, suffix, payload=None, *, timeout=8.0):
        assert (method, suffix, timeout) == ("POST", "authorization-context", 8.0)
        refreshed_subjects.append(payload["sub"])
        return {
            "sub": payload["sub"],
            "email": payload["email"],
            "instance_kind": "organization",
            **_organization_claims(config, revision=42),
        }

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)
    remote_access._replace_authorization_revision(config, 42)

    default_session = default_client.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    ).get_json()
    custom_session = custom_client.get(
        "/api/session",
        base_url="https://max.fileguard.io",
    ).get_json()

    assert default_session["authenticated"] is True
    assert default_session["authorization_state"] == "current"
    assert custom_session["authenticated"] is True
    assert custom_session["authorization_state"] == "current"
    assert refreshed_subjects == ["default-user", "custom-user"]


def test_revision_snapshot_expiry_and_renewal_fail_closed(monkeypatch, tmp_path):
    """I1057-AC4: offline snapshots and renewal races cannot extend old claims."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None

    remote_access._replace_authorization_revision(config, 42)
    with pytest.raises(
        remote_access.OAuthCodeExchangeError,
        match="stale_authorization_revision",
    ):
        remote_access.make_session_cookie(
            config,
            "user-1@example.com",
            "user-1",
            session_claims=payload,
        )

    remote_access._replace_authorization_revision(config, 42)
    snapshot = remote_access._load_authorization_revision_snapshot(config)
    assert snapshot is not None
    _, updated_at = snapshot
    assert remote_access.session_authorization_is_current(
        config,
        {"vibe_instance_authorization_revision": 42},
        now=updated_at + remote_access.AUTHORIZATION_REVISION_MAX_AGE_SECONDS + 1,
    ) is False


def test_workbench_and_show_sse_end_after_revision_change(monkeypatch, tmp_path):
    """I1057-AC4/AC6: active and resumed SSE streams converge on the watermark."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    payload = remote_access.parse_session_cookie(config, cookie)
    identity = remote_access.parse_session_identity(config, cookie)
    assert payload is not None
    assert identity is not None
    context = context_from_session_payload(payload)

    class EmptyShowEventStore:
        def list(self, *args, **kwargs):
            return {"events": [], "next_after_id": None}

        def close(self):
            return None

    monkeypatch.setattr(ui_server, "_show_session_event_store", EmptyShowEventStore)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(403, {"error": "access_denied"})
        ),
    )

    async def exercise() -> None:
        with app.test_request_context(
            "/api/events",
            base_url="https://alex.avibe.bot",
            headers={"Cookie": f"{remote_access.SESSION_COOKIE_NAME}={cookie}"},
        ):
            g.authorization_context = context
            g.remote_session_identity = identity
            g.remote_session_payload = payload
            response = await ui_server.workbench_events()
            iterator = response.body_iterator.__aiter__()
            try:
                for _ in range(3):
                    await iterator.__anext__()
                remote_access._replace_authorization_revision(config, 42)
                terminal = await asyncio.wait_for(iterator.__anext__(), timeout=1)
                assert '"state":"revoked"' in terminal
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(iterator.__anext__(), timeout=1)
            finally:
                await iterator.aclose()

        remote_access._replace_authorization_revision(config, 42)
        show_response = await ui_server._show_events_stream(
            "ses123",
            after_id="show_evt_resume",
            authorization_context=context,
            remote_session_identity=identity,
            remote_session_payload=payload,
            remote_session_cookie=cookie,
            remote_request_host="alex.avibe.bot",
            remote_config=config,
        )
        show_iterator = show_response.body_iterator.__aiter__()
        try:
            terminal = await asyncio.wait_for(show_iterator.__anext__(), timeout=1)
            assert '"state":"revoked"' in terminal
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(show_iterator.__anext__(), timeout=1)
        finally:
            await show_iterator.aclose()

    asyncio.run(exercise())


def test_websocket_reconnect_and_active_waiter_recheck_revision(monkeypatch, tmp_path):
    """I1057-AC4/AC6: active sockets close and stale reconnects are rejected."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)

    class Socket:
        cookies = {remote_access.SESSION_COOKIE_NAME: cookie}

    websocket = Socket()
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *args: False)
    monkeypatch.setattr(ui_server, "_remote_access_host_allowed", lambda *args: True)
    monkeypatch.setattr(ui_server, "_websocket_normalized_host", lambda *args: "alex.avibe.bot")
    monkeypatch.setattr(ui_server, "_AUTHORIZATION_REVISION_RECHECK_SECONDS", 0.001)
    monkeypatch.setattr(
        remote_access,
        "_device_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(403, {"error": "access_denied"})
        ),
    )

    payload = ui_server._remote_access_websocket_session_payload(websocket, config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert payload is not None
    assert identity is not None

    async def exercise() -> None:
        waiter = asyncio.create_task(
            ui_server._wait_for_remote_session_authorization_loss(
                config,
                identity,
                payload,
                session_cookie=cookie,
                request_host="alex.avibe.bot",
            )
        )
        await asyncio.sleep(0)
        remote_access._replace_authorization_revision(config, 42)
        assert await asyncio.wait_for(waiter, timeout=1) == "revoked"

    asyncio.run(exercise())
    assert ui_server._remote_access_websocket_session_payload(websocket, config) is None


def test_remote_websocket_authorization_rejects_disabled_cloud(monkeypatch, tmp_path):
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    config.remote_access.vibe_cloud.enabled = False

    class Socket:
        cookies = {remote_access.SESSION_COOKIE_NAME: cookie}

    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *args: False)
    monkeypatch.setattr(ui_server, "_remote_access_host_allowed", lambda *args: True)
    monkeypatch.setattr(ui_server, "_websocket_normalized_host", lambda *args: "alex.avibe.bot")
    monkeypatch.setattr(
        remote_access,
        "resolve_current_authorization_async",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled remote socket reached the authorization resolver")
        ),
    )

    identity, resolution = asyncio.run(
        ui_server._remote_access_websocket_authorization(Socket(), config)
    )

    assert identity is None
    assert resolution.state == "invalid_identity"
    assert resolution.reason == "remote_access_disabled"


def test_live_remote_authorization_rechecks_the_remote_entry_gate(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    monkeypatch.setattr(
        remote_access,
        "resolve_current_authorization_async",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid live session reached the authorization resolver")
        ),
    )

    config.remote_access.vibe_cloud.enabled = False
    config.save()
    disabled = asyncio.run(
        ui_server._live_remote_authorization_resolution(
            config,
            identity,
            session_cookie=cookie,
            request_host="alex.avibe.bot",
        )
    )
    assert disabled.state == "invalid_identity"
    assert disabled.reason == "remote_access_disabled"

    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.session_secret = "rotated-session-secret"
    config.save()
    rotated = asyncio.run(
        ui_server._live_remote_authorization_resolution(
            config,
            identity,
            session_cookie=cookie,
            request_host="alex.avibe.bot",
        )
    )
    assert rotated.state == "invalid_identity"
    assert rotated.reason == "identity_invalid"

    config.remote_access.vibe_cloud.session_secret = "session-secret"
    config.remote_access.vibe_cloud.public_url = "https://other.avibe.bot"
    config.save()
    moved = asyncio.run(
        ui_server._live_remote_authorization_resolution(
            config,
            identity,
            session_cookie=cookie,
            request_host="alex.avibe.bot",
        )
    )
    assert moved.state == "invalid_identity"
    assert moved.reason == "remote_access_host_mismatch"


def test_terminal_websocket_reports_stale_remote_session_after_accept(
    monkeypatch,
    tmp_path,
):
    """I1057-AC4: no terminal service starts before a state-specific close."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)

    class RecordingWebSocket:
        client = None
        headers = {"host": "alex.avibe.bot"}
        query_params = {}

        def __init__(self):
            self.calls = []

        async def accept(self):
            self.calls.append(("accept", None))

        async def close(self, code=1000):
            self.calls.append(("close", code))

    websocket = RecordingWebSocket()
    monkeypatch.setattr(ui_server, "_terminal_enabled", lambda: True)
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    monkeypatch.setattr(ui_server, "_terminal_origin_allowed", lambda socket: True)
    monkeypatch.setattr(ui_server, "_load_remote_access_config", lambda: config)
    monkeypatch.setattr(ui_server, "_websocket_is_local_request", lambda *a: False)
    monkeypatch.setattr(
        ui_server,
        "_remote_access_websocket_authorization",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result=(None, remote_access.AuthorizationResolution("unavailable")),
        ),
    )
    monkeypatch.setattr(
        ui_server,
        "get_terminal_service",
        lambda: (_ for _ in ()).throw(AssertionError("stale socket reached terminal")),
    )

    asyncio.run(ui_server.terminal_websocket(websocket, "test"))

    assert websocket.calls == [
        ("accept", None),
        ("close", ui_server._AUTHORIZATION_UNAVAILABLE_WEBSOCKET_CLOSE_CODE),
    ]


def test_trusted_local_access_ignores_hosted_revision_state(monkeypatch, tmp_path):
    """I1057-AC5: trusted local access stays owner-equivalent while sync is offline."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    state_path = remote_access._authorization_revision_state_path()
    state_path.unlink()
    remote_access._clear_authorization_revision_cache()
    assert remote_access.current_authorization_revision(config) is None

    response = app.test_client().get(
        "/api/config",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json()["runtime"]["default_cwd"] == "."


def test_denied_response_during_reconciliation_does_not_persist_revoked(
    monkeypatch,
    tmp_path,
):
    """Regression PR #1606 r1: a 403 racing a same-generation reconciliation
    must not persist a revoked row the completed binding then trips over."""

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert record is not None
    # A reconciliation begins BEFORE the refresh starts: same generation is
    # captured, and completing a transition does not increment it.
    started = remote_access_authorization_service.begin_instance_binding_transition(
        instance_id="inst_123",
        instance_kind="personal",
    )
    assert (
        started["state"]
        == remote_access_authorization_service.INSTANCE_BINDING_STATE_RECONCILING
    )

    def deny(*args, **kwargs):
        raise remote_access.BackendRequestError(403, {"error": "access_denied"})

    monkeypatch.setattr(remote_access, "_device_json_request", deny)

    result = remote_access._fetch_authorization_context(
        config,
        identity,
        record,
        now=now,
        observed_revision=41,
    )

    assert result.state == "unavailable"
    assert result.reason == "instance_binding_changed"
    stored = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    if stored is not None:
        assert stored.get("authorization_state") != "revoked"


def test_known_kind_without_binding_row_bootstraps_a_durable_transition(tmp_path):
    """Regression PR #1606 r1: upgrade path — config carries a kind but the
    state_meta binding row is absent; the gate must earn a validated row
    through one real transition instead of trusting the config value."""

    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    # Simulate an upgrade: config has a known kind, the new state_meta row
    # has not been written yet.
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import state_meta
    from sqlalchemy import delete as sa_delete

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            sa_delete(state_meta).where(
                state_meta.c.key == remote_access_authorization_service.INSTANCE_BINDING_STATE_META_KEY
            )
        )
    assert (
        remote_access_authorization_service.load_instance_binding_state(ensure=False)
        is None
    )
    # The storage gate alone fails closed for a known kind with no row.
    assert remote_access_authorization_service.binding_is_ready_for_pairing(
        instance_id="inst_123",
        instance_kind="personal",
        ensure=False,
    ) is False

    # The consumer gate bootstraps one durable transition, then admits.
    assert remote_access.binding_is_ready(config) is True
    state = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert state is not None
    assert state["state"] == remote_access_authorization_service.INSTANCE_BINDING_STATE_READY
    assert state["instance_id"] == "inst_123"
    assert state["instance_kind"] == "personal"
    assert state["generation"] >= 1


def test_personal_to_organization_reclassification_rejects_stale_personal_row(
    monkeypatch,
    tmp_path,
):
    """Regression PR #1606 r2: the kind-mismatch refresh must run before the
    Organization branch too — a stale personal-tagged row must not be served
    as current under the reclassified Organization policy."""

    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="personal",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    stored = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert stored is not None
    assert stored["claims"]["vibe_instance_kind"] == "personal"

    # Controller heartbeat reclassifies Personal -> Organization.
    assert remote_access._persist_instance_kind("inst_123", "organization", reconcile=True) is True
    reclassified = V2Config.load()
    assert reclassified.remote_access.vibe_cloud.instance_kind == "organization"

    # The old personal-tagged row must not answer under the Organization policy.
    blocked = remote_access.resolve_current_authorization(
        reclassified,
        identity,
        allow_refresh=False,
    )
    assert blocked.current is False

    calls = []

    def refresh(_config, _method, _suffix, payload, **kwargs):
        calls.append(payload)
        return _authorization_context_response(
            config,
            payload,
            revision=41,
            instance_kind="organization",
        )

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)

    result = remote_access.resolve_current_authorization(reclassified, identity)

    assert result.current is True
    assert result.refreshed is True
    assert result.policy == "organization"
    assert len(calls) == 1
    revalidated = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert revalidated is not None
    assert revalidated["claims"]["vibe_instance_kind"] == "organization"


def test_revoked_write_refused_when_transition_completes_between_check_and_write(
    monkeypatch,
    tmp_path,
):
    """Regression PR #1606 r3: the compare and the authorization-row write
    are one atomic CAS under the binding lock. Completing a transition
    between a TOCTOU-style check and the write must refuse the revoked upsert.
    """

    config = _paired_config(tmp_path)
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="organization",
    )
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert record is not None
    started = remote_access_authorization_service.begin_instance_binding_transition(
        instance_id="inst_123",
        instance_kind="personal",
    )
    captured = remote_access_authorization_service.current_instance_binding_generation()

    def deny(*args, **kwargs):
        # Completes AFTER _fetch captured the reconciling generation and
        # BEFORE the revoked upsert — the atomic CAS must refuse the write.
        remote_access_authorization_service.complete_instance_binding_transition(
            instance_id="inst_123",
            instance_kind="personal",
            generation=started["generation"],
        )
        raise remote_access.BackendRequestError(403, {"error": "access_denied"})

    monkeypatch.setattr(remote_access, "_device_json_request", deny)

    result = remote_access._fetch_authorization_context(
        config,
        identity,
        record,
        now=now,
        observed_revision=41,
    )

    stored = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert result.reason == "instance_binding_changed" or (
        stored is not None and stored.get("authorization_state") != "revoked"
    )
    if stored is not None:
        assert stored.get("authorization_state") != "revoked"
    # The captured generation is no longer current.
    assert remote_access_authorization_service.current_instance_binding_generation() >= captured


def test_kindless_current_row_revalidates_after_known_kind_bootstrap(
    monkeypatch,
    tmp_path,
):
    """Regression PR #1606 r4: a released current row with no vibe_instance_kind
    must refresh on the first request after a known-kind Personal pairing
    bootstraps, instead of returning a kindless payload.
    """

    config = _paired_config(tmp_path)
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.save()
    cookie = _organization_cookie(config)
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    now = int(time.time())
    record = remote_access_authorization_service.load_reference_record(
        reference=identity["authorization_ref"],
        instance_id="inst_123",
        subject="user-1",
        now=now,
    )
    assert record is not None
    claims = dict(record["claims"])
    claims.pop("vibe_instance_kind", None)
    remote_access_authorization_service.upsert_scoped(
        reference=record["id"],
        instance_id="inst_123",
        subject="user-1",
        email=record["email"],
        scope_kind=record["scope_kind"],
        scope_ref=record["scope_ref"],
        authorization_state="current",
        claims=claims,
        last_checked_at=now,
        updated_at=now,
    )
    calls = []

    def refresh(_config, _method, _suffix, payload, **kwargs):
        calls.append(payload)
        return _authorization_context_response(
            config,
            payload,
            revision=41,
            instance_kind="personal",
        )

    monkeypatch.setattr(remote_access, "_device_json_request", refresh)

    result = remote_access.resolve_current_authorization(config, identity)

    assert result.current is True
    assert result.refreshed is True
    assert result.policy == "personal"
    assert len(calls) == 1
    assert result.payload is not None
    assert result.payload.get("vibe_instance_kind") == "personal"
