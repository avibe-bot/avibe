"""Model Hub native-config migration scenarios over real HTTP and IPC."""

from __future__ import annotations

import hashlib
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
                    "ANTHROPIC_API_KEY": SYNTHETIC_MIGRATION_API_KEY,
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
    seeded_api_key_digest = hashlib.sha256(
        seeded_api_key.encode("utf-8")
    ).hexdigest()
    _configure_protocol(
        mock_llm_upstream,
        "anthropic",
        models=[{"id": "claude-sonnet-4-6"}],
    )
    mock_llm_upstream.configure(required_api_key=seeded_api_key)
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
            "/api/models/migration/apply", {"item_ids": item_ids}
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

        captured_digests = set()
        for request in mock_llm_upstream.requests():
            headers = request["headers"]
            credential = headers.get("x-api-key")
            if credential is None:
                authorization = headers.get("authorization", "")
                prefix = "Bearer "
                credential = (
                    authorization[len(prefix) :]
                    if authorization.startswith(prefix)
                    else authorization
                )
            if credential:
                captured_digests.add(
                    hashlib.sha256(credential.encode("utf-8")).hexdigest()
                )
        assert seeded_api_key_digest in captured_digests
        catalogue_headers = [
            request["headers"]
            for request in mock_llm_upstream.requests()
            if request["path"] == "/v1/models"
        ]
        assert any(headers.get("x-api-key") == seeded_api_key for headers in catalogue_headers)
        assert any(
            not headers.get("x-api-key") and not headers.get("authorization")
            for headers in catalogue_headers
        )


@pytest.mark.parametrize(
    "required_api_key", [None, "sk-ant-e2e-different-key"],
    ids=["public-catalogue", "rejected-key"],
)
def test_f2_unproven_key_keeps_all_native_material(
    model_hub_app_factory, mock_llm_upstream, required_api_key: str | None,
) -> None:
    """F2: a public catalogue or credential rejection cannot authorize cleanup."""
    _configure_protocol(
        mock_llm_upstream, "anthropic",
        models=[{"id": "claude-sonnet-4-6"}],
    )
    mock_llm_upstream.configure(required_api_key=required_api_key)
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
        assert response.status == 409, response.json()
        assert response.json()["error"] == "migration_item_conflict"
        assert all(path.read_bytes() == before[path] for path in seeded_paths)
        listed = app.client.get("/api/models/sources")
        assert listed.status == 200, listed.json()
        assert listed.json()["sources"] == []
