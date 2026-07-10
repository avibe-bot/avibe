import json
import time

import pytest

from vibe import backend_model_catalog


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_backend_model_entries_normalize_string_and_dict_entries():
    catalog = {
        "schema_version": 1,
        "backends": {
            "codex": {
                "models": [
                    "gpt-custom",
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}],
                    },
                ]
            }
        },
    }

    entries = backend_model_catalog.backend_model_entries("codex", catalog)

    assert entries == [
        {"id": "gpt-custom"},
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "reasoning_efforts": ["low", "ultra"],
        },
    ]


def test_bundled_codex_56_efforts_include_ultra():
    catalog = backend_model_catalog.load_bundled_catalog()
    entries = {
        backend_model_catalog.model_id(entry): backend_model_catalog.reasoning_efforts(entry)
        for entry in backend_model_catalog.backend_model_entries("codex", catalog)
    }

    assert "ultra" in entries["gpt-5.6-sol"]
    assert "ultra" in entries["gpt-5.6-terra"]


def test_refresh_remote_catalog_persists_state_cache(monkeypatch, tmp_path):
    payload = {
        "schema_version": 1,
        "backends": {
            "claude": {
                "models": [
                    {"id": "claude-fable-6", "reasoning_efforts": ["low", "max"]},
                ]
            }
        },
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(payload),
    )
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()

    catalog = backend_model_catalog.refresh_remote_catalog_now("https://example.test/catalog.json")

    assert backend_model_catalog.backend_model_entries("claude", catalog)[0]["id"] == "claude-fable-6"
    cached_payload = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert cached_payload["catalog"]["backends"]["claude"]["models"][0]["id"] == "claude-fable-6"
    assert cached_payload["error"] is None
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
    reloaded = backend_model_catalog.load_cached_remote_catalog(schedule_refresh=False)
    assert backend_model_catalog.backend_model_entries("claude", reloaded)[0]["id"] == "claude-fable-6"
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()


def test_malformed_remote_catalog_preserves_last_good_cache(monkeypatch, tmp_path):
    previous_catalog = {
        "schema_version": 1,
        "backends": {"claude": {"models": [{"id": "claude-fable-6"}]}},
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse({"schema_version": 2, "models": []}),
    )
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
    backend_model_catalog._write_cached_remote_payload(
        {"fetched_at": 100.0, "catalog": previous_catalog, "error": None}
    )
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", True)

    backend_model_catalog._refresh_remote_catalog_worker()

    cached_payload = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert cached_payload["catalog"] == previous_catalog
    assert cached_payload["failed_at"] == 200.0
    assert "fetched_at" not in cached_payload
    assert "Unsupported backend model catalog schema version" in cached_payload["error"]
    assert backend_model_catalog._REMOTE_REFRESH_IN_FLIGHT is False
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()


def test_fetch_remote_catalog_rejects_invalid_model_entries(monkeypatch):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codex": {"models": [{"label": "missing id"}]}},
            }
        ),
    )

    with pytest.raises(ValueError, match="invalid model entry"):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


def test_failed_refresh_with_stale_catalog_uses_failure_ttl():
    payload = {
        "fetched_at": time.time() - backend_model_catalog.REMOTE_CATALOG_TTL_SECONDS + 60,
        "failed_at": time.time() - backend_model_catalog.REMOTE_CATALOG_FAILURE_TTL_SECONDS - 1,
        "catalog": {"backends": {"codex": {"models": ["gpt-5.6-sol"]}}},
    }

    assert backend_model_catalog._remote_cache_stale(payload) is True


def test_remote_catalog_refresh_pending_tracks_in_flight_and_token_changes(monkeypatch):
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
    backend_model_catalog._REMOTE_MEMORY_CACHE.update({"fetched_at": 10.0, "catalog": {}})
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", False)

    token = backend_model_catalog.remote_catalog_token()

    assert backend_model_catalog.remote_catalog_refresh_pending(token) is False
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", True)
    assert backend_model_catalog.remote_catalog_refresh_pending(token) is True
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", False)
    backend_model_catalog._REMOTE_MEMORY_CACHE["fetched_at"] = 11.0
    assert backend_model_catalog.remote_catalog_refresh_pending(token) is True
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
