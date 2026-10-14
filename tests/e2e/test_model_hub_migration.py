"""Model Hub native-config migration scenarios over real HTTP and IPC."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.test_model_hub_sources import _configure_protocol


pytestmark = pytest.mark.e2e_model_hub

SYNTHETIC_MIGRATION_API_KEY = "sk-ant-e2e-claude-independent-123456"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_native_configs(app, upstream_url: str) -> list[Path]:
    home = app.home
    paths = [
        home / ".claude" / "settings.json",
        home / ".codex" / "auth.json",
        home / ".codex" / "config.toml",
        home / ".config" / "opencode" / "opencode.json",
        home / ".local" / "share" / "opencode" / "auth.json",
        home / ".cache" / "opencode" / "models.json",
    ]
    _write(
        paths[0],
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": SYNTHETIC_MIGRATION_API_KEY,
                    "ANTHROPIC_BASE_URL": upstream_url,
                },
                "permissions": {"allow": ["Read"]},
            },
            indent=2,
        )
        + "\n",
    )
    _write(
        paths[1],
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "codex-oauth-e2e-123456"},
            }
        ),
    )
    _write(paths[2], 'cli_auth_credentials_store = "file"\n')
    _write(
        paths[3],
        json.dumps(
            {
                "provider": {
                    "alibaba-cn": {
                        "options": {
                            "apiKey": "sk-unsupported-e2e-123456",
                            "baseURL": upstream_url,
                        }
                    }
                }
            }
        ),
    )
    _write(
        paths[4],
        json.dumps(
            {
                "alibaba-cn": {
                    "type": "api",
                    "key": "sk-unsupported-e2e-123456",
                }
            }
        ),
    )
    _write(
        paths[5],
        json.dumps(
            {
                "alibaba-cn": {
                    "id": "alibaba-cn",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": upstream_url,
                }
            }
        ),
    )
    return paths


def _launch_seeded_app(model_hub_app_factory, mock_llm_upstream):
    seeded_paths: list[Path] = []

    def seed(app) -> None:
        seeded_paths.extend(
            _seed_native_configs(app, mock_llm_upstream.url)
        )

    return model_hub_app_factory(before_start=seed), seeded_paths


def test_f1_native_scan_returns_the_grouped_full_custody_action_matrix(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F1: complete credentials import; blocked credentials stay visible."""

    launch, _ = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        response = app.client.post("/api/models/migration/scan", {})
        body = response.json()
        assert response.status == 200, body
        items = body["scan"]["items"]
        assert [
            (
                item["backend"],
                item["kind"],
                item["proposed_action"],
                item["selected"],
            )
            for item in items
        ] == [
            ("claude", "api_key", "import", True),
            ("codex", "oauth_native", "keep_native", False),
            ("opencode", "api_key", "reauth", False),
        ]
        serialized = json.dumps(items)
        for secret in (
            "sk-ant-e2e-claude-independent-123456",
            "codex-oauth-e2e-123456",
            "sk-unsupported-e2e-123456",
        ):
            assert secret not in serialized


def test_f1_unsupported_opencode_provider_ids_remain_visible_blockers(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F1: an unsupported native OpenCode provider blocks only OpenCode."""

    launch, _ = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        response = app.client.post("/api/models/migration/scan", {})
        opencode_items = [
            item
            for item in response.json()["scan"]["items"]
            if item["backend"] == "opencode"
        ]
        assert [
            (item["kind"], item["proposed_action"], item["selected"])
            for item in opencode_items
        ] == [("api_key", "reauth", False)]
        assert opencode_items[0]["notes_key"] == (
            "settings.models.migration.blocked.credential"
        )


def test_f2_apply_takes_over_selected_key_and_preserves_blocked_native_material(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F2: grouped custody cleans the selected key without touching blockers."""

    seeded_api_key = SYNTHETIC_MIGRATION_API_KEY
    _configure_protocol(
        mock_llm_upstream,
        "anthropic",
        models=[{"id": "claude-sonnet-4-6"}],
    )
    mock_llm_upstream.configure(required_api_key=seeded_api_key, required_auth_scheme="bearer")
    launch, seeded_paths = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        before = {path: path.read_bytes() for path in seeded_paths}
        scan = app.client.post("/api/models/migration/scan", {})
        scan_body = scan.json()
        assert scan.status == 200, scan_body
        item_ids = [
            item["id"]
            for item in scan_body["scan"]["items"]
            if item["selected"] is True
        ]
        assert [
            (item["backend"], item["kind"])
            for item in scan_body["scan"]["items"]
            if item["id"] in item_ids
        ] == [("claude", "api_key")]
        mock_llm_upstream.reset_requests()

        applied = app.client.post(
            "/api/models/migration/apply", {"item_ids": item_ids, "clean_api_keys": True}
        )
        body = applied.json()
        assert applied.status == 200, body
        assert body["applied"] == 1
        assert [
            (source["vendor"], source["kind"], source["supply_channel"])
            for source in body["sources"]
        ] == [
            ("anthropic", "api_key", "hub"),
        ]
        claude_settings = seeded_paths[0]
        assert json.loads(claude_settings.read_text(encoding="utf-8")) == {
            "env": {},
            "permissions": {"allow": ["Read"]},
        }
        assert all(
            path.read_bytes() == before[path] for path in seeded_paths[1:]
        )

        listed = app.client.get("/api/models/sources")
        assert listed.status == 200, listed.json()
        assert [source["id"] for source in listed.json()["sources"]] == [
            source["id"] for source in body["sources"]
        ]
        serialized = json.dumps(body)
        assert "sk-ant-e2e-claude-independent-123456" not in serialized
        assert "codex-oauth-e2e-123456" not in serialized

        # Migration moves configuration, not upstream authentication evidence.
        assert mock_llm_upstream.requests() == []
        assert body["sources"][0]["verification_pending"]
        assert body["sources"][0]["last_discovered_at"] is None


@pytest.mark.parametrize("clean_api_keys", [False, True])
def test_mh_mig_015_keys_migrate_over_http_without_any_upstream_request(
    model_hub_app_factory, mock_llm_upstream, clean_api_keys,
) -> None:
    """MH-MIG-015: HTTP/IPC imports even rejected keys, without probing them.

    Service doubles alone cannot show that the real custody/runtime adapters do
    not authenticate or fetch inventories. This local upstream rejects every
    credential; its request log must remain empty through migration and replay.
    """
    mock_llm_upstream.configure(auth="401", protocol="openai_chat")
    credentials = {
        "deepseek": "fixture-stale-native-key",
        "custom-relay": "fixture-unchecked-native-key",
    }
    paths: list[Path] = []

    def seed(app) -> None:
        path = app.home / ".config/opencode/opencode.json"
        paths.append(path)
        _write(path, json.dumps({"provider": {
            vendor: {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"apiKey": key, "baseURL": mock_llm_upstream.url},
                "models": {"configured-model": {"name": "Configured model"}},
            }
            for vendor, key in credentials.items()
        }}))

    with model_hub_app_factory(before_start=seed) as app:
        before = paths[0].read_bytes()
        scan = app.client.post("/api/models/migration/scan", {})
        assert scan.status == 200, scan.json()
        items = scan.json()["scan"]["items"]
        assert {item["vendor"] for item in items} == set(credentials)
        request = {"item_ids": [item["id"] for item in items], "clean_api_keys": clean_api_keys}
        response = app.client.post("/api/models/migration/apply", request)
        body = response.json()
        assert response.status == 200, body
        assert body["applied"] == 2
        assert {source["vendor"] for source in body["sources"]} == set(credentials)
        assert mock_llm_upstream.requests() == []
        for source in body["sources"]:
            assert source["protocol"] == "openai_chat"
            assert source["verification_pending"]
            assert source["last_discovered_at"] is None
            assert [(model["id"], model["origin"]) for model in source["models"]] == [
                ("configured-model", "manual"),
            ]
        assert all(key not in json.dumps(body) for key in credentials.values())
        if clean_api_keys:
            assert all(key not in paths[0].read_text() for key in credentials.values())
        else:
            assert paths[0].read_bytes() == before
        replay = app.client.post("/api/models/migration/apply", request)
        assert replay.status == 200, replay.json()
        assert replay.json()["applied"] == 2
        assert {source["id"] for source in replay.json()["sources"]} == {
            source["id"] for source in body["sources"]
        }
        assert mock_llm_upstream.requests() == []


@pytest.mark.parametrize(
    "required_api_key", [None, "sk-ant-e2e-different-key"],
    ids=["public-catalogue", "rejected-key"],
)
def test_f2_unproven_key_is_copied_without_upstream_validation(
    model_hub_app_factory, mock_llm_upstream, required_api_key: str | None,
) -> None:
    """F2: upstream authentication is not admission to a configuration transfer."""
    _configure_protocol(
        mock_llm_upstream, "anthropic",
        models=[{"id": "claude-sonnet-4-6"}],
    )
    mock_llm_upstream.configure(required_api_key=required_api_key, required_auth_scheme="bearer")
    launch, seeded_paths = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream,
    )
    with launch as app:
        before = {path: path.read_bytes() for path in seeded_paths}
        scan = app.client.post("/api/models/migration/scan", {})
        assert scan.status == 200, scan.json()
        selected = [
            item for item in scan.json()["scan"]["items"] if item["selected"]
        ]
        assert [(item["backend"], item["kind"]) for item in selected] == [
            ("claude", "api_key")
        ]
        response = app.client.post(
            "/api/models/migration/apply", {"item_ids": [item["id"] for item in selected]},
        )
        body = response.json()
        assert response.status == 200, body
        assert body["applied"] == 1
        assert body["sources"][0]["verification_pending"]
        assert mock_llm_upstream.requests() == []
        assert all(path.read_bytes() == before[path] for path in seeded_paths)
        listed = app.client.get("/api/models/sources")
        assert listed.status == 200, listed.json()
        assert len(listed.json()["sources"]) == 1
