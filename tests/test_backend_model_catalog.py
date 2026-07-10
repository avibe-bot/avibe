import json
import time

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


def test_failed_refresh_with_stale_catalog_uses_failure_ttl():
    payload = {
        "fetched_at": time.time() - backend_model_catalog.REMOTE_CATALOG_TTL_SECONDS + 60,
        "failed_at": time.time() - backend_model_catalog.REMOTE_CATALOG_FAILURE_TTL_SECONDS - 1,
        "catalog": {"backends": {"codex": {"models": ["gpt-5.6-sol"]}}},
    }

    assert backend_model_catalog._remote_cache_stale(payload) is True
