"""Model Hub native-config migration scenarios over real HTTP and IPC."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.test_model_hub_sources import _configure_protocol


pytestmark = pytest.mark.e2e_model_hub


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
                    "ANTHROPIC_API_KEY": "sk-ant-e2e-copy-only-123456",
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


def test_f1_native_scan_returns_the_copy_only_action_matrix(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F1: Claude key and Codex login map to import/keep-native actions."""

    launch, _ = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        response = app.client.post("/api/models/migration/scan", {})
        body = response.json()
        assert response.status == 200, body
        items = body["scan"]["items"]
        assert [
            (item["backend"], item["kind"], item["proposed_action"])
            for item in items
        ] == [
            ("claude", "api_key", "import"),
            ("codex", "oauth_native", "keep_native"),
        ]
        assert all(item["selected"] is True for item in items)
        serialized = json.dumps(items)
        for secret in (
            "sk-ant-e2e-copy-only-123456",
            "codex-oauth-e2e-123456",
            "sk-unsupported-e2e-123456",
        ):
            assert secret not in serialized


@pytest.mark.xfail(
    reason=(
        "F1 product/contract gap: unsupported OpenCode provider ids are silently "
        "dropped and migration-scan.schema.json has no notice field"
    )
)
def test_f1_unsupported_opencode_provider_ids_are_noted(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F1: an unsupported native OpenCode provider must be visible as a note."""

    launch, _ = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        response = app.client.post("/api/models/migration/scan", {})
        assert response.status == 200, response.json()
        assert any(
            item["backend"] == "opencode"
            and item.get("notes_key")
            for item in response.json()["scan"]["items"]
        )


def test_f2_apply_is_copy_only_and_places_native_login_before_keys(
    model_hub_app_factory,
    mock_llm_upstream,
) -> None:
    """F2: apply copies selected material and performs the one-time sort."""

    _configure_protocol(
        mock_llm_upstream,
        "anthropic",
        models=[{"id": "claude-sonnet-4-6"}],
    )
    launch, seeded_paths = _launch_seeded_app(
        model_hub_app_factory, mock_llm_upstream
    )
    with launch as app:
        before = {path: path.read_bytes() for path in seeded_paths}
        scan = app.client.post("/api/models/migration/scan", {})
        scan_body = scan.json()
        assert scan.status == 200, scan_body
        item_ids = [item["id"] for item in scan_body["scan"]["items"]]

        applied = app.client.post(
            "/api/models/migration/apply", {"item_ids": item_ids}
        )
        body = applied.json()
        assert applied.status == 200, body
        assert body["applied"] == 2
        assert [
            (source["vendor"], source["kind"], source["supply_channel"])
            for source in body["sources"]
        ] == [
            ("openai", "subscription", "native_cli"),
            ("anthropic", "api_key", "hub"),
        ]
        assert all(path.read_bytes() == content for path, content in before.items())

        listed = app.client.get("/api/models/sources")
        assert listed.status == 200, listed.json()
        assert [source["id"] for source in listed.json()["sources"]] == [
            source["id"] for source in body["sources"]
        ]
        serialized = json.dumps(body)
        assert "sk-ant-e2e-copy-only-123456" not in serialized
        assert "codex-oauth-e2e-123456" not in serialized


@pytest.mark.xfail(
    reason=(
        "F3/B2 fix-first: the importable-items banner is mounted in the wizard "
        "but not on the first upgraded /settings/models visit"
    )
)
def test_f3_first_models_page_visit_surfaces_the_migration_banner() -> None:
    """F3: importable native configuration is visible on first page open."""

    pytest.fail("F3 requires the pending settings-page banner product change")
