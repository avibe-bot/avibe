from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import update

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from core.services.skills import skill_resource_id
from storage import resource_access_service
from storage.db import get_cached_sqlite_engine
from storage.migrations import run_migrations
from storage.models import agent_sessions, agents, show_pages, vault_secrets
from vibe import remote_access


@dataclass
class _Response:
    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


def _config() -> V2Config:
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
    cloud.instance_id = "inst-1"
    cloud.instance_secret = "paired-device-secret"
    return config


def _seed_policy() -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id="agent-1",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
            policy_revision=1,
            last_applied_control_plane_revision=1,
        )


def _descriptor() -> dict[str, Any]:
    return {
        "resource_kind": "agent",
        "resource_id": "agent-1",
        "display_name": "Research agent",
        "owner_user_id": "owner-1",
        "metadata_revision": 1,
        "applied_acl_revision": 1,
        "access_level": "private",
        "group_ids": [],
    }


def _seed_named_resources() -> dict[str, str]:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    created_at = "2026-07-27T20:00:00.000001+00:00"
    resource_ids = {
        "agent": "agent-safe-name",
        "vault_secret": "vlt_safe_name",
        "skill": skill_resource_id(
            "codex",
            scope="global",
            project_dir=None,
            name="release-notes",
        ),
        "show_page": "ses-safe-name",
    }
    with engine.begin() as connection:
        connection.execute(
            agents.insert().values(
                id=resource_ids["agent"],
                name="Research Agent",
                normalized_name="research-agent",
                description="private-agent-description",
                backend="codex",
                model="private-agent-model",
                reasoning_effort="high",
                system_prompt="private-agent-prompt",
                enabled=1,
                source="user",
                source_ref="/private/agent/source",
                metadata_json='{"execution_output":"private-agent-output"}',
                created_at=created_at,
                updated_at=created_at,
            )
        )
        connection.execute(
            vault_secrets.insert().values(
                id=resource_ids["vault_secret"],
                name="PRODUCTION_API_KEY",
                tags='["private-vault-tag"]',
                kind="static",
                protection="standard",
                source="manual",
                ciphertext="private-vault-ciphertext",
                nonce="private-vault-nonce",
                wrap_meta="private-vault-wrap",
                public_meta='{"description":"private-vault-description"}',
                policy='{"allowed_hosts":["private.example"]}',
                use_count=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        connection.execute(
            agent_sessions.insert().values(
                id=resource_ids["show_page"],
                scope_id=None,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="private-show-anchor",
                workdir="/private/show/workspace",
                native_session_id="private-native-session",
                title="Quarterly Results",
                status="active",
                metadata_json='{"execution_output":"private-show-output"}',
                created_at=created_at,
                updated_at=created_at,
                last_active_at=created_at,
            )
        )
        connection.execute(
            show_pages.insert().values(
                session_id=resource_ids["show_page"],
                access_mode="private",
                access_revision=0,
                share_id=None,
                offline_at=None,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        for resource_kind, resource_id in resource_ids.items():
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind=resource_kind,
                resource_id=resource_id,
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="private",
                policy_revision=3,
                last_applied_control_plane_revision=2,
            )
    return resource_ids


def test_local_descriptors_resolve_all_resource_names_without_content() -> None:
    resource_ids = _seed_named_resources()

    descriptors = remote_access._local_policy_resource_descriptors("org-1")
    by_kind = {descriptor["resource_kind"]: descriptor for descriptor in descriptors}

    assert set(by_kind) == {"agent", "vault_secret", "skill", "show_page"}
    assert by_kind["agent"]["display_name"] == "Research Agent"
    assert by_kind["vault_secret"]["display_name"] == "PRODUCTION_API_KEY"
    assert by_kind["skill"]["display_name"] == "release-notes"
    assert by_kind["show_page"]["display_name"] == "Quarterly Results"
    allowed_descriptor_fields = {
        "resource_id",
        "resource_kind",
        "display_name",
        "owner_user_id",
        "metadata_revision",
        "applied_acl_revision",
        "access_level",
        "group_ids",
        "sync_status",
    }
    for resource_kind, resource_id in resource_ids.items():
        assert set(by_kind[resource_kind]) == allowed_descriptor_fields
        assert by_kind[resource_kind]["resource_id"] == resource_id
        assert by_kind[resource_kind]["metadata_revision"] >= 3

    serialized = repr(descriptors)
    for forbidden in (
        "private-agent-description",
        "private-agent-model",
        "private-agent-prompt",
        "private-agent-output",
        "/private/agent/source",
        "private-vault-tag",
        "private-vault-ciphertext",
        "private-vault-nonce",
        "private-vault-wrap",
        "private-vault-description",
        "private.example",
        "private-show-anchor",
        "/private/show/workspace",
        "private-native-session",
        "private-show-output",
    ):
        assert forbidden not in serialized


def test_local_descriptor_title_revision_and_safe_fallback() -> None:
    resource_ids = _seed_named_resources()
    first = {
        item["resource_kind"]: item
        for item in remote_access._local_policy_resource_descriptors("org-1")
    }["show_page"]
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        connection.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == resource_ids["show_page"])
            .values(
                title="Executive Overview",
                updated_at="2026-07-27T20:00:00.000002+00:00",
            )
        )

    renamed = {
        item["resource_kind"]: item
        for item in remote_access._local_policy_resource_descriptors("org-1")
    }["show_page"]
    assert renamed["display_name"] == "Executive Overview"
    assert renamed["metadata_revision"] > first["metadata_revision"]

    with engine.begin() as connection:
        connection.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == resource_ids["show_page"])
            .values(
                title="Q3 / Launch: Ops",
                updated_at="2026-07-27T20:00:00.000003+00:00",
            )
        )
    separated = {
        item["resource_kind"]: item
        for item in remote_access._local_policy_resource_descriptors("org-1")
    }["show_page"]
    assert separated["display_name"] == "Q3 / Launch: Ops"
    assert separated["metadata_revision"] > renamed["metadata_revision"]

    with engine.begin() as connection:
        connection.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == resource_ids["show_page"])
            .values(
                title="Quarterly\nResults",
                updated_at="2026-07-27T20:00:00.000004+00:00",
            )
        )
    unsafe = {
        item["resource_kind"]: item
        for item in remote_access._local_policy_resource_descriptors("org-1")
    }["show_page"]
    assert unsafe["display_name"] == resource_ids["show_page"]
    assert unsafe["metadata_revision"] > separated["metadata_revision"]
    assert "Quarterly\nResults" not in repr(unsafe)


def test_local_descriptors_omit_missing_source_rows() -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        for resource_kind, resource_id in (
            ("agent", "missing-agent"),
            ("vault_secret", "missing-vault"),
            ("skill", "missing-skill"),
            ("show_page", "missing-show-page"),
        ):
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind=resource_kind,
                resource_id=resource_id,
                organization_id="org-1",
                owner_user_id="owner-1",
                access_level="private",
            )

    assert remote_access._local_policy_resource_descriptors("org-1") == []


def test_sync_applies_only_newer_intent_and_acknowledges_exact_revision(monkeypatch) -> None:
    _seed_policy()
    config = _config()
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "resource_kind": "agent",
                            "resource_id": "agent-1",
                            "revision": 2,
                            "access_level": "scope",
                            "group_ids": ["group-engineering"],
                        }
                    ],
                }
            ),
            _Response({"resource": {"resource_kind": "agent", "resource_id": "agent-1"}}),
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "resource_kind": "agent",
                            "resource_id": "agent-1",
                            "revision": 1,
                            "access_level": "private",
                            "group_ids": [],
                        }
                    ],
                }
            ),
        ]
    )

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(remote_access.requests, "request", request)

    first = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )
    second = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )

    assert first["ok"] is True
    assert first["organizations"][0]["applied"] == 1
    assert second["ok"] is True
    assert second["organizations"][0]["skipped"] == 1
    assert [call["method"] for call in calls] == ["PUT", "GET", "POST", "PUT", "GET"]
    assert calls[0]["headers"]["X-Vibe-Device-Secret"] == "paired-device-secret"
    assert set(calls[0]["json"]["resources"][0]) <= {
        "resource_id",
        "resource_kind",
        "display_name",
        "owner_user_id",
        "metadata_revision",
        "applied_acl_revision",
        "access_level",
        "group_ids",
        "sync_status",
    }
    assert calls[2]["json"] == {
        "resource_kind": "agent",
        "resource_id": "agent-1",
        "revision": 2,
        "outcome": "applied",
    }

    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "scope"
    assert policy["group_ids"] == ["group-engineering"]
    assert policy["last_applied_control_plane_revision"] == 2


def test_sync_offline_retains_last_applied_policy(monkeypatch) -> None:
    _seed_policy()
    config = _config()

    def request(*_args: Any, **_kwargs: Any):
        raise remote_access.requests.ConnectionError("offline")

    monkeypatch.setattr(remote_access.requests, "request", request)

    result = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )

    assert result["ok"] is False
    assert result["organizations"][0]["error"] == "resource_acl_sync_failed"
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "private"
    assert policy["last_applied_control_plane_revision"] == 1


def test_sync_publishes_empty_index_after_last_organization_policy_is_deleted(monkeypatch) -> None:
    _seed_policy()
    config = _config()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        assert resource_access_service.delete_resource_policy(
            connection,
            "agent",
            "agent-1",
        )
    assert resource_access_service.list_resource_organization_ids() == ["org-1"]

    def offline_request(*_args: Any, **_kwargs: Any):
        raise remote_access.requests.ConnectionError("offline")

    monkeypatch.setattr(remote_access.requests, "request", offline_request)
    offline = remote_access.sync_resource_acl_once(config)
    assert offline["ok"] is False
    assert resource_access_service.list_resource_organization_ids() == ["org-1"]

    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [],
                }
            ),
        ]
    )

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(remote_access.requests, "request", request)

    result = remote_access.sync_resource_acl_once(config)

    assert result["ok"] is True
    assert result["organizations"][0]["organization_id"] == "org-1"
    assert [call["method"] for call in calls] == ["PUT", "GET"]
    assert calls[0]["json"]["resources"] == []
    assert resource_access_service.list_resource_organization_ids() == []


def test_malformed_intent_keeps_empty_organization_queued_for_retry(monkeypatch) -> None:
    _seed_policy()
    config = _config()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        assert resource_access_service.delete_resource_policy(connection, "agent", "agent-1")

    responses = iter(
        [
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [{"resource_kind": "invalid"}],
                }
            ),
        ]
    )
    monkeypatch.setattr(remote_access.requests, "request", lambda *_args, **_kwargs: next(responses))

    result = remote_access.sync_resource_acl_once(config)

    assert result["ok"] is False
    assert result["organizations"][0]["rejected"] == 1
    assert result["organizations"][0]["ack_errors"] == 1
    assert resource_access_service.list_resource_organization_ids() == ["org-1"]


def test_applied_acl_changes_publish_one_authorization_change_for_all_resource_caches(monkeypatch) -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    resource_ids = {
        "agent": "agent-cache-revocation",
        "skill": "skill-cache-revocation",
        "vault_secret": "vault-cache-revocation",
        "show_page": "show-cache-revocation",
    }
    with engine.begin() as connection:
        for resource_kind, resource_id in resource_ids.items():
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind=resource_kind,
                resource_id=resource_id,
                organization_id="org-cache-revocation",
                owner_user_id="owner-1",
                access_level="public",
                policy_revision=1,
                last_applied_control_plane_revision=1,
            )

    monkeypatch.setattr(
        remote_access,
        "publish_resource_index",
        lambda *_args, **_kwargs: {
            "organization_id": "org-cache-revocation",
            "resources": [],
        },
    )
    monkeypatch.setattr(
        remote_access,
        "pull_resource_acl_intents",
        lambda *_args, **_kwargs: {
            "organization_id": "org-cache-revocation",
            "intents": [
                {
                    "resource_kind": resource_kind,
                    "resource_id": resource_id,
                    "revision": 2,
                    "access_level": "private",
                    "group_ids": [],
                }
                for resource_kind, resource_id in resource_ids.items()
            ],
        },
    )
    monkeypatch.setattr(
        remote_access,
        "acknowledge_resource_acl_intent",
        lambda *_args, **_kwargs: {},
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: events.append((event_type, data)),
    )

    result = remote_access._sync_one_organization(
        None,
        organization_id="org-cache-revocation",
        resources=[],
    )

    assert result["ok"] is True
    assert result["applied"] == 4
    assert events == [
        (
            "authorization.changed",
            {
                "project_ids": [],
                "resource_kinds": ["agent", "show_page", "skill", "vault_secret"],
            },
        )
    ]


def test_show_page_acl_widening_publishes_authorization_change(monkeypatch) -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="show_page",
            resource_id="show-page-widening",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
            policy_revision=1,
            last_applied_control_plane_revision=1,
        )
    monkeypatch.setattr(
        remote_access,
        "publish_resource_index",
        lambda *_args, **_kwargs: {"organization_id": "org-1", "resources": []},
    )
    monkeypatch.setattr(
        remote_access,
        "pull_resource_acl_intents",
        lambda *_args, **_kwargs: {
            "organization_id": "org-1",
            "intents": [
                {
                    "resource_kind": "show_page",
                    "resource_id": "show-page-widening",
                    "revision": 2,
                    "access_level": "public",
                    "group_ids": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        remote_access,
        "acknowledge_resource_acl_intent",
        lambda *_args, **_kwargs: {},
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "vibe.sse_broker.broker.publish",
        lambda event_type, data: events.append((event_type, data)),
    )

    result = remote_access._sync_one_organization(
        _config(),
        organization_id="org-1",
        resources=[],
    )

    assert result["ok"] is True
    assert result["applied"] == 1
    assert events == [
        (
            "authorization.changed",
            {"project_ids": [], "resource_kinds": ["show_page"]},
        )
    ]


def test_transient_apply_failure_leaves_intent_unacknowledged(monkeypatch) -> None:
    _seed_policy()
    config = _config()
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "resource_kind": "agent",
                            "resource_id": "agent-1",
                            "revision": 2,
                            "access_level": "public",
                            "group_ids": [],
                        }
                    ],
                }
            ),
        ]
    )

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(remote_access.requests, "request", request)
    monkeypatch.setattr(
        resource_access_service,
        "apply_control_plane_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database is busy")),
    )

    result = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )

    organization = result["organizations"][0]
    assert result["ok"] is False
    assert organization["rejected"] == 0
    assert organization["acknowledged"] == 0
    assert organization["ack_errors"] == 1
    assert [call["method"] for call in calls] == ["PUT", "GET"]

    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "private"
    assert policy["last_applied_control_plane_revision"] == 1


def test_poll_delay_honors_successful_organization_backoff() -> None:
    result = {
        "ok": False,
        "organizations": [
            {"organization_id": "org-1", "ok": True, "poll_after_seconds": 45},
            {"organization_id": "org-2", "ok": True, "poll_after_seconds": 120},
            {"organization_id": "org-3", "ok": False, "poll_after_seconds": 300},
        ],
    }

    assert remote_access._resource_acl_poll_delay(result, 30) == 120


def test_poll_delay_falls_back_without_valid_successful_result() -> None:
    result = {
        "ok": False,
        "organizations": [
            {"organization_id": "org-1", "ok": False, "poll_after_seconds": 120},
            {"organization_id": "org-2", "ok": True, "poll_after_seconds": 0},
            {"organization_id": "org-3", "ok": True, "poll_after_seconds": "invalid"},
        ],
    }

    assert remote_access._resource_acl_poll_delay(result, 30) == 30
