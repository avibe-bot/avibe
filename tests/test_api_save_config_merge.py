from __future__ import annotations

import ast
import copy
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import fields
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import (
    _FIELD_SCOPED_RECOVERY_SECTIONS,
    AudioAsrConfig,
    RuntimeConfig,
    UiConfig,
    V2Config,
    VibeCloudRemoteAccessConfig,
)
from core.audio_asr import AudioAsrService
from vibe import api, remote_access


def _full_config_payload() -> dict:
    return {
        "platform": "discord",
        "mode": "self_host",
        "version": "v2",
        "slack": {
            "bot_token": "",
            "app_token": None,
            "signing_secret": None,
            "team_id": None,
            "team_name": None,
            "app_id": None,
            "require_mention": False,
            "disable_link_unfurl": False,
        },
        "discord": {
            "bot_token": "discord-token-1234567890",
            "application_id": None,
            "guild_allowlist": ["754776951587340359"],
            "guild_denylist": [],
            "require_mention": False,
        },
        "lark": {
            "app_id": "",
            "app_secret": "",
            "require_mention": False,
            "domain": "feishu",
        },
        "runtime": {
            "default_cwd": "/tmp/workdir",
            "log_level": "INFO",
        },
        "agents": {
            "default_backend": "codex",
            "opencode": {
                "enabled": True,
                "cli_path": "opencode",
                "default_agent": None,
                "default_model": None,
                "default_reasoning_effort": None,
                "error_retry_limit": 1,
                "active_turn_timeout_seconds": 7200,
            },
            "claude": {
                "enabled": True,
                "cli_path": "claude",
                "default_model": None,
            },
            "codex": {
                "enabled": True,
                "cli_path": "codex",
                "default_model": "gpt-5.4",
            },
        },
        "gateway": None,
        "ui": {
            "setup_host": "127.0.0.1",
            "setup_port": 5123,
            "open_browser": False,
        },
        "update": {
            "auto_update": False,
            "check_interval_minutes": 0,
            "idle_minutes": 30,
            "notify_admins": False,
        },
        "ack_mode": "reaction",
        "show_duration": True,
        "include_time_info": True,
        "include_user_info": True,
        "reply_enhancements": True,
        "show_pages_prompt": True,
        "language": "en",
    }


@pytest.mark.parametrize("prepared", [False, True])
def test_config_save_initializes_real_state_with_or_without_schema_preparation(
    monkeypatch, tmp_path, sqlite_schema_db_factory, prepared,
):
    from storage import importer
    from tests.conftest import _is_default_sqlite_call

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert not any(_is_default_sqlite_call(node) for node in ast.walk(tree) if isinstance(node, ast.Call))
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "state" / "vibe.sqlite"
    assert not db_path.exists()
    if prepared:
        sqlite_schema_db_factory(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone() == (0,)
            markers = {row[0] for row in connection.execute("SELECT key FROM state_meta")}
            assert importer.JSON_IMPORT_MARKER not in markers
            assert importer.BACKGROUND_IMPORT_MARKER not in markers

    imported_paths = []
    original_write = importer._write_parsed_state

    def observe_import(target_db, parsed):
        imported_paths.append(target_db)
        return original_write(target_db, parsed)

    monkeypatch.setattr(importer, "_write_parsed_state", observe_import)
    created = api.save_config(_full_config_payload())
    updated = api.save_config({"show_duration": False})
    assert created.show_duration is True
    assert updated.show_duration is False
    assert imported_paths == [db_path.resolve()]
    with closing(sqlite3.connect(db_path)) as connection:
        markers = {row[0] for row in connection.execute("SELECT key FROM state_meta")}
        assert {importer.JSON_IMPORT_MARKER, importer.BACKGROUND_IMPORT_MARKER} <= markers
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    store = api.VibeAgentStore()
    try:
        enabled = {
            backend for backend, options in _full_config_payload()["agents"].items()
            if isinstance(options, dict) and options.get("enabled")
        }
        assert {agent.backend for agent in store.list_agents() if agent.enabled} == enabled
        assert store.get_default_agent_name() in {agent.name for agent in store.list_agents()}
    finally:
        store.close()
    settings = api.SettingsStore.get_instance()
    assert settings.has_guild_scope_for_platform("discord")
    assert set(settings.get_guilds_for_platform("discord")) == set(
        _full_config_payload()["discord"]["guild_allowlist"]
    )


def test_save_config_merges_partial_payload(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    original = api.save_config(_full_config_payload())
    assert original.show_duration is True
    assert original.include_time_info is True
    assert original.update.auto_update is False
    assert all(
        not hasattr(getattr(original.agents, backend), "default_model")
        for backend in ("claude", "codex", "opencode")
    )

    updated = api.save_config({"show_duration": False, "include_time_info": False, "update": {"auto_update": True}})

    assert updated.show_duration is False
    assert updated.include_time_info is False
    assert updated.update.auto_update is True
    assert updated.platform == "discord"
    assert updated.discord is not None
    assert updated.discord.bot_token == "discord-token-1234567890"
    assert updated.runtime.default_cwd == "/tmp/workdir"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_events_trace_retention_enabled", "false"),
        ("agent_events_trace_retention_enabled", 1),
        ("agent_events_trace_retention_days", "90"),
        ("agent_events_trace_retention_days", 0),
        ("agent_events_trace_retention_days", True),
        ("agent_events_trace_retention_days", 1_000_000),
    ],
)
def test_save_config_rejects_malformed_trace_retention_policy(monkeypatch, tmp_path, field, value):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["runtime"][field] = value

    with pytest.raises(ValueError, match=f"runtime\\.{field}"):
        api.save_config(payload)


def test_save_config_seeds_default_and_drops_retired_model_key(monkeypatch, tmp_path):
    """Regression: a fresh install (no config file yet) must accept the wizard's
    reused provider-config modal POSTing only ``{"agents": ...}``.

    Before the default-seed fix the partial payload went straight into
    ``V2Config.from_payload`` and raised (missing ``mode``/``runtime``), so the
    advertised first-run "Configure provider" flow failed until a config existed.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    with pytest.raises(FileNotFoundError):
        api.load_config()  # precondition: truly fresh, no config file

    created = api.save_config(
        {"agents": {"claude": {"enabled": True, "cli_path": "claude", "default_model": "sonnet"}}}
    )

    # The partial save merges onto the workbench-only default and persists.
    assert created.mode == "self_host"
    assert created.agents.claude.enabled is True
    assert not hasattr(created.agents.claude, "default_model")
    # Configuring a provider mid-wizard must not complete setup...
    assert created.setup_completed is False
    assert created.setup_state()["needs_setup"] is True
    # ...nor leave a phantom Slack transport: the seeded base is workbench-only.
    assert created.platforms.enabled == []
    assert created.platforms.primary == "avibe"


def test_save_config_defaults_show_duration_to_false_for_new_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload.pop("show_duration")

    created = api.save_config(payload)

    assert created.show_duration is False


def test_save_config_defaults_include_time_info_to_true_for_new_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload.pop("include_time_info")

    created = api.save_config(payload)

    assert created.include_time_info is True


def test_save_config_accepts_typing_ack_mode(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    updated = api.save_config({**_full_config_payload(), "ack_mode": "typing"})

    assert updated.ack_mode == "typing"


def test_remote_access_transport_protocol_is_normalized_and_validated() -> None:
    payload = _full_config_payload()
    payload["remote_access"] = {
        "vibe_cloud": {
            "transport_protocol": " HTTP2 ",
            "auto_recovery": "false",
            "optimization_profile": " LOW_LATENCY ",
            "edge_ip_version": " 4 ",
            "edge_bind_address": " 192.0.2.10 ",
        }
    }

    config = V2Config.from_payload(payload)

    assert config.remote_access.vibe_cloud.transport_protocol == "http2"
    assert config.remote_access.vibe_cloud.auto_recovery is False
    assert config.remote_access.vibe_cloud.optimization_profile == "low_latency"
    assert config.remote_access.vibe_cloud.edge_ip_version == "4"
    assert config.remote_access.vibe_cloud.edge_bind_address == "192.0.2.10"

    payload["remote_access"]["vibe_cloud"]["transport_protocol"] = "tcp"
    with pytest.raises(ValueError, match="transport_protocol"):
        V2Config.from_payload(payload)

    payload["remote_access"]["vibe_cloud"]["transport_protocol"] = "auto"
    payload["remote_access"]["vibe_cloud"]["optimization_profile"] = "fastest"
    with pytest.raises(ValueError, match="optimization_profile"):
        V2Config.from_payload(payload)

    payload["remote_access"]["vibe_cloud"]["optimization_profile"] = "balanced"
    payload["remote_access"]["vibe_cloud"]["edge_bind_address"] = "wifi"
    with pytest.raises(ValueError, match="edge_bind_address"):
        V2Config.from_payload(payload)


def test_remote_access_legacy_config_keeps_cloudflared_ipv4_default() -> None:
    payload = _full_config_payload()
    payload["remote_access"] = {"vibe_cloud": {"transport_protocol": "auto"}}

    config = V2Config.from_payload(payload)

    assert config.remote_access.vibe_cloud.edge_ip_version == "4"


def test_save_config_rejects_unassigned_remote_access_bind_address(
    monkeypatch,
    tmp_path,
    sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())
    monkeypatch.setattr(
        remote_access,
        "network_interfaces",
        lambda: {
            "ok": True,
            "interfaces": [
                {
                    "id": "en0:192.0.2.10",
                    "name": "en0",
                    "address": "192.0.2.10",
                    "ip_version": "4",
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="active network interface"):
        api.save_config(
            {
                "remote_access": {
                    "vibe_cloud": {
                        "edge_ip_version": "4",
                        "edge_bind_address": "192.0.2.20",
                    }
                }
            }
        )

    assert V2Config.load().remote_access.vibe_cloud.edge_bind_address == ""


def test_save_config_rejects_remote_access_bind_family_mismatch(
    monkeypatch,
    tmp_path,
    sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())
    monkeypatch.setattr(
        remote_access,
        "network_interfaces",
        lambda: {
            "ok": True,
            "interfaces": [
                {
                    "id": "en0:192.0.2.10",
                    "name": "en0",
                    "address": "192.0.2.10",
                    "ip_version": "4",
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="must match"):
        api.save_config(
            {
                "remote_access": {
                    "vibe_cloud": {
                        "edge_ip_version": "6",
                        "edge_bind_address": "192.0.2.10",
                    }
                }
            }
        )

    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.edge_ip_version == "4"
    assert saved.edge_bind_address == ""


def test_generic_config_save_rejects_connector_control_changes(
    monkeypatch,
    tmp_path,
    sqlite_schema_db_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())

    with pytest.raises(ValueError, match="/api/remote-access/settings"):
        api.save_config(
            {
                "remote_access": {
                    "vibe_cloud": {"transport_protocol": "http2"}
                }
            },
            generic_remote_access=True,
        )

    assert V2Config.load().remote_access.vibe_cloud.transport_protocol == "auto"


def test_generic_runtime_save_validates_an_unchanged_stale_bind_address(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {
            "enabled": True,
            "public_url": "https://old.avibe.bot",
            "edge_bind_address": "192.0.2.10",
        },
    }
    V2Config.from_payload(payload).save()
    monkeypatch.setattr(
        remote_access,
        "network_interfaces",
        lambda: {"ok": True, "interfaces": []},
    )

    with pytest.raises(ValueError, match="active network interface"):
        api.save_config(
            {
                "remote_access": {
                    "vibe_cloud": {"public_url": "https://new.avibe.bot"}
                }
            },
            generic_remote_access=True,
        )

    assert V2Config.load().remote_access.vibe_cloud.public_url == "https://old.avibe.bot"


def test_generic_policy_save_does_not_validate_an_unchanged_stale_bind_address(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {
            "enabled": True,
            "edge_bind_address": "192.0.2.10",
        },
    }
    V2Config.from_payload(payload).save()
    monkeypatch.setattr(
        remote_access,
        "network_interfaces",
        lambda: (_ for _ in ()).throw(
            AssertionError("policy-only saves must not validate Connector binding")
        ),
    )

    updated = api.save_config(
        {"remote_access": {"vibe_cloud": {"auto_recovery": False}}},
        generic_remote_access=True,
    )

    assert updated.remote_access.vibe_cloud.auto_recovery is False


def test_save_config_merges_audio_asr_settings(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    created = api.save_config(_full_config_payload())
    assert created.audio_asr.enabled is True
    assert created.audio_asr.enabled_configured is False
    assert created.audio_asr.echo_transcript is True

    updated = api.save_config({"audio_asr": {"enabled": False, "enabled_configured": True, "echo_transcript": False}})
    payload = api.config_to_payload(updated)

    assert updated.audio_asr.enabled is False
    assert updated.audio_asr.enabled_configured is True
    assert updated.audio_asr.echo_transcript is False
    assert updated.audio_asr.endpoint_path == "/v1/audio/transcriptions"
    assert payload["audio_asr"]["enabled"] is False
    assert payload["audio_asr"]["enabled_configured"] is True
    assert payload["audio_asr"]["echo_transcript"] is False


def test_save_config_marks_explicit_audio_asr_disable_patch(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    api.save_config(_full_config_payload())

    updated = api.save_config({"audio_asr": {"enabled": False}})

    assert updated.audio_asr.enabled is False
    assert updated.audio_asr.enabled_configured is True


def test_config_load_defaults_missing_audio_asr_to_enabled():
    payload = _full_config_payload()
    payload.pop("audio_asr", None)

    created = V2Config.from_payload(payload)

    assert created.audio_asr.enabled is True
    assert created.audio_asr.enabled_configured is False


def test_config_payload_defaults_instance_name_to_remote_access_slug(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["instance_name"] == ""
    assert payload["ui"]["default_instance_name"] == "alex"
    assert payload["ui"]["system_hostname"] == "macbook"


def test_config_payload_default_instance_name_falls_back_to_hostname(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.public_url = "https://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_config_payload_default_instance_name_ignores_invalid_remote_url(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "http://alex-app.avibe.bot"

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_config_payload_default_instance_name_ignores_malformed_remote_url(monkeypatch):
    monkeypatch.setattr(api, "_system_hostname", lambda: "macbook")
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://["

    payload = api.config_to_payload(config)

    assert payload["ui"]["default_instance_name"] == "macbook"


def test_save_config_preserves_show_pages_prompt_toggle(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    created = api.save_config(_full_config_payload())
    assert created.show_pages_prompt is True

    updated = api.save_config({"show_pages_prompt": False})
    payload = api.config_to_payload(updated)

    assert updated.show_pages_prompt is False
    assert payload["show_pages_prompt"] is False


def test_save_config_preserves_status_bubble_settings_on_partial_save(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """An unrelated partial save must NOT reset agent_progress_style / intervals.

    Regression for the config_to_payload omission that wiped these on any UI save.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    full = _full_config_payload()
    full["agent_progress_style"] = "verbose"
    full["agent_status_heartbeat_ms"] = 12000
    created = api.save_config(full)
    assert created.agent_progress_style == "verbose"
    assert created.agent_status_heartbeat_ms == 12000

    # Toggle an unrelated field — the status-bubble settings must survive.
    updated = api.save_config({"show_duration": False})
    payload = api.config_to_payload(updated)

    assert updated.agent_progress_style == "verbose"
    assert updated.agent_status_heartbeat_ms == 12000
    assert payload["agent_progress_style"] == "verbose"
    assert payload["agent_status_heartbeat_ms"] == 12000


def test_save_config_preserves_harness_runtime_knobs_on_partial_save(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """The config-only Harness knobs must survive an unrelated UI save (Codex P1).

    ``config_to_payload`` is the deep-merge base for every ``/api/config`` save, so a
    ``runtime`` key it omits is absent from the merged payload and ``from_payload``
    rebuilds it from the dataclass default: a ``harness_prompt_echo: false`` opt-out
    came back enabled after any unrelated settings change. Same shape as the
    status-bubble regression above.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    full = _full_config_payload()
    full["runtime"]["harness_prompt_echo"] = False
    full["runtime"]["harness_run_queued_ttl_seconds"] = 4242
    created = api.save_config(full)
    assert created.runtime.harness_prompt_echo is False
    assert created.runtime.harness_run_queued_ttl_seconds == 4242

    updated = api.save_config({"show_duration": False})
    payload = api.config_to_payload(updated)

    assert updated.runtime.harness_prompt_echo is False
    assert updated.runtime.harness_run_queued_ttl_seconds == 4242
    assert payload["runtime"]["harness_prompt_echo"] is False
    assert payload["runtime"]["harness_run_queued_ttl_seconds"] == 4242


def test_show_page_api_timeout_defaults_round_trips_and_survives_partial_save(
    monkeypatch,
    tmp_path,
    sqlite_schema_db_factory,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    defaulted = V2Config.from_payload(_full_config_payload())
    assert defaulted.runtime.show_page_api_timeout_seconds == 90.0

    full = _full_config_payload()
    full["runtime"]["show_page_api_timeout_seconds"] = 135
    created = api.save_config(full)
    assert created.runtime.show_page_api_timeout_seconds == 135.0

    updated = api.save_config({"show_duration": False})
    payload = api.config_to_payload(updated)
    assert updated.runtime.show_page_api_timeout_seconds == 135.0
    assert payload["runtime"]["show_page_api_timeout_seconds"] == 135.0


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_show_page_api_timeout_rejects_values_that_do_not_name_a_deadline(value):
    payload = _full_config_payload()
    payload["runtime"]["show_page_api_timeout_seconds"] = value

    with pytest.raises(
        ValueError,
        match="Config 'runtime.show_page_api_timeout_seconds'",
    ):
        V2Config.from_payload(payload)


def test_config_load_defaults_missing_show_pages_prompt_to_enabled():
    payload = _full_config_payload()
    payload.pop("show_pages_prompt")

    created = V2Config.from_payload(payload)

    assert created.show_pages_prompt is True


def test_config_load_preserves_pre_upgrade_audio_asr_false_as_opt_out():
    payload = _full_config_payload()
    payload["audio_asr"] = {"enabled": False, "echo_transcript": True}

    created = V2Config.from_payload(payload)

    assert created.audio_asr.enabled is False
    assert created.audio_asr.enabled_configured is True


def test_save_config_preserves_explicit_audio_asr_opt_out(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    payload = _full_config_payload()
    payload["audio_asr"] = {
        "enabled": False,
        "enabled_configured": True,
        "echo_transcript": True,
    }

    created = api.save_config(payload)

    assert created.audio_asr.enabled is False
    assert created.audio_asr.enabled_configured is True


def test_config_to_payload_redacts_remote_access_secrets_and_save_preserves_them(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    payload = _full_config_payload()
    payload["remote_access"] = {
        "provider": "vibe_cloud",
        "vibe_cloud": {
            "enabled": True,
            "backend_url": "https://avibe.bot",
            "public_url": "https://alex.avibe.bot",
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://avibe.bot",
            "authorization_endpoint": "https://avibe.bot/oauth/authorize",
            "token_endpoint": "https://avibe.bot/oauth/token",
            "jwks_uri": "https://avibe.bot/oauth/jwks.json",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
            "session_secret": "session-secret",
        },
    }
    created = api.save_config(payload)

    redacted = api.config_to_payload(created)
    cloud_payload = redacted["remote_access"]["vibe_cloud"]
    updated = api.save_config({**redacted, "show_duration": False})

    assert "tunnel_token" not in cloud_payload
    assert "instance_secret" not in cloud_payload
    assert "session_secret" not in cloud_payload
    assert updated.remote_access.vibe_cloud.tunnel_token == "tunnel-token"
    assert updated.remote_access.vibe_cloud.instance_secret == "instance-secret"
    assert updated.remote_access.vibe_cloud.session_secret == "session-secret"


def test_config_to_payload_redacts_platform_and_gateway_secrets_and_save_preserves_them(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    payload = _full_config_payload()
    payload["slack"] = {
        **payload["slack"],
        "bot_token": "xoxb-secret-token",
        "app_token": "xapp-secret-token",
        "signing_secret": "slack-signing-secret",
    }
    payload["telegram"] = {
        "bot_token": "123456:telegram-secret",
        "webhook_secret_token": "telegram-webhook-secret",
        "require_mention": True,
        "forum_auto_topic": True,
        "use_webhook": True,
    }
    payload["lark"] = {
        "app_id": "cli_lark_id",
        "app_secret": "lark-secret",
        "require_mention": False,
        "domain": "feishu",
    }
    payload["wechat"] = {
        "bot_token": "wechat-secret",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "require_mention": False,
    }
    payload["gateway"] = {
        "relay_url": "https://relay.example",
        "workspace_token": "workspace-secret",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }

    created = api.save_config(payload)
    redacted = api.config_to_payload(created)

    assert redacted["slack"]["bot_token_length"] == len("xoxb-secret-token")
    assert redacted["slack"]["has_bot_token"] is True
    assert "bot_token" not in redacted["slack"]
    assert redacted["slack"]["has_app_token"] is True
    assert "app_token" not in redacted["slack"]
    assert redacted["slack"]["has_signing_secret"] is True
    assert "signing_secret" not in redacted["slack"]
    assert redacted["discord"]["has_bot_token"] is True
    assert "bot_token" not in redacted["discord"]
    assert redacted["telegram"]["has_bot_token"] is True
    assert "bot_token" not in redacted["telegram"]
    assert redacted["telegram"]["has_webhook_secret_token"] is True
    assert "webhook_secret_token" not in redacted["telegram"]
    assert redacted["lark"]["app_id"] == "cli_lark_id"
    assert redacted["lark"]["has_app_secret"] is True
    assert "app_secret" not in redacted["lark"]
    assert redacted["wechat"]["has_bot_token"] is True
    assert "bot_token" not in redacted["wechat"]
    assert redacted["gateway"]["has_workspace_token"] is True
    assert "workspace_token" not in redacted["gateway"]
    assert redacted["gateway"]["has_client_secret"] is True
    assert "client_secret" not in redacted["gateway"]

    included = api.config_to_payload(created, include_secrets=True)
    assert included["slack"]["bot_token"] == "xoxb-secret-token"
    assert included["gateway"]["client_secret"] == "client-secret"

    redacted["show_duration"] = False
    updated = api.save_config(redacted)

    assert updated.slack.bot_token == "xoxb-secret-token"
    assert updated.slack.app_token == "xapp-secret-token"
    assert updated.slack.signing_secret == "slack-signing-secret"
    assert updated.discord.bot_token == "discord-token-1234567890"
    assert updated.telegram.bot_token == "123456:telegram-secret"
    assert updated.telegram.webhook_secret_token == "telegram-webhook-secret"
    assert updated.lark.app_secret == "lark-secret"
    assert updated.wechat.bot_token == "wechat-secret"
    assert updated.gateway is not None
    assert updated.gateway.workspace_token == "workspace-secret"
    assert updated.gateway.client_secret == "client-secret"


def test_save_config_accepts_slack_disable_link_unfurl(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    payload = _full_config_payload()
    payload["slack"]["disable_link_unfurl"] = True

    updated = api.save_config(payload)

    assert updated.slack.disable_link_unfurl is True


def test_save_config_preserves_platforms_metadata(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    payload = _full_config_payload()
    payload["slack"]["bot_token"] = "xoxb-valid-token"
    payload["slack"]["app_token"] = "xapp-valid-token"
    updated = api.save_config(
        {
            **payload,
            "wechat": {
                "corp_id": "wk123",
                "agent_id": "agent1",
                "secret": "sec",
                "token": "tok",
                "aes_key": "aes",
            },
            "platforms": {"enabled": ["slack", "discord", "wechat"], "primary": "discord"},
        }
    )

    assert updated.platform == "discord"
    assert updated.platforms.primary == "discord"
    assert updated.platforms.enabled == ["slack", "discord", "wechat"]


def test_save_config_migrates_legacy_single_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    updated = api.save_config(_full_config_payload())
    payload = api.config_to_payload(updated)

    assert updated.platforms.primary == "discord"
    assert updated.platforms.enabled == ["discord"]
    assert payload["platforms"] == {"enabled": ["discord"], "primary": "discord"}


def test_save_config_rejects_enabled_platform_without_config(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    import pytest

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["lark"] = None
    created = api.save_config(payload)
    assert created.platforms.enabled == []
    assert created.lark is None

    with pytest.raises(ValueError, match="Config 'lark' must be provided when lark is enabled"):
        api.save_config({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}})


def test_save_config_rejects_enabled_platform_without_runtime_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    with pytest.raises(ValueError, match="Config 'lark.app_id', 'lark.app_secret' must be provided"):
        api.save_config(
            {
                **_full_config_payload(),
                "platform": "lark",
                "platforms": {"enabled": ["lark"], "primary": "lark"},
                "lark": {},
            }
        )

    with pytest.raises(ValueError, match="Config 'slack.bot_token' must be provided"):
        api.save_config(
            {
                **_full_config_payload(),
                "platform": "slack",
                "platforms": {"enabled": ["slack"], "primary": "slack"},
                "slack": {"bot_token": "", "app_token": "xapp-valid"},
            }
        )


def test_save_config_allows_slack_bot_token_only_runtime_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = {
        **_full_config_payload(),
        "platform": "slack",
        "platforms": {"enabled": ["slack"], "primary": "slack"},
        "slack": {"bot_token": "xoxb-valid", "app_token": ""},
    }

    config = api.save_config(payload)

    assert config.platforms.enabled == ["slack"]
    assert config.slack.bot_token == "xoxb-valid"
    assert config.slack.app_token == ""


def test_save_config_rejects_setup_completion_with_enabled_platform_without_runtime_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    import pytest

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    created = api.save_config(payload)
    assert created.platforms.enabled == []

    with pytest.raises(ValueError, match="Config 'lark.app_id', 'lark.app_secret' must be provided"):
        api.save_config(
            {
                "platform": "lark",
                "platforms": {"enabled": ["lark"], "primary": "lark"},
                "lark": {"domain": "feishu"},
                "setup_completed": True,
            }
        )


def test_save_config_allows_unrelated_save_for_legacy_enabled_platform_without_runtime_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["platform"] = "slack"
    payload["platforms"] = {"enabled": ["slack"], "primary": "slack"}
    payload["slack"] = {"bot_token": "", "app_token": ""}
    V2Config.from_payload(payload).save()

    updated = api.save_config({"remote_access": {"vibe_cloud": {"enabled": False}}})

    assert updated.platforms.enabled == ["slack"]
    assert updated.slack.bot_token == ""
    assert updated.remote_access.vibe_cloud.enabled is False


def test_save_config_allows_redacted_lark_round_trip_for_legacy_missing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    payload = _full_config_payload()
    payload["platform"] = "lark"
    payload["platforms"] = {"enabled": ["lark"], "primary": "lark"}
    payload["lark"] = {"app_id": "cli_lark_id", "app_secret": "", "domain": "feishu"}
    V2Config.from_payload(payload).save()

    updated = api.save_config(
        {
            "lark": {
                "app_id": "cli_lark_id",
                "has_app_secret": True,
                "app_secret_length": 0,
                "domain": "lark",
            }
        }
    )

    assert updated.platforms.enabled == ["lark"]
    assert updated.lark.app_id == "cli_lark_id"
    assert updated.lark.app_secret == ""
    assert updated.lark.domain == "lark"

    import pytest

    with pytest.raises(ValueError, match="Config 'lark.app_secret' must be provided"):
        api.save_config(
            {
                "lark": {
                    "app_id": "cli_lark_changed",
                    "has_app_secret": True,
                    "app_secret_length": 0,
                    "domain": "lark",
                }
            }
        )


def test_save_config_preserves_disabled_platform_credentials(monkeypatch, tmp_path, sqlite_schema_db_factory):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    payload = _full_config_payload()
    payload["platform"] = "avibe"
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["lark"] = None
    created = api.save_config(payload)
    assert created.platforms.enabled == []
    assert created.lark is None

    updated = api.save_config(
        {
            "platform": "lark",
            "lark": {
                "app_id": "cli_test",
                "app_secret": "secret",
                "domain": "feishu",
            },
        }
    )

    assert updated.platforms.enabled == []
    assert updated.platforms.primary == "avibe"
    assert updated.lark is not None
    assert updated.lark.app_id == "cli_test"
    assert updated.lark.app_secret == "secret"

    enabled = api.save_config({"platform": "lark", "platforms": {"enabled": ["lark"], "primary": "lark"}})

    assert enabled.platforms.enabled == ["lark"]
    assert enabled.platforms.primary == "lark"
    assert enabled.lark is not None
    assert enabled.lark.app_id == "cli_test"
    assert enabled.lark.app_secret == "secret"


def test_init_sessions_is_noop_when_sessions_file_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    store = api.SessionsStore()
    store.state.session_mappings = {
        "discord::749794605024936027": {
            "codex": {"discord_1482432040375943208": "019d1f70-692b-7c32-b152-b4aef9e24002"}
        }
    }
    store.save()

    api.init_sessions()

    reloaded = api.SessionsStore()
    reloaded.load()
    assert reloaded.state.session_mappings == store.state.session_mappings


def test_config_post_does_not_call_init_sessions():
    source = Path("vibe/ui_server.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    functions = {node.name: node for node in module.body if isinstance(node, function_types)}
    pending = ["config_post"]
    save_path_nodes = {}
    while pending:
        name = pending.pop()
        if name in save_path_nodes:
            continue
        node = functions[name]
        save_path_nodes[name] = node
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in functions and child.id != name:
                pending.append(child.id)

    assert "_save_config_and_runtime_decisions" in save_path_nodes

    calls_init_sessions = []
    for name, function_node in save_path_nodes.items():
        for node in ast.walk(function_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "api" and node.func.attr == "init_sessions":
                    calls_init_sessions.append(name)

    assert calls_init_sessions == []


def test_settings_platforms_apply_uses_parent_platform_identity():
    source = Path("ui/src/components/settings/SettingsPlatformsPage.tsx").read_text(encoding="utf-8")

    assert "const handleApplyPlatform = async (platform: string, nextData: any)" in source
    assert "onApply={(data) => handleApplyPlatform(id, data)}" in source
    assert "const platform = String(nextData?.platform || '')" not in source


def test_settings_platforms_persists_discord_guild_scope_before_auto_enable():
    source = Path("ui/src/components/settings/SettingsPlatformsPage.tsx").read_text(encoding="utf-8")

    assert "const savePlatformSettings = async (platform: string, nextData: any)" in source
    assert "platform === 'discord'" in source
    assert "await api.saveSettings({" in source
    assert "await savePlatformSettings(platform, nextData);" in source


def test_platform_runnable_config_keeps_wechat_token_optional():
    source = Path("ui/src/lib/platforms.ts").read_text(encoding="utf-8")

    assert "if (platform === 'wechat')" in source
    assert "return Boolean(data?.wechat);" in source


def test_wizard_platform_selection_submits_only_edited_credential_drafts():
    source = Path("ui/src/components/steps/PlatformSelection.tsx").read_text(encoding="utf-8")

    assert "const nextData = {" in source
    assert "const dirtySections" in source
    assert "dirtyPayload[key] = credentialDraft[key]" in source
    assert "...dirtyPayload," in source
    assert "await onSave(nextData);" in source
    assert "onNext(nextData);" in source
    assert "onNext(selectionData);" not in source
    assert "...credentialDraft," not in source


def test_ui_config_writes_have_one_mutation_boundary():
    """Every browser config write must pass through ApiContext's mutation serializer."""

    ui_root = Path("ui/src")
    raw_save_callers = []
    direct_config_posts = []
    for path in (*ui_root.rglob("*.ts"), *ui_root.rglob("*.tsx")):
        if ".test." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        if "saveConfig" in text:
            raw_save_callers.append(str(path))
        if path.name != "ApiContext.tsx" and re.search(
            r"\b(?:postJson|apiFetch)\(\s*['\"]\/api\/config", text
        ):
            direct_config_posts.append(str(path))

    assert raw_save_callers == []
    assert direct_config_posts == []


# ---------------------------------------------------------------------------
# Config field-completeness (guards the "partial save silently drops a field"
# class of bug). ``save_config`` deep-merges the incoming payload onto
# ``config_to_payload(load_config())`` as its base; any field the base payload
# omits is lost whenever a save does not itself re-send that field. So every
# persisted config field MUST appear in both serializers.
# ---------------------------------------------------------------------------


def test_config_to_payload_includes_avault_agent():
    """Regression: ``config_to_payload`` dropped ``agents.avault`` entirely, so
    every UI save reset ``agents.avault.cli_path`` to the dataclass default."""
    config = V2Config.from_payload(_full_config_payload())
    config.agents.avault.cli_path = "/opt/managed/avault"

    payload = api.config_to_payload(config)

    assert "avault" in payload["agents"]
    assert payload["agents"]["avault"]["cli_path"] == "/opt/managed/avault"


def test_save_config_preserves_avault_cli_path_on_unrelated_partial_save(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """A partial UI save (e.g. toggling ``show_duration``) must NOT reset a
    previously-persisted ``agents.avault.cli_path`` (set by ``vibe runtime
    prepare`` -> ``_persist_avault_cli_path``)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    full = _full_config_payload()
    full["agents"]["avault"] = {"cli_path": "/opt/managed/avault"}
    created = api.save_config(full)
    assert created.agents.avault.cli_path == "/opt/managed/avault"

    updated = api.save_config({"show_duration": False})

    assert updated.agents.avault.cli_path == "/opt/managed/avault"
    assert api.config_to_payload(updated)["agents"]["avault"]["cli_path"] == "/opt/managed/avault"


def test_save_config_preserves_ui_fields_on_unrelated_partial_save(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """The owner-facing scenario: after enabling ``show_agent_activity`` (and a
    custom font size / instance name), an unrelated partial save must keep them.
    Guards the ``ui`` sub-block of the deep-merge base."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    full = _full_config_payload()
    full["ui"] = {
        **full["ui"],
        "show_agent_activity": True,
        "chat_message_font_size": 20,
        "instance_name": "OwnerBox",
    }
    created = api.save_config(full)
    assert created.ui.show_agent_activity is True

    updated = api.save_config({"show_duration": False})

    assert updated.ui.show_agent_activity is True
    assert updated.ui.chat_message_font_size == 20
    assert updated.ui.instance_name == "OwnerBox"


def test_full_config_serializers_cover_every_config_field(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """Mechanism guard for the whole class: both full-config serializers
    (``V2Config.save`` on disk and ``config_to_payload``, the save merge base)
    must emit every persisted field — top-level, every ``UiConfig`` sub-field,
    and every agent backend. A newly-added field hand-listed into only one
    serializer (or neither) fails here, so it cannot silently drop on save."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    config = api.save_config(_full_config_payload())

    ui_field_names = {f.name for f in fields(UiConfig)}
    runtime_field_names = {f.name for f in fields(RuntimeConfig)}
    # ``platform_configs`` is the internal per-platform aggregate; it is emitted
    # under each platform's own key, not as a top-level ``platform_configs`` key.
    top_level = {f.name for f in fields(V2Config)} - {"platform_configs"}
    agents = {"opencode", "claude", "codex", "avault"}

    def _assert_complete(label: str, payload: dict) -> None:
        assert top_level <= set(payload), f"{label} top-level missing: {top_level - set(payload)}"
        assert ui_field_names <= set(payload["ui"]), f"{label} ui missing: {ui_field_names - set(payload['ui'])}"
        assert runtime_field_names <= set(payload["runtime"]), (
            f"{label} runtime missing: {runtime_field_names - set(payload['runtime'])}"
        )
        assert agents <= set(payload["agents"]), f"{label} agents missing: {agents - set(payload['agents'])}"
        assert payload["agents"]["opencode"]["active_turn_timeout_seconds"] == 7200

    _assert_complete("config_to_payload", api.config_to_payload(config))

    import json

    from config import paths

    _assert_complete("V2Config.save", json.loads(paths.get_config_path().read_text(encoding="utf-8")))


def test_save_config_strips_codex_relay_marker_from_generic_patch(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """The relay marker is auth-owned state like ``api_key``: a generic
    Settings save that round-trips a stale full-config snapshot (loaded
    before an OAuth transition captured the marker) must not null the
    fresh marker."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")

    original = api.save_config(_full_config_payload())
    original.agents.codex.oauth_relay_marker = {
        "provider_id": "OpenAI",
        "base_url": "https://relay.example/v1",
    }
    original.save()

    # Stale snapshot still carrying a null marker (the pre-transition view).
    updated = api.save_config({"agents": {"codex": {"oauth_relay_marker": None}}})

    assert updated.agents.codex.oauth_relay_marker == {
        "provider_id": "OpenAI",
        "base_url": "https://relay.example/v1",
    }


def test_non_owner_config_keeps_asr_and_pairing_without_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "inst_123"
    config.remote_access.vibe_cloud.backend_url = "https://avibe.bot"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-secret"
    config.remote_access.vibe_cloud.session_secret = "session-secret"
    config.audio_asr.enabled = True
    config.audio_asr.echo_transcript = False
    config.audio_asr.model = "secret-model"

    payload = api.non_owner_config_payload(config)

    assert payload["audio_asr"] == {
        "enabled": True,
        "echo_transcript": False,
        "enabled_configured": False,
    }
    assert payload["remote_access"] == {"vibe_cloud": {"paired": True}}
    assert payload["platforms"]["enabled"]
    assert payload["platform_catalog"]
    serialized = str(payload)
    assert "tunnel-secret" not in serialized
    assert "session-secret" not in serialized
    assert "instance-secret" not in serialized
    assert "secret-model" not in serialized
    assert "runtime" not in payload
    assert "agents" not in payload


def test_non_owner_config_projects_exactly_the_declared_surface(tmp_path, monkeypatch):
    """The projection is the writable surface plus the context that renders it.

    An equality, not a list of forbidden keys: a field added to either
    declaration but not projected fails here, and so does any key that starts
    leaking into non-owner responses. That is the same intent the older
    ``"remote_access" not in payload`` check carried, stated so that it also
    covers the keys nobody has thought of yet.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.from_payload(_full_config_payload())

    payload = api.non_owner_config_payload(config)

    assert set(payload) == set(api._EDITOR_CONFIG_WRITE_FIELDS) | set(
        api._NON_OWNER_CONFIG_CONTEXT_FIELDS
    )
    assert set(api._EDITOR_CONFIG_UI_WRITE_FIELDS) <= set(payload["ui"])
    assert set(api._EDITOR_AUDIO_ASR_WRITE_FIELDS) <= set(payload["audio_asr"])


@pytest.mark.parametrize(
    "missing_field",
    ["backend_url", "instance_id", "instance_secret"],
)
def test_non_owner_config_pairing_requires_runtime_ready_cloud(tmp_path, monkeypatch, missing_field):
    """Every credential the ASR runtime needs also gates the projected flag.

    One case per member of the runtime requirement, so a recovered or
    partially migrated instance can never advertise a pairing the runtime
    refuses to use.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.from_payload(_full_config_payload())
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.backend_url = "https://avibe.bot"
    config.remote_access.vibe_cloud.instance_id = "inst_123"
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    assert api.non_owner_config_payload(config)["remote_access"] == {"vibe_cloud": {"paired": True}}

    setattr(config.remote_access.vibe_cloud, missing_field, "")

    assert api.non_owner_config_payload(config)["remote_access"] == {"vibe_cloud": {"paired": False}}
    assert AudioAsrService(config)._runtime_config() is None


def test_malformed_cloud_section_is_refused_on_writes_and_recovered_on_disk(tmp_path, monkeypatch):
    """A non-string cloud value is refused; a persisted one degrades, never 500s.

    Two boundaries, two answers, from one rule. ``V2Config.from_payload`` is
    where an API write is validated, so a number where a credential belongs is
    refused rather than emptied — emptying is a repair, and this repair clears
    a credential the caller never asked to clear, then answers 200. Disk
    loading is the one path allowed to repair, so the same file recovers the
    section behind a backup and a warning, which keeps the malformed shape a
    disabled cloud feature rather than a 500 on every ``/api/config`` read.

    Seeded over every field the dataclass declares as ``str`` rather than over
    the three the pairing predicate reads, because the section is copied
    verbatim out of the stored config and any of them can reach code that does
    string work. Seeding is complete by construction, so a field added later
    is covered without editing this test.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"

    def _payload(vibe_cloud: dict) -> dict:
        payload = _full_config_payload()
        payload["remote_access"] = {"provider": "vibe_cloud", "vibe_cloud": vibe_cloud}
        return payload

    def _load_from_disk(vibe_cloud: dict) -> V2Config:
        config_path.write_text(json.dumps(_payload(vibe_cloud)), encoding="utf-8")
        return V2Config.load(config_path)

    def _assert_degrades(config: V2Config) -> None:
        assert config.remote_access.vibe_cloud.is_runtime_paired() is False
        assert AudioAsrService(config)._runtime_config() is None
        assert api.non_owner_config_payload(config)["remote_access"] == {
            "vibe_cloud": {"paired": False}
        }
        assert api.client_config_payload(config)["remote_access"]["vibe_cloud"]["paired"] is False

    string_fields = [
        info.name
        for info in fields(VibeCloudRemoteAccessConfig)
        # ``instance_kind`` is server-set at pairing and means "unknown" when it
        # is not one of the kinds this build knows, so for it an unreadable
        # value and an unrecognised one are the same answer.
        if info.type in (str, "str") and info.name != "instance_kind"
    ]
    assert string_fields

    paired = {
        "enabled": True,
        "backend_url": "https://avibe.bot",
        "instance_id": "inst_123",
        "instance_secret": "instance-secret",
    }
    assert V2Config.from_payload(_payload(paired)).remote_access.vibe_cloud.is_runtime_paired()

    for name in string_fields:
        # The write path names the field it refused, so the Settings page can
        # say which one, and the disk path recovers rather than failing.
        with pytest.raises(ValueError, match=re.escape(f"remote_access.vibe_cloud.{name}")):
            V2Config.from_payload(_payload({**paired, name: 12345}))
        config = _load_from_disk({**paired, name: 12345})
        assert any("remote_access" in warning for warning in config.load_warnings), name
        _assert_degrades(config)

    # Every field at once, so recovery is not being carried by one lucky field.
    config = _load_from_disk({"enabled": True, **{name: 12345 for name in string_fields}})
    assert config.load_warnings
    _assert_degrades(config)


def _settings_write_paths() -> list[tuple[str, ...]]:
    """Every preference path the shared Settings pages may write.

    Read out of the route's own allowlists instead of restated here, so a
    preference added to them is covered by the tests below without editing
    them.
    """
    paths: list[tuple[str, ...]] = []
    for key in sorted(api._EDITOR_CONFIG_WRITE_FIELDS):
        if key == "ui":
            paths += [("ui", sub) for sub in sorted(api._EDITOR_CONFIG_UI_WRITE_FIELDS)]
        elif key == "audio_asr":
            paths += [("audio_asr", sub) for sub in sorted(api._EDITOR_AUDIO_ASR_WRITE_FIELDS)]
        else:
            paths.append((key,))
    # ``show_pages_prompt`` is Owner-only, so it is absent from the Editor
    # allowlist while sharing the same validation block.
    return [*paths, ("show_pages_prompt",)]


def _nested_patch(path: tuple[str, ...], value: object) -> dict:
    patch: object = value
    for key in reversed(path):
        patch = {key: patch}
    return patch  # type: ignore[return-value]


def _walk(root: object, path: tuple[str, ...], *, attr: bool) -> object:
    for key in path:
        root = getattr(root, key) if attr else root[key]  # type: ignore[index]
    return root


def test_wrong_typed_settings_writes_are_refused_instead_of_silently_defaulted(
    tmp_path, monkeypatch, sqlite_schema_db_factory,
):
    """``/api/config`` stores what the caller sent, or it refuses the write.

    Seeded over the whole Settings write surface and driven through both roles,
    rather than over the preferences a review happened to name: the paths come
    from the allowlists the route itself uses, so the field nobody enumerated
    is covered too.

    The defect being pinned is silent success, not a bad stored value. A stale
    or non-browser client posting ``{"show_duration": "true"}`` was answered
    200 while the stored value became ``False`` — a request to switch a
    preference on switched it off, and nothing said so. ``ack_mode``, three
    lines above the same block, already raised; this asserts the whole surface
    answers alike.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())

    def _stored() -> dict:
        return api.config_to_payload(api.load_config(), include_secrets=True, include_internal=True)

    baseline = _stored()
    paths = _settings_write_paths()
    assert paths

    for path in paths:
        for value in ([], {}, None):
            label = (".".join(path), value)
            with pytest.raises(ValueError):
                api.save_config(_nested_patch(path, value))
            if path[0] in api._EDITOR_CONFIG_WRITE_FIELDS:
                with pytest.raises(ValueError):
                    api.save_config(api.editor_config_write_payload(_nested_patch(path, value)))
            assert _stored() == baseline, label

    # Posting each stored value back still succeeds, so the refusals above
    # cannot be produced by a route that refuses every write.
    for path in paths:
        api.save_config(_nested_patch(path, _walk(baseline, path, attr=False)))
    assert _stored() == baseline

    # The review's own payload: asking to switch a preference on must never be
    # answered 200 with the preference switched off.
    api.save_config({"show_duration": True})
    with pytest.raises(ValueError, match="show_duration"):
        api.save_config({"show_duration": "true"})
    assert api.load_config().show_duration is True


def test_wrong_typed_settings_on_disk_degrade_instead_of_failing_the_load(tmp_path, monkeypatch):
    """Refusing over HTTP must not turn a hand-edited config into a dead start.

    ``from_payload`` is the strict boundary and ``load`` is the recovering one
    (``_reset_recoverable_config_section`` says so in as many words), so every
    refusal added above has to come back out of the recovery table rather than
    reaching the caller. Seeded over the same surface as the write test, which
    is what makes the two halves one statement instead of two lists.

    Recovery is also held to what it may not decide on the config's behalf. It
    may not answer for a field other than the one it repaired, so a sibling the
    file still spells out survives; and it may not leave an optional feature
    running, so a section declaring ``enabled`` comes back with it off no matter
    which of its fields was unreadable.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    healthy = api.config_to_payload(
        V2Config.from_payload(_full_config_payload()), include_secrets=True, include_internal=True
    )
    config_path.write_text(json.dumps(healthy), encoding="utf-8")
    assert V2Config.load(config_path).load_warnings == ()

    for path in _settings_write_paths():
        label = ".".join(path)
        payload = copy.deepcopy(healthy)
        _walk(payload, path[:-1], attr=False)[path[-1]] = []  # type: ignore[index]
        config_path.write_text(json.dumps(payload), encoding="utf-8")

        config = V2Config.load(config_path)

        assert any(path[0] in warning for warning in config.load_warnings), label
        declared = _walk(V2Config.default(), path, attr=True)
        expected = (
            False
            if path[0] in _FIELD_SCOPED_RECOVERY_SECTIONS and isinstance(declared, bool)
            else declared
        )
        repaired = copy.deepcopy(healthy)
        _walk(repaired, path[:-1], attr=False)[path[-1]] = expected  # type: ignore[index]
        if len(path) > 1:
            section = _walk(repaired, path[:-1], attr=False)
            if isinstance(section, dict) and "enabled" in section:
                # An optional feature that needed recovering does not come back
                # running, whichever of its fields was the unreadable one.
                section["enabled"] = False
        # The load must equal the parse of the file with that one field written
        # as its recovered value. Stated against the parser rather than against
        # the constant above because some of these fields are derived — the
        # recovered ``audio_asr.enabled: false`` is what makes
        # ``enabled_configured`` true — and a constant would assert the raw
        # value the parser is contracted to move off.
        reparsed = V2Config.from_payload(copy.deepcopy(repaired))
        assert _walk(config, path, attr=True) == _walk(reparsed, path, attr=True), label
        recovered = api.config_to_payload(config, include_secrets=True, include_internal=True)
        if len(path) > 1:
            # The recovered section must be exactly what that file would have
            # parsed to — so every sibling survives, including the ones derived
            # from it. Asserting sibling-by-sibling would instead pass for a
            # recovery that quietly re-derived a flag nobody listed.
            assert (
                recovered[path[0]]
                == api.config_to_payload(reparsed, include_secrets=True, include_internal=True)[
                    path[0]
                ]
            ), label

    # A number that names no setting reaches the file by a route none of the
    # shapes above take. ``json`` reads a hand-edited ``1e309`` and the
    # ``Infinity`` an older release serialized into the same float, and on an
    # ``int`` field converting it raises ``OverflowError`` — not the
    # ``ValueError`` this recovery is built on, so it escaped ``load`` and took
    # startup down with it, which is the one outcome this test exists to rule
    # out. Asserted as "recovers like any other unreadable value" rather than
    # against a hand-written expectation, so it cannot drift from the sweep
    # above, and seeded from the declared numeric fields of the held sections,
    # so a number added to one of them is covered without editing this.
    assert json.loads("1e309") == float("inf")
    assert json.loads(json.dumps(float("inf"))) == float("inf")
    numeric_paths = [
        (section, info.name)
        for section in _FIELD_SCOPED_RECOVERY_SECTIONS
        for info in fields(V2Config.__dataclass_fields__[section].default_factory)
        if any(kind in str(info.type) for kind in ("int", "float"))
    ]
    assert len(numeric_paths) >= 3, numeric_paths
    for path in numeric_paths:
        label = ".".join(path)
        unreadable = copy.deepcopy(healthy)
        _walk(unreadable, path[:-1], attr=False)[path[-1]] = []  # type: ignore[index]
        config_path.write_text(json.dumps(unreadable), encoding="utf-8")
        like_any_other = api.config_to_payload(
            V2Config.load(config_path), include_secrets=True, include_internal=True
        )
        for number in (float("inf"), float("-inf"), float("nan")):
            payload = copy.deepcopy(healthy)
            _walk(payload, path[:-1], attr=False)[path[-1]] = number  # type: ignore[index]
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            config = V2Config.load(config_path)

            assert any(path[0] in warning for warning in config.load_warnings), (label, number)
            assert (
                api.config_to_payload(config, include_secrets=True, include_internal=True)
                == like_any_other
            ), (label, number)
        # Everything outside the corrupted section survives. Without this the
        # test would pass just as well for a recovery that threw the whole file
        # away, which for a scalar looks identical to repairing that scalar.
        assert {key: value for key, value in recovered.items() if key != path[0]} == {
            key: value for key, value in healthy.items() if key != path[0]
        }, label


def test_unreadable_optional_feature_fields_never_load_the_feature_back_on(tmp_path, monkeypatch):
    """Recovery keeps the siblings it can read, and never leaves a feature on.

    Two properties from two questions that must not share an answer. *How much
    is discarded* — replacing a section wholesale answers an unreadable font
    size by hiding the tool-call rows and an unreadable model name by
    forgetting the endpoint, so the unreadable field is repaired alone and its
    siblings stay as written. *Whether the feature still runs* — it does not:
    a section that declares ``enabled`` comes back disabled, whichever of its
    fields was unreadable, which is what ``AGENTS.md`` means by "a broken
    optional-feature section disables that feature and warns".

    The second property reverses an earlier round of this PR, which argued the
    two directions were symmetric. They are not: leaving ASR on ships user
    audio off-machine under settings nobody wrote and cannot be undone, while
    leaving it off costs a warning the operator reads before re-enabling.

    Stated over the declared fields of every field-scoped section rather than
    over the fields either review named, so a field or a whole section added to
    ``_FIELD_SCOPED_RECOVERY_SECTIONS`` is covered without editing this test.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    base = api.config_to_payload(
        V2Config.from_payload(_full_config_payload()), include_secrets=True, include_internal=True
    )

    def _load(payload: dict) -> V2Config:
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return V2Config.load(config_path)

    def _section_payload(config: V2Config, section: str) -> dict:
        return api.config_to_payload(config, include_secrets=True, include_internal=True)[section]

    recovered_fields: set[tuple[str, str]] = set()
    every_switch: set[tuple[str, str]] = set()
    for section in _FIELD_SCOPED_RECOVERY_SECTIONS:
        declared = fields(V2Config.__dataclass_fields__[section].default_factory)
        switches = [info.name for info in declared if info.type in (bool, "bool")]
        assert switches, section
        every_switch |= {(section, name) for name in switches}
        # Both sides, so "keeps the sibling as written" is a real statement
        # rather than one that happens to match the declared defaults.
        for side in (False, True):
            seed = copy.deepcopy(base)
            seed[section].update({name: side for name in switches})
            assert _load(seed).load_warnings == (), (section, side)

            for info in declared:
                for unreadable in ([], "garbage", {"nope": 1}):
                    corrupted = copy.deepcopy(seed)
                    corrupted[section][info.name] = unreadable
                    try:
                        V2Config.from_payload(corrupted)
                    except (TypeError, ValueError):
                        pass
                    else:
                        # This field reads that value, so there is nothing to
                        # recover. Driving the sweep off the parser rather than
                        # off a table of types keeps a newly validated field
                        # covered without editing this test.
                        continue
                    recovered_fields.add((section, info.name))

                    config = _load(corrupted)
                    assert config.load_warnings, (section, side, info.name, unreadable)

                    # A declared switch resolves off; anything else falls back
                    # to its own default; and a section that declares a feature
                    # switch comes back with it off. Either way the section must
                    # be exactly what the file would have parsed to had it been
                    # written that way — which is how every sibling, including a
                    # flag derived from the repaired field, is held in a single
                    # assertion.
                    repaired = copy.deepcopy(seed)
                    repaired[section][info.name] = (
                        False
                        if info.name in switches
                        else getattr(getattr(V2Config.default(), section), info.name)
                    )
                    if "enabled" in switches:
                        repaired[section]["enabled"] = False
                    assert _section_payload(config, section) == _section_payload(
                        V2Config.from_payload(repaired), section
                    ), (section, side, info.name, unreadable)

        # An unreadable section keeps no sibling, so every switch resolves off
        # — stated the same way, as the parse of a section written that way.
        broken = copy.deepcopy(base)
        broken[section] = 5
        config = _load(broken)
        assert config.load_warnings, section
        all_off = copy.deepcopy(base)
        all_off[section] = {name: False for name in switches}
        assert _section_payload(config, section) == _section_payload(
            V2Config.from_payload(all_off), section
        ), section

    # The sweep skips any field the parser reads happily, so state that no
    # switch was skipped: a switch that stopped being validated would make the
    # property above vacuous exactly where it matters.
    assert every_switch <= recovered_fields, every_switch - recovered_fields

    # Both reviews' own cases: the switch itself, then the siblings. Whichever
    # field was unreadable, and whichever side the file had asked for, an
    # ``audio_asr`` that needed recovering comes back not transcribing.
    assert "enabled" in {
        info.name
        for info in fields(V2Config.__dataclass_fields__["audio_asr"].default_factory)
        if info.type in (bool, "bool")
    }
    for side in (False, True):
        stored = copy.deepcopy(base)
        stored["audio_asr"].update({"enabled": side, "enabled_configured": True})
        for corrupt_field in (
            "enabled",
            "model",
            "timeout_seconds",
            "echo_transcript",
            "enabled_configured",
        ):
            payload = copy.deepcopy(stored)
            payload["audio_asr"][corrupt_field] = []
            recovered = _load(payload)
            assert recovered.load_warnings, (side, corrupt_field)
            assert recovered.audio_asr.enabled is False, (side, corrupt_field)

    # Disabled, not discarded: the siblings the file could still be read for
    # survive, so this is not the whole-section wipe in a new costume.
    kept = copy.deepcopy(base)
    kept["audio_asr"].update(
        {"enabled": True, "endpoint_path": "/v1/custom/transcriptions", "model": []}
    )
    recovered = _load(kept)
    assert recovered.audio_asr.enabled is False
    assert recovered.audio_asr.endpoint_path == "/v1/custom/transcriptions"
    assert recovered.audio_asr.model == V2Config.default().audio_asr.model

    # A section with no feature switch is presentation, not an optional feature:
    # it keeps every sibling and disables nothing beyond the unreadable field.
    hidden_rows = copy.deepcopy(base)
    hidden_rows["ui"].update({"show_tool_calls": False, "instance_name": "kept"})
    hidden_rows["ui"]["chat_message_font_size"] = "garbage"
    recovered = _load(hidden_rows)
    assert recovered.ui.show_tool_calls is False
    assert recovered.ui.instance_name == "kept"
    assert recovered.ui.instance_name == "kept"
    assert recovered.ui.chat_message_font_size == V2Config.default().ui.chat_message_font_size

    # Two unreadable fields of one section are two repairs, not a reason to
    # discard the file: the recovery loop dedupes per field, not per section.
    both = copy.deepcopy(base)
    both["ui"].update(
        {"show_agent_activity": "garbage", "show_tool_calls": 5, "instance_name": "kept"}
    )
    recovered = _load(both)
    assert recovered.ui.show_agent_activity is False
    assert recovered.ui.show_tool_calls is False
    assert recovered.ui.instance_name == "kept"


@pytest.mark.parametrize(
    "remote_access",
    (
        None,
        False,
        [],
        0,
        "",
        {
            "provider": "vibe_cloud",
            "vibe_cloud": {
                "instance_id": "inst-hijacked",
                "backend_url": "https://attacker.example",
                "future_pairing_field": "new-secret",
            },
        },
    ),
)
def test_non_owner_config_write_refuses_pairing_identity_in_any_value_shape(remote_access):
    """Pairing identity is Owner-only, in every value shape, by the same allowlist.

    This used to be a dedicated ``remote_access`` strip applied to non-owner
    writes. It is now the closed allowlist doing it: ``remote_access`` is not a
    field any writer below Owner may set, so the write is refused rather than
    silently accepted with one key removed -- which is also what stops a falsy
    patch from wiping stored pairing.
    """

    payload = {"ack_mode": "message", "remote_access": remote_access}

    with pytest.raises(ValueError) as excinfo:
        api.editor_config_write_payload(payload)

    assert api.editor_config_write_error_code(excinfo.value) == "editor_config_write_forbidden"


def test_editor_config_write_payload_keeps_messaging_fields_only():
    projected = api.editor_config_write_payload(
        {
            "audio_asr": {"enabled": False, "enabled_configured": True, "echo_transcript": False},
            "ack_mode": "message",
            "ui": {"chat_message_font_size": 16, "show_agent_activity": True},
        }
    )

    assert projected == {
        "audio_asr": {"enabled": False, "enabled_configured": True, "echo_transcript": False},
        "ack_mode": "message",
        "ui": {"chat_message_font_size": 16, "show_agent_activity": True},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"runtime": {"default_cwd": "/tmp/owned"}},
        {"audio_asr": {"enabled": False, "model": "owned-model"}},
        {"ui": {"setup_host": "0.0.0.0"}},
        {"ui": {"instance_name": "renamed"}},
        {"ui": {"default_instance_name": "owned"}},
        {"show_pages_prompt": False},
    ],
)
def test_editor_config_write_payload_rejects_owner_fields(payload):
    with pytest.raises(ValueError, match="editor_config_write_forbidden"):
        api.editor_config_write_payload(payload)


@pytest.mark.parametrize("payload", [[1], "ack_mode", 1, None])
def test_editor_config_write_payload_rejects_non_object_with_stable_code(payload):
    with pytest.raises(ValueError, match="editor_config_write_invalid"):
        api.editor_config_write_payload(payload)


def test_editor_config_write_error_code_keeps_every_failure_localizable():
    """Any failure on an Editor write answers with a code the client can render.

    The codes this module raises pass through; everything else — the English
    sentences raised much later by ``V2Config.from_payload`` — collapses to the
    generic invalid code, so no message can reach a non-English client
    unlocalized just because nobody enumerated it here.
    """
    for code in api._EDITOR_CONFIG_WRITE_ERROR_CODES:
        assert api.editor_config_write_error_code(ValueError(code)) == code

    for exc in (
        ValueError("Config 'ack_mode' must be one of typing, reaction, message"),
        ValueError("Config 'agent_progress_style' must be 'off', 'concise', or 'verbose'"),
        ValueError(""),
    ):
        assert api.editor_config_write_error_code(exc) == "editor_config_write_invalid"


def test_save_config_list_ops_merge_against_lock_fresh_base(monkeypatch, tmp_path):
    """The ``__avibe_list_ops`` verb mutates the CURRENT persisted list
    instead of replacing it with a stale browser snapshot: a toggle that
    was computed before another process added a platform must not drop
    that platform (#1458 stage ③ list semantics)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    original = api.save_config(_full_config_payload())
    # Another process seeds lark credentials and enables lark AFTER the
    # browser rendered its snapshot.
    api.save_config(
        {
            "lark": {"app_id": "cli_lark_id", "app_secret": "lark-secret"},
            "platforms": {"enabled": ["discord", "lark"]},
        }
    )

    # The browser's stale snapshot only knows slack; the toggle wants to
    # remove slack — expressed as an operation, not a list replacement.
    updated = api.save_config(
        {
          "__avibe_list_ops": {"platforms.enabled": {"remove": ["discord"]}},
        }
    )

    assert "discord" not in updated.platforms.enabled
    assert "lark" in updated.platforms.enabled


def test_list_ops_enable_requires_credentials(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """List-op additions are validated like explicit list saves: enabling
    a platform without credentials through the verb is rejected (#1458
    stage ③ list semantics)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())

    import pytest

    with pytest.raises(ValueError, match="lark"):
        api.save_config({"__avibe_list_ops": {"platforms.enabled": {"add": ["lark"]}}})


def test_list_ops_reject_unwhitelisted_paths(monkeypatch, tmp_path, sqlite_schema_db_factory):
    """Only whitelisted list paths accept operations — arbitrary dotted
    paths (e.g. model_hub.sources, which must go through ModelHubService)
    are rejected instead of silently mutating the persisted base."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())

    import pytest

    with pytest.raises(ValueError, match="not supported"):
        api.save_config(
            {"__avibe_list_ops": {"model_hub.sources": {"add": ["rogue"]}}}
        )


@pytest.mark.parametrize(
    "operations",
    [
        {"add": 1},
        {"remove": 1},
        {"add": ["discord", 1]},
        {"remove": [""]},
        {"add": ["discord"], "replace": ["slack"]},
    ],
)
def test_list_ops_reject_malformed_operands(monkeypatch, tmp_path, operations, sqlite_schema_db_factory):
    """Malformed list operations are client errors, never a server 500."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sqlite_schema_db_factory(tmp_path / "state" / "vibe.sqlite")
    api.save_config(_full_config_payload())

    with pytest.raises(ValueError, match="Config list operation"):
        api.save_config({"__avibe_list_ops": {"platforms.enabled": operations}})
