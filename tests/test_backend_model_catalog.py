import builtins
import json
import threading
import time
from types import SimpleNamespace

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


@pytest.fixture(autouse=True)
def _reset_remote_cache(monkeypatch):
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", False)
    yield
    backend_model_catalog._REMOTE_MEMORY_CACHE.clear()


def test_backend_model_entries_normalize_runtime_catalog_shape():
    catalog = {
        "schema_version": 1,
        "backends": {
            "codex": {
                "models": [
                    "gpt-custom",
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "visibility": "list",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "ultra"},
                        ],
                    },
                ]
            }
        },
    }

    assert backend_model_catalog.backend_model_entries("codex", catalog) == [
        {"id": "gpt-custom"},
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "visibility": "list",
            "reasoning_efforts": ["low", "ultra"],
        },
    ]


def test_merge_sources_applies_tombstones_and_fills_missing_metadata():
    merged = backend_model_catalog.merge_model_sources(
        [
            (
                "remote",
                [
                    {"id": "hidden-model", "visibility": "hidden"},
                    {"id": "shared-model", "label": "Remote label"},
                ],
            ),
            (
                "local",
                [
                    {"id": "hidden-model", "reasoning_efforts": ["ultra"]},
                    {"id": "shared-model", "reasoning_efforts": ["low", "high"]},
                    {"id": "local-model"},
                ],
            ),
        ]
    )

    assert [entry["id"] for entry in merged] == ["shared-model", "local-model"]
    assert merged[0] == {
        "id": "shared-model",
        "label": "Remote label",
        "reasoning_efforts": ["low", "high"],
        "source": "remote",
    }


def test_codex_local_hidden_tombstone_overrides_remote_visible(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "account-hidden", "visibility": "hide"},
                    {"slug": "account-visible", "visibility": "list"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {
                "codex": {
                    "models": [
                        {"id": "account-hidden"},
                        {"id": "remote-model"},
                    ]
                }
            },
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert "account-hidden" not in snapshot["models"]
    assert snapshot["models"][:2] == ["remote-model", "account-visible"]


def test_codex_local_efforts_override_remote_metadata(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "shared-model",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {
                "codex": {
                    "models": [
                        {
                            "id": "shared-model",
                            "reasoning_efforts": ["low", "high", "ultra"],
                        }
                    ]
                }
            },
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert [entry["value"] for entry in snapshot["reasoning_options"]["shared-model"]] == [
        "__default__",
        "low",
        "high",
    ]


def test_claude_snapshot_merges_configured_custom_models(monkeypatch, tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        json.dumps(
            {
                "model": "custom-claude-model",
                "env": {"ANTHROPIC_SMALL_FAST_MODEL": "custom-fast-model"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backend_model_catalog.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(backend_model_catalog, "load_cached_remote_catalog", lambda **kwargs: {})

    snapshot = backend_model_catalog.backend_model_snapshot("claude", schedule_refresh=False)

    assert "custom-claude-model" in snapshot["models"]
    assert "custom-fast-model" in snapshot["models"]
    assert snapshot["model_labels"]["claude-opus-4-6"] == "claude-opus-4-6 [1M]"


def test_remote_hidden_tombstone_overrides_stale_local_visible(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "retired-model", "visibility": "list"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        backend_model_catalog,
        "load_cached_remote_catalog",
        lambda **kwargs: {
            "schema_version": 1,
            "backends": {
                "codex": {
                    "models": [{"id": "retired-model", "visibility": "hidden"}]
                }
            },
        },
    )
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    assert "retired-model" not in snapshot["models"]


def test_snapshot_returns_immediately_while_remote_refresh_runs(monkeypatch, tmp_path):
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(backend_model_catalog, "load_bundled_catalog", lambda: {})

    def slow_refresh(url=None):
        refresh_started.set()
        release_refresh.wait(timeout=2)
        return {}

    monkeypatch.setattr(backend_model_catalog, "refresh_remote_catalog_now", slow_refresh)

    started_at = time.monotonic()
    snapshot = backend_model_catalog.backend_model_snapshot("claude")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert refresh_started.wait(timeout=1)
    assert snapshot["catalog_refresh_pending"] is True
    release_refresh.set()
    deadline = time.monotonic() + 1
    while backend_model_catalog._REMOTE_REFRESH_IN_FLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend_model_catalog._REMOTE_REFRESH_IN_FLIGHT is False


def test_refresh_remote_catalog_persists_validated_cache(monkeypatch, tmp_path):
    payload = {
        "schema_version": 1,
        "backends": {
            "claude": {
                "models": [{"id": "claude-fable-6", "reasoning_efforts": ["low", "max"]}]
            }
        },
    }
    monkeypatch.setattr(backend_model_catalog.paths, "get_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(payload),
    )

    catalog = backend_model_catalog.refresh_remote_catalog_now("https://example.test/catalog.json")

    assert backend_model_catalog.backend_model_entries("claude", catalog)[0]["id"] == "claude-fable-6"
    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert persisted["catalog"] == catalog
    assert persisted["error"] is None


def test_malformed_refresh_preserves_last_good_catalog(monkeypatch, tmp_path):
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
    backend_model_catalog._write_cached_remote_payload(
        {"fetched_at": 100.0, "catalog": previous_catalog, "error": None}
    )
    monkeypatch.setattr(backend_model_catalog.time, "time", lambda: 200.0)
    monkeypatch.setattr(backend_model_catalog, "_REMOTE_REFRESH_IN_FLIGHT", True)

    backend_model_catalog._refresh_remote_catalog_worker()

    persisted = json.loads((tmp_path / "backend_model_catalog.json").read_text(encoding="utf-8"))
    assert persisted["catalog"] == previous_catalog
    assert persisted["fetched_at"] == 100.0
    assert persisted["failed_at"] == 200.0
    assert "Unsupported backend model catalog schema version" in persisted["error"]


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


@pytest.mark.parametrize(
    "model_entry, error",
    [
        ({"id": "gpt-invalid", "visibility": "sometimes"}, "invalid visibility"),
        ({"id": "gpt-invalid", "priority": "first"}, "invalid priority"),
        ({"id": "gpt-invalid", "reasoning_efforts": "ultra"}, "invalid reasoning efforts"),
    ],
)
def test_fetch_remote_catalog_rejects_malformed_model_metadata(monkeypatch, model_entry, error):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codex": {"models": [model_entry]}},
            }
        ),
    )

    with pytest.raises(ValueError, match=error):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


def test_fetch_remote_catalog_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(
        backend_model_catalog.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {
                "schema_version": 1,
                "backends": {"codxe": {"models": [{"id": "gpt-typo"}]}},
            }
        ),
    )

    with pytest.raises(ValueError, match="unsupported backend"):
        backend_model_catalog.fetch_remote_catalog("https://example.test/catalog.json")


def test_bundled_codex_56_efforts_include_ultra():
    snapshot = backend_model_catalog.backend_model_snapshot("codex", schedule_refresh=False)

    values = {
        entry["value"]
        for entry in snapshot["reasoning_options"]["gpt-5.6-terra"]
    }
    assert "ultra" in values


def test_codex_catalog_readers_expand_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-state"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-expanded", "visibility": "list"}]}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "gpt-configured"', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "~/codex-state")

    assert backend_model_catalog._read_codex_models_cache() == [
        {"id": "gpt-expanded", "visibility": "list"}
    ]
    assert backend_model_catalog._read_codex_config_models()[0]["id"] == "gpt-configured"


def test_parse_toml_falls_back_to_tomli_when_tomllib_is_unavailable(monkeypatch):
    real_import = builtins.__import__
    fallback = SimpleNamespace(loads=lambda raw: {"model": "gpt-python-310"})

    def fake_import(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        if name == "tomli":
            return fallback
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert backend_model_catalog._parse_toml('model = "gpt-python-310"') == {
        "model": "gpt-python-310"
    }
